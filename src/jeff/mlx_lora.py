"""LoRA adapters for the MLX backend: several PEFT adapters (jeff.lora) side by side on one MLX Qwen3.5 model.

mlx-lm's own LoRA layer holds one adapter in its own file format; this layer holds every adapter's A and B matrices,
read from PEFT's adapter_model.safetensors, and applies the active one: linear(x) + (x A^T)(sB)^T, in the model's dtype
or in float32 as PEFT does (the precision chosen when loading). No active adapter gives exactly the plain layer."""

import json
from pathlib import Path
from typing import cast

import mlx.core as mx
from mlx.nn.layers.base import Module
from mlx.nn.layers.linear import Linear
from mlx.utils import tree_unflatten

PEFT_PREFIX = "base_model.model."


class MultiLoRALinear(Module):
    def __init__(self, linear: Module) -> None:
        super().__init__()  # type: ignore[no-untyped-call]
        self.linear = linear
        # name -> {"a": (rank, in), "b": (out, rank) with the scale alpha / rank folded in}, in the adapter's precision:
        # the model's dtype (computed in it), or float32 (x cast up and the update cast back, as PEFT does)
        self.adapters: dict[str, dict[str, mx.array]] = {}
        self.active: str | None = None

    def __call__(self, x: mx.array) -> mx.array:
        y: mx.array = self.linear(x)
        if self.active is None:
            return y
        adapter = self.adapters[self.active]
        if adapter["a"].dtype == x.dtype:
            return y + (x @ adapter["a"].T) @ adapter["b"].T
        return y + ((x.astype(mx.float32) @ adapter["a"].T) @ adapter["b"].T).astype(y.dtype)


def read_peft(directory: Path) -> tuple[float, dict[str, tuple[mx.array, mx.array]]]:
    """The LoRA scale (alpha / rank) and each adapted layer's (A, B), by the layer's name in the PyTorch backbone."""
    config = json.loads((directory / "adapter_config.json").read_text())
    if config.get("peft_type") != "LORA" or config.get("use_rslora") or config.get("use_dora") \
            or config.get("rank_pattern") or config.get("alpha_pattern") or config.get("bias") != "none":
        raise ValueError(f"{directory}: the MLX backend reads plain LoRA adapters (no rsLoRA, DoRA, per-layer ranks or biases)")
    scale = float(config["lora_alpha"]) / int(config["r"])
    weights = cast(dict[str, mx.array], mx.load(str(directory / "adapter_model.safetensors")))
    layers: dict[str, dict[str, mx.array]] = {}
    for key, value in weights.items():
        if not key.startswith(PEFT_PREFIX) or not key.endswith((".lora_A.weight", ".lora_B.weight")):
            raise ValueError(f"{directory}: unexpected adapter weight {key}")
        name, part = key[len(PEFT_PREFIX):].rsplit(".", 2)[0], key.rsplit(".", 2)[1]
        layers.setdefault(name, {})[part] = value.astype(mx.float32)
    if not layers or any(set(parts) != {"lora_A", "lora_B"} for parts in layers.values()):
        raise ValueError(f"{directory}: every adapted layer needs both lora_A and lora_B")
    return scale, {name: (parts["lora_A"], parts["lora_B"]) for name, parts in layers.items()}


def mlx_path(name: str) -> str:
    """A layer's name in the PyTorch Qwen3.5 backbone ("language_model.layers.0.mlp.gate_proj") as a path in the MLX
    model ("language_model.model.layers.0.mlp.gate_proj")."""
    if not name.startswith("language_model."):
        raise ValueError(f"Adapted layer {name} is outside the text decoder")
    return "language_model.model." + name[len("language_model."):]


def add_adapter(model: Module, name: str, directory: Path, precision: str) -> list[MultiLoRALinear]:
    """Load one adapter into the model, wrapping each layer it touches the first time an adapter touches it. Returns
    every adapter layer of the model. Nothing changes if the adapter does not fit. precision: "model" keeps A and B in
    the layer's dtype; "float32" keeps them in float32 (see MultiLoRALinear)."""
    if precision not in ("model", "float32"):
        raise ValueError(f"LoRA precision {precision!r}; use model or float32")
    modules = dict(model.named_modules())  # type: ignore[no-untyped-call]
    scale, layers = read_peft(directory)
    planned: list[tuple[str, Module, mx.array, mx.array]] = []
    for layer, (a, b) in layers.items():
        path = mlx_path(layer)
        module = modules.get(path)
        linear = module.linear if isinstance(module, MultiLoRALinear) else module
        if not isinstance(linear, Linear):
            raise ValueError(f"{directory}: {layer} is not a linear layer of the MLX model")
        out_features, in_features = linear.weight.shape
        if a.shape[1] != in_features or b.shape[0] != out_features or a.shape[0] != b.shape[1]:
            raise ValueError(f"{directory}: adapter shapes for {layer} do not fit the layer")
        planned.append((path, cast(Module, module), a, b))
    wrapped: list[tuple[str, MultiLoRALinear]] = []
    for path, module, a, b in planned:
        wrapper = module if isinstance(module, MultiLoRALinear) else MultiLoRALinear(module)
        if not isinstance(module, MultiLoRALinear):
            wrapped.append((path, wrapper))
        dtype = wrapper.linear.weight.dtype if precision == "model" else mx.float32
        wrapper.adapters[name] = {"a": a.astype(mx.float32).astype(dtype), "b": (b.astype(mx.float32) * scale).astype(dtype)}
    if wrapped:
        model.update_modules(tree_unflatten(wrapped))
    return adapter_layers(model)


def adapter_layers(model: Module) -> list[MultiLoRALinear]:
    return [module for _, module in model.named_modules() if isinstance(module, MultiLoRALinear)]  # type: ignore[no-untyped-call]


def remove_adapter(model: Module, name: str) -> None:
    for layer in adapter_layers(model):
        layer.adapters.pop(name, None)
        if layer.active == name:
            layer.active = None


def add_adapters(model: Module, adapters: dict[str, Path], precision: str) -> list[MultiLoRALinear]:
    layers: list[MultiLoRALinear] = []
    for name, directory in adapters.items():
        layers = add_adapter(model, name, directory, precision)
    return layers
