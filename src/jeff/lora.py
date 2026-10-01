"""LoRA adapters: one base Jeff checkpoint loaded once, and a small adapter per application (see DESIGN-lora.md).

An adapter is a folder with PEFT's adapter_config.json and adapter_model.safetensors (low-rank updates of the text
decoder's attention and MLP projections), the adapter's own readout.safetensors, and a decision_config.json like a full
checkpoint's with an extra "adapter" section: the rank and targets, and the base checkpoint it was trained on with the
SHA-256 of that base's weight files. An adapter is only ever used on exactly that base."""

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import torch
from safetensors.torch import load_file, save_file

from jeff.model import MAX_OPTIONS
from jeff.types import JSONValue

if TYPE_CHECKING:
    from peft import PeftModel

ADAPTER_FORMAT = 1
# Linear layers that get a low-rank update, by the last part of their name: full attention (Qwen3.5, Gemma: q/k/v/o;
# Phi-4-mini: qkv_proj, o_proj), Qwen3.5's linear attention (Gated DeltaNet; its tiny in_proj_a and in_proj_b gates,
# hidden -> number of heads, stay frozen) and the MLP (gate/up/down; Phi-4-mini: gate_up_proj, down_proj).
TARGET_NAMES = frozenset({"q_proj", "k_proj", "v_proj", "o_proj", "qkv_proj", "in_proj_qkv", "in_proj_z", "out_proj",
                          "gate_proj", "up_proj", "down_proj", "gate_up_proj"})
# Towers that text decisions never use; nothing inside them is adapted.
TOWERS = frozenset({"visual", "vision_tower", "vision_model", "audio_tower", "audio_model", "embed_vision", "embed_audio"})
NAME = re.compile(r"[a-z0-9][a-z0-9.-]{0,63}")


@dataclass(frozen=True)
class LoraSettings:
    rank: int
    alpha: int
    dropout: float = 0.0

    def __post_init__(self) -> None:
        if self.rank < 1 or self.alpha < 1 or not 0 <= self.dropout < 1:
            raise ValueError("LoRA needs a positive rank and alpha, and a dropout in [0, 1)")


@dataclass
class Head:
    """What an adapter changes besides the backbone: the answer readout, the temperature, the prompt layout and the
    option limit. The base checkpoint has one too."""
    readout: torch.nn.Linear
    temperature: float
    prompt_layout: str | None  # None for models without prompt layouts (the generic decoder)
    max_options: int | None


def target_modules(backbone: torch.nn.Module) -> list[str]:
    """Names of the linear layers of the text decoder that get a LoRA update. None found is an error."""
    names = [name for name, module in backbone.named_modules()
             if isinstance(module, torch.nn.Linear) and name.rsplit(".", 1)[-1] in TARGET_NAMES
             and not TOWERS & set(name.split("."))]
    if not names:
        raise ValueError(f"{type(backbone).__name__} has no attention or MLP projections named {sorted(TARGET_NAMES)}")
    return names


def weight_files(directory: Path) -> list[str]:
    """The backbone weight files of a full checkpoint: model.safetensors, or the shard index and its shards."""
    if (directory / "model.safetensors").is_file():
        return ["model.safetensors"]
    index = directory / "model.safetensors.index.json"
    if not index.is_file():
        raise ValueError(f"{directory} has no backbone weights (model.safetensors or a shard index)")
    shards = sorted(set(cast(dict[str, str], json.loads(index.read_text())["weight_map"]).values()))
    return [index.name, *shards]


