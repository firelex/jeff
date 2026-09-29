"""Choose the decision model architecture: the Qwen decoder (DecisionModel), any other chat decoder
(GenericDecoderDecisionModel, e.g. Gemma or Phi-4-mini) or the ModernBERT encoder."""

import json
import os
from pathlib import Path
from typing import Any

import torch

from jeff.decoder import DECODER_MODELS, GenericDecoderDecisionModel
from jeff.encoder import ENCODER_MODELS, EncoderDecisionModel
from jeff.model import DecisionModel


def architecture(checkpoint: str | Path | None, base_model: str | None) -> str:
    """A checkpoint says which architecture it is; otherwise the base model name decides."""
    if checkpoint is not None:
        return str(json.loads((Path(checkpoint) / "decision_config.json").read_text()).get("architecture", "qwen"))
    if base_model in ENCODER_MODELS:
        return EncoderDecisionModel.architecture
    if base_model in DECODER_MODELS:
        return GenericDecoderDecisionModel.architecture
    return "qwen"


def is_encoder(checkpoint: str | Path | None, base_model: str | None) -> bool:
    return architecture(checkpoint, base_model) == EncoderDecisionModel.architecture


def device_from_environment() -> str | None:
    """JEFF_DEVICE ("cuda", "mps" or "cpu") picks the device explicitly; unset keeps the default (CUDA when present,
    otherwise the CPU). A device this machine does not have is an error, not a quiet switch to the CPU."""
    wanted = os.getenv("JEFF_DEVICE")
    if wanted is None:
        return None
    available = {"cuda": torch.cuda.is_available(), "mps": torch.backends.mps.is_available(), "cpu": True}
    if wanted not in available:
        raise ValueError(f"JEFF_DEVICE={wanted!r}; use cuda, mps or cpu")
    if not available[wanted]:
        raise RuntimeError(f"JEFF_DEVICE={wanted} but this machine has no {wanted} device")
    return wanted


def load_decision_model(checkpoint: str | Path | None = None, **kwargs: Any) -> torch.nn.Module:
    kind = architecture(checkpoint, kwargs.get("base_model"))
    layout = kwargs.pop("prompt_layout", None)
    if layout is not None and kind != "qwen":
        raise ValueError(f"A prompt layout is only implemented for Qwen3.5 checkpoints, not {kind}")
    if kind == EncoderDecisionModel.architecture:
        return EncoderDecisionModel(checkpoint=checkpoint, **kwargs)
    if kind == GenericDecoderDecisionModel.architecture:
        return GenericDecoderDecisionModel(checkpoint=checkpoint, **kwargs)
    return DecisionModel(checkpoint=checkpoint, prompt_layout=layout, **kwargs)
