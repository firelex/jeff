"""Run a Qwen3.5 Jeff checkpoint with MLX, Apple's framework, which has fast Metal kernels for Qwen3.5's Gated DeltaNet
layers (PyTorch on a Mac GPU only has a slow reference version of them).

The checkpoint is used as saved; nothing is converted on disk. MLX loads the backbone weights (renamed to the layout its
Qwen3.5 loader expects; the vision tower is left out, so this path is text only) and runs the model to its final
hidden state. Our trained answer readout, answer codes and fitted temperature are then applied exactly as in
jeff.model, and the prompt is built by the same function with the checkpoint's own chat template.

LoRA adapters (jeff.lora) can be loaded beside the base (jeff.mlx_lora); `use(name)` switches between them per request."""

import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from jeff.model import MAX_OPTIONS, PROMPT_LAYOUTS, decision_messages, options
from jeff.types import DecisionInput


MLX_CACHE_LIMIT_BYTES = 1 << 30


class MlxDecisionModel:
    backend = "mlx"

    def __init__(self, checkpoint: str | Path, adapters: dict[str, Path] | None = None, *, lora_precision: str = "model") -> None:
        import mlx.core as mx
        from mlx_lm.models.qwen3_5 import Model, ModelArgs
        from transformers import AutoProcessor

        directory = Path(checkpoint)
        decision = json.loads((directory / "decision_config.json").read_text())
        # Qwen3.5 checkpoints (jeff.model) record no architecture; Gemma and ModernBERT checkpoints name theirs.
        if decision.get("format_version") != 1 or "architecture" in decision:
            raise ValueError(f"{directory} is not a Qwen3.5 Jeff checkpoint")
        config = json.loads((directory / "config.json").read_text())
        if config.get("model_type") != "qwen3_5":
            raise ValueError(f"The MLX backend serves Qwen3.5 checkpoints; {directory} is {config.get('model_type')!r}")
        self.base_model = str(decision["base_model"])
        self.revision = str(decision["revision"])
        self.codes: list[str] = list(decision["codes"])
        self.token_ids: list[int] = list(decision["token_ids"])
        self.temperature = float(decision["temperature"])
        # Checkpoints made before prompt layouts existed use the original state-first order.
        self.prompt_layout = str(decision.get("prompt_layout", "state-first"))
        if self.prompt_layout not in PROMPT_LAYOUTS:
            raise ValueError(f"{directory} has an unknown prompt layout {self.prompt_layout!r}")
        if not math.isfinite(self.temperature) or self.temperature <= 0:
            raise ValueError("Temperature must be positive and finite.")
        self.mx = mx
        # MLX keeps freed buffers in a cache with no limit by default; under a stream of requests with varying prompt
        # lengths it grew to about 14 GB for a 2 GB model. 1 GiB holds memory at about 3.4 GB with unchanged latency.
        mx.set_cache_limit(MLX_CACHE_LIMIT_BYTES)
        self.model = Model(ModelArgs.from_dict(config))
        raw = mx.load(str(directory / "model.safetensors"))
        # Our checkpoints store the bare Qwen3.5 model ("language_model.*", "visual.*"); MLX's loader expects the
        # Hugging Face full-model names ("model.language_model.*") and applies its own conversions from there.
        renamed = {f"model.{name}": value for name, value in raw.items() if name.startswith("language_model.")}
        if not renamed:
            raise ValueError(f"{directory} has no language_model weights")
        self.model.load_weights(list(self.model.sanitize(renamed).items()), strict=True)
        self.model.eval()  # MLX modules start in training mode, which makes Qwen3.5 skip its fast Metal kernel
        self.readout = mx.load(str(directory / "readout.safetensors"))["weight"].astype(mx.float32)
        if self.readout.shape[0] != MAX_OPTIONS:
            raise ValueError(f"Readout has {self.readout.shape[0]} rows, expected {MAX_OPTIONS}")
        mx.eval(self.model.parameters(), self.readout)
        self.processor = AutoProcessor.from_pretrained(str(directory))
        # name -> (readout, temperature, prompt layout); None is the base itself
        self.heads: dict[str | None, tuple[Any, float, str]] = {None: (self.readout, self.temperature, self.prompt_layout)}
        self.lora_layers: list[Any] = []
        self.base_hashes: dict[str, str] | None = None  # computed when the first adapter is added
        self.directory = directory
        self.lora_precision = lora_precision
        for name, path in (adapters or {}).items():
            self.add_adapter(name, path)

    def add_adapter(self, name: str, path: Path) -> None:
        """Load one adapter folder beside the others (jeff.mlx_lora); the base weights are shared."""
        from jeff.lora import NAME, check_base, read_adapter, weights_sha256
        from jeff.mlx_lora import add_adapter

        mx = self.mx
        if not NAME.fullmatch(name):
            raise ValueError(f"Adapter name {name!r}: use lower-case letters, digits, dots and dashes")
        if name in self.heads:
            raise ValueError(f"Adapter {name!r} is already loaded")
        if self.base_hashes is None:
            self.base_hashes = weights_sha256(self.directory)
        config = read_adapter(path)
        check_base(config, path, self, self.base_hashes)  # type: ignore[arg-type]
        layout = str(config.get("prompt_layout", "state-first"))
        if layout not in PROMPT_LAYOUTS:
            raise ValueError(f"{path} has an unknown prompt layout {layout!r}")
        readout = mx.load(str(path / "readout.safetensors"))["weight"].astype(mx.float32)
        if readout.shape != self.heads[None][0].shape:
            raise ValueError(f"Adapter {path} readout has shape {readout.shape}, expected {self.heads[None][0].shape}")
        temperature = float(config["temperature"])  # type: ignore[arg-type]
        if not math.isfinite(temperature) or temperature <= 0:
            raise ValueError(f"Adapter {path} temperature must be positive and finite")
        self.lora_layers = add_adapter(self.model, name, path, self.lora_precision)
        self.heads[name] = (readout, temperature, layout)
        self.model.eval()
        mx.eval(self.model.parameters(), readout)
        self.use(None)

    def remove_adapter(self, name: str) -> None:
        from jeff.mlx_lora import remove_adapter

        if name is None or name not in self.heads:
            raise ValueError(f"Unknown adapter {name!r}")
        remove_adapter(self.model, name)
        del self.heads[name]
        self.use(None)

    def use(self, name: str | None) -> None:
        """Switch to an adapter, or to the base with None; nothing is loaded or moved."""
        if name not in self.heads:
            raise ValueError(f"Unknown adapter {name!r}; loaded: {[key for key in self.heads if key is not None]}")
        for layer in self.lora_layers:
            layer.active = name if name in layer.adapters else None
        self.readout, self.temperature, self.prompt_layout = self.heads[name]

    def prompt_ids(self, row: DecisionInput) -> list[int]:
        if row.get("images"):
            raise ValueError("The MLX backend reads text only; use the PyTorch backend for image decisions")
        text = self.processor.apply_chat_template(decision_messages(row, self.codes, self.prompt_layout), tokenize=False,
                                                  add_generation_prompt=True, enable_thinking=False)
        return list(self.processor.tokenizer(text, add_special_tokens=False)["input_ids"])

    def decide(self, rows: Sequence[DecisionInput]) -> list[tuple[list[float], int]]:
        """Probabilities over each row's options (temperature applied) and the number of input tokens, one row at a time."""
        mx = self.mx
        results = []
        for row in rows:
            count = len(options(row["question"])[0])
            ids = self.prompt_ids(row)
            hidden = self.model.language_model.model(mx.array([ids]))[0, -1].astype(mx.float32)
            logits = (self.readout[:count] @ hidden) / self.temperature
            probabilities = mx.softmax(logits, axis=-1)
            results.append(([float(p) for p in probabilities.tolist()], len(ids)))
        return results