def weights_sha256(directory: str | Path) -> dict[str, str]:
    """SHA-256 of each backbone weight file of a full checkpoint: the identity an adapter is bound to."""
    path = Path(directory)
    result = {}
    for name in weight_files(path):
        with (path / name).open("rb") as stream:
            result[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def is_adapter(checkpoint: str | Path) -> bool:
    return "adapter" in json.loads((Path(checkpoint) / "decision_config.json").read_text())


def attach(model: torch.nn.Module, settings: LoraSettings, base_checkpoint: str | Path) -> None:
    """Training: wrap the backbone with a new adapter. Every backbone weight is frozen; the LoRA weights and the
    readout train. `base_checkpoint` is the full checkpoint the model was loaded from."""
    from peft import LoraConfig, get_peft_model

    decoder = cast(Any, model)
    if getattr(decoder, "architecture", "qwen") not in ("qwen", "decoder-generic"):
        raise ValueError(f"LoRA adapters are implemented for chat decoders, not {decoder.architecture}")
    if getattr(decoder, "adapter", None) is not None:
        raise ValueError("The model already has an adapter")
    targets = target_modules(decoder.backbone)
    config = LoraConfig(r=settings.rank, lora_alpha=settings.alpha, lora_dropout=settings.dropout,
                        target_modules=targets, bias="none")
    decoder.backbone = get_peft_model(decoder.backbone, config)
    base = Path(base_checkpoint).resolve()
    decoder.adapter = {"format_version": ADAPTER_FORMAT, "rank": settings.rank, "alpha": settings.alpha,
                       "dropout": settings.dropout, "target_modules": targets,
                       "base_checkpoint": {"path": str(base), "weights_sha256": weights_sha256(base)}}
    decoder.to(decoder.device_name)


def save_adapter(model: torch.nn.Module, directory: str | Path, temperature: float | None = None,
                 **metadata: JSONValue) -> None:
    """Write a new adapter folder (the adapter's counterpart of DecisionModel.save)."""
    decoder = cast(Any, model)
    if getattr(decoder, "adapter", None) is None:
        raise ValueError("The model has no adapter to save")
    destination = Path(directory)
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"Refusing to overwrite checkpoint contents: {destination}")
    scale = decoder.temperature if temperature is None else temperature
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("Temperature must be positive and finite.")
    destination.mkdir(parents=True, exist_ok=True)
    cast("PeftModel", decoder.backbone).save_pretrained(str(destination))
    save_file({"weight": decoder.readout.weight.detach().cpu().contiguous()}, str(destination / "readout.safetensors"))
    config: dict[str, JSONValue] = dict(metadata)
    config.update({"format_version": 1, "base_model": decoder.base_model, "revision": decoder.revision,
                   "codes": list(decoder.codes), "token_ids": list(decoder.token_ids), "temperature": scale,
                   "adapter": decoder.adapter})
    if hasattr(decoder, "architecture"):
        config["architecture"] = decoder.architecture
    if hasattr(decoder, "prompt_layout"):
        config["prompt_layout"] = decoder.prompt_layout
    (destination / "decision_config.json").write_text(json.dumps(config, indent=2) + "\n")


def read_adapter(directory: str | Path) -> dict[str, JSONValue]:
    path = Path(directory)
    config = cast(dict[str, JSONValue], json.loads((path / "decision_config.json").read_text()))
    adapter = config.get("adapter")
    if config.get("format_version") != 1 or not isinstance(adapter, dict) or adapter.get("format_version") != ADAPTER_FORMAT:
        raise ValueError(f"{path} is not a Jeff adapter (decision_config.json needs format_version 1 and an adapter section)")
    for name in ("adapter_config.json", "adapter_model.safetensors", "readout.safetensors"):
        if not (path / name).is_file():
            raise ValueError(f"Adapter {path} has no {name}")
    return config


def check_base(config: dict[str, JSONValue], path: Path, model: torch.nn.Module, base_hashes: dict[str, str]) -> None:
    """The adapter at `path` must have been trained on exactly this base: same Hugging Face model and revision, same
    architecture and answer vocabulary, and the same weight files."""
    decoder = cast(Any, model)
    expected: dict[str, object] = {"base_model": decoder.base_model, "revision": decoder.revision,
                                   "architecture": getattr(decoder, "architecture", None),
                                   "codes": list(decoder.codes), "token_ids": list(decoder.token_ids)}
    for key, value in expected.items():
        if config.get(key) != value:
            raise ValueError(f"Adapter {path} was trained for a different base ({key} differs)")
    adapter = cast(dict[str, JSONValue], config["adapter"])
    trained_on = cast(dict[str, JSONValue], adapter["base_checkpoint"])["weights_sha256"]
    if trained_on != base_hashes:
        raise ValueError(f"Adapter {path} was trained on the base checkpoint {cast(dict[str, JSONValue], adapter['base_checkpoint'])['path']}, "
                         "whose weights differ from the loaded base checkpoint")


PRECISIONS = ("model", "float32")


class LoraLinear(torch.nn.Module):
    """A linear layer of the base with several LoRA updates beside it (serving only). `active` names the update in use;
    None gives exactly the plain layer. Each update is kept as (A, s*B), with the scale s = alpha / rank folded into B.

    precision "model": A and B in the base weights' dtype (bfloat16 on a GPU) and one fused multiply-add,
    y = base(x) + (x A^T)(sB)^T: two small matrix products per layer and no casts. precision "float32": computed in
    float32 as PEFT does (x cast up, the update cast back), the exact reference."""

    def __init__(self, base: torch.nn.Linear, precision: str) -> None:
        super().__init__()
        if precision not in PRECISIONS:
            raise ValueError(f"LoRA precision {precision!r}; use one of {PRECISIONS}")
        self.base = base
        self.precision = precision
        self.updates: dict[str, tuple[torch.Tensor, torch.Tensor]] = {}
        self.active: str | None = None

    def add(self, name: str, a: torch.Tensor, b: torch.Tensor, scale: float) -> None:
        weight = self.base.weight
        if a.shape[1] != weight.shape[1] or b.shape[0] != weight.shape[0] or a.shape[0] != b.shape[1]:
            raise ValueError(f"LoRA shapes {tuple(a.shape)} and {tuple(b.shape)} do not fit a {tuple(weight.shape)} layer")
        dtype = weight.dtype if self.precision == "model" else torch.float32
        self.updates[name] = (a.to(weight.device, torch.float32).to(dtype).contiguous(),
                              (b.to(weight.device, torch.float32) * scale).to(dtype).contiguous())

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        y: torch.Tensor = self.base(x)
        if self.active is None:
            return y
        a, b = self.updates[self.active]
        flat = x.reshape(-1, x.shape[-1])
        if self.precision == "model":
            return torch.addmm(y.reshape(-1, y.shape[-1]), flat @ a.t(), b.t()).reshape(y.shape)
        return y + ((flat.float() @ a.t()) @ b.t()).to(y.dtype).reshape(y.shape)


def read_lora(path: Path) -> tuple[float, dict[str, tuple[torch.Tensor, torch.Tensor]]]:
    """The scale (alpha / rank) and each adapted layer's (A, B) from PEFT's files, by the layer's name in the backbone.
    Only plain LoRA is served (no rsLoRA, DoRA, per-layer ranks or biases)."""
    config = json.loads((path / "adapter_config.json").read_text())
    if config.get("peft_type") != "LORA" or config.get("use_rslora") or config.get("use_dora") \
            or config.get("rank_pattern") or config.get("alpha_pattern") or config.get("bias") != "none":
        raise ValueError(f"{path}: only plain LoRA adapters are served (no rsLoRA, DoRA, per-layer ranks or biases)")
    prefix = "base_model.model."
    layers: dict[str, dict[str, torch.Tensor]] = {}
    for key, value in load_file(str(path / "adapter_model.safetensors")).items():
        if not key.startswith(prefix) or not key.endswith((".lora_A.weight", ".lora_B.weight")):
            raise ValueError(f"{path}: unexpected adapter weight {key}")
        name, part, _ = key[len(prefix):].rsplit(".", 2)
        layers.setdefault(name, {})[part] = value
    if not layers or any(set(parts) != {"lora_A", "lora_B"} for parts in layers.values()):
        raise ValueError(f"{path}: every adapted layer needs both lora_A and lora_B")
    scale = float(config["lora_alpha"]) / int(config["r"])
    return scale, {name: (parts["lora_A"], parts["lora_B"]) for name, parts in layers.items()}


def adapter_head(config: dict[str, JSONValue], path: Path, model: torch.nn.Module, base_layout: str | None) -> Head:
    """An adapter's readout, temperature, prompt layout and option limit, checked, on the model's device."""
    decoder = cast(Any, model)
    temperature = config["temperature"]
    if not isinstance(temperature, float) or not math.isfinite(temperature) or temperature <= 0:
        raise ValueError(f"Adapter {path} temperature must be positive and finite")
    layout = config.get("prompt_layout")
    if (layout is None) != (base_layout is None):
        raise ValueError(f"Adapter {path} prompt layout {layout!r} does not fit this model type")
    readout = torch.nn.Linear(decoder.readout.in_features, MAX_OPTIONS, bias=False, dtype=decoder.readout.weight.dtype)
    readout.load_state_dict(load_file(str(path / "readout.safetensors")))
    readout.requires_grad_(False)
    options = config.get("max_options")
    return Head(readout.to(decoder.device_name), temperature, None if layout is None else str(layout),
                options if isinstance(options, int) else None)


class AdapterSet:
    """Several adapters on one loaded base model (the shared-base serving mode). `use(name)` switches the model to an
    adapter (None: the base itself) without moving any weights: it selects the active update in every LoraLinear and
    swaps in that adapter's readout, temperature and prompt layout. Adapters can be added and removed while serving.
    The caller serialises all calls (the server's lock). `precision`: see LoraLinear."""

    def __init__(self, model: torch.nn.Module, base_checkpoint: str | Path, adapters: dict[str, Path] | None = None,
                 base_max_options: int | None = None, *, precision: str) -> None:
        if precision not in PRECISIONS:
            raise ValueError(f"LoRA precision {precision!r}; use one of {PRECISIONS}")
        decoder = cast(Any, model)
        if decoder.training:
            raise ValueError("Adapters are loaded for inference only")
        if getattr(decoder, "merged_adapter", None) is not None:
            raise ValueError(f"The model has the adapter {decoder.merged_adapter} merged into its weights")
        self.model = decoder
        self.precision = precision
        self.base_hashes = weights_sha256(base_checkpoint)
        self.layers: dict[str, LoraLinear] = {}
        self.heads: dict[str | None, Head] = {
            None: Head(decoder.readout, decoder.temperature, getattr(decoder, "prompt_layout", None), base_max_options)}
        for name, path in (adapters or {}).items():
            self.add_adapter(name, path)
        self.use(None)

    @property
    def names(self) -> list[str]:
        return [name for name in self.heads if name is not None]

    def add_adapter(self, name: str, path: Path) -> None:
        """Load one adapter folder beside the others; the base weights are shared, never copied. Nothing changes if
        the adapter does not fit."""
        if not NAME.fullmatch(name):
            raise ValueError(f"Adapter name {name!r}: use lower-case letters, digits, dots and dashes")
        if name in self.heads:
            raise ValueError(f"Adapter {name!r} is already loaded")
        config = read_adapter(path)
        check_base(config, path, self.model, self.base_hashes)
        head = adapter_head(config, path, self.model, self.heads[None].prompt_layout)
        scale, updates = read_lora(path)
        backbone: torch.nn.Module = self.model.backbone
        planned = []
        for layer, (a, b) in updates.items():
            module = self.layers.get(layer) or backbone.get_submodule(layer)
            linear = module.base if isinstance(module, LoraLinear) else module
            if not isinstance(linear, torch.nn.Linear):
                raise ValueError(f"{path}: {layer} is not a linear layer of the base")
            if a.shape[1] != linear.in_features or b.shape[0] != linear.out_features or a.shape[0] != b.shape[1]:
                raise ValueError(f"{path}: adapter shapes for {layer} do not fit the layer")
            planned.append((layer, linear, a, b))
        for layer, linear, a, b in planned:
            if layer not in self.layers:
                wrapper = LoraLinear(linear, self.precision)
                parent, _, child = layer.rpartition(".")
                setattr(backbone.get_submodule(parent) if parent else backbone, child, wrapper)
                self.layers[layer] = wrapper
            self.layers[layer].add(name, a, b, scale)
        self.heads[name] = head
        self.use(None)

    def remove_adapter(self, name: str) -> None:
        if name is None or name not in self.heads:
            raise ValueError(f"Unknown adapter {name!r}; loaded: {self.names}")
        for layer in self.layers.values():
            layer.updates.pop(name, None)
        del self.heads[name]
        self.use(None)

    def use(self, name: str | None) -> None:
        if name not in self.heads:
            raise ValueError(f"Unknown adapter {name!r}; loaded: {self.names}")
        for layer in self.layers.values():
            layer.active = name if name in layer.updates else None
        head = self.heads[name]
        self.model.readout = head.readout
        self.model.temperature = head.temperature
        if head.prompt_layout is not None:
            self.model.prompt_layout = head.prompt_layout


@torch.no_grad()
def merge_adapter(model: torch.nn.Module, base_checkpoint: str | Path, name: str, path: Path) -> Head:
    """The merged serving mode: fold ONE adapter into the base weights (W += s B A, computed in float32, stored in the
    weights' dtype) and make its readout, temperature and layout the model's, so it runs at the base's speed. The plain
    base is gone from this model; it is marked, and no adapter can be added to it."""
    decoder = cast(Any, model)
    if decoder.training:
        raise ValueError("Adapters are loaded for inference only")
    if getattr(decoder, "merged_adapter", None) is not None:
        raise ValueError(f"The adapter {decoder.merged_adapter} is already merged into this model")
    if any(isinstance(module, LoraLinear) for module in decoder.backbone.modules()):
        raise ValueError("The model already serves adapters in the shared-base mode")
    if not NAME.fullmatch(name):
        raise ValueError(f"Adapter name {name!r}: use lower-case letters, digits, dots and dashes")
    config = read_adapter(path)
    check_base(config, path, decoder, weights_sha256(base_checkpoint))
    head = adapter_head(config, path, decoder, getattr(decoder, "prompt_layout", None))
    scale, updates = read_lora(path)
    planned = []
    for layer, (a, b) in updates.items():
        linear = decoder.backbone.get_submodule(layer)
        if not isinstance(linear, torch.nn.Linear) or tuple(linear.weight.shape) != (b.shape[0], a.shape[1]) \
                or a.shape[0] != b.shape[1]:
            raise ValueError(f"{path}: adapter shapes for {layer} do not fit the layer")
        planned.append((linear, a, b))
    for linear, a, b in planned:
        weight = linear.weight
        delta = (b.to(weight.device, torch.float32) * scale) @ a.to(weight.device, torch.float32)
        weight.copy_((weight.float() + delta).to(weight.dtype))
    decoder.readout, decoder.temperature = head.readout, head.temperature
    if head.prompt_layout is not None:
        decoder.prompt_layout = head.prompt_layout
    decoder.merged_adapter = name
    return head


def load_adapted(directory: str | Path, *, precision: str = "float32", **kwargs: Any) -> torch.nn.Module:
    """An adapter as a ready decision model (for jeff-evaluate and jeff-latency): the base checkpoint recorded in the
    adapter, with the adapter attached and in use. Evaluation uses the float32 reference precision unless told otherwise."""
    from jeff.models import load_decision_model

    path = Path(directory)
    if kwargs.get("train"):
        raise ValueError("Training on top of an adapter is not supported; start from the full base checkpoint")
    if kwargs.get("prompt_layout") is not None:
        raise ValueError("An adapter uses the prompt layout it was trained with")
    config = read_adapter(path)
    base = Path(str(cast(dict[str, JSONValue], cast(dict[str, JSONValue], config["adapter"])["base_checkpoint"])["path"]))
    if not base.is_dir():
        raise FileNotFoundError(f"Adapter {path} needs its base checkpoint {base}, which does not exist here")
    if is_adapter(base):
        raise ValueError(f"The base {base} of adapter {path} is itself an adapter")
    model = load_decision_model(checkpoint=base, **kwargs)
    adapters = AdapterSet(model, base, {"adapter": path}, precision=precision)
    adapters.use("adapter")
    cast(Any, model).adapters = adapters
    return model
