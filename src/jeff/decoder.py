"""A text-only decision model for any chat decoder that transformers can load (used here for Gemma 3, Gemma 4 and
Phi-4-mini).

It mirrors the Qwen DecisionModel: the same decision prompt through the model's own chat template, and a readout over
single-token answer codes initialised from the model's output embedding rows, so training, calibration, evaluation
and serving are unchanged. Per-layer embedding tables (Gemma 4 "E" models) can be frozen to fit optimizer memory."""

import itertools
import json
import math
import string
from collections.abc import Sequence
from pathlib import Path

import torch
from safetensors.torch import load_file, save_file
from transformers import AutoConfig, AutoModel, AutoModelForCausalLM, AutoModelForImageTextToText, AutoTokenizer

from jeff.model import MAX_OPTIONS, PreparedBatch, decision_messages, options
from jeff.types import DecisionInput, JSONValue

# Base models this class serves, pinned; value = parameter-name fragments to freeze (empty = train everything).
DECODER_MODELS: dict[str, tuple[str, tuple[str, ...]]] = {
    "google/gemma-3-270m-it": ("ac82b4e820549b854eebf28ce6dedaf9fdfa17b3", ()),
    "google/gemma-4-E2B-it": ("3e22461f65e89153144f8adb70e3b8c2cc9845a7", ("embed_tokens_per_layer",)),
    "microsoft/Phi-4-mini-instruct": ("cfbefacb99257ffa30c83adab238a50856ac3083", ()),
}


class GenericDecoderDecisionModel(torch.nn.Module):
    architecture = "decoder-generic"

    def __init__(
        self, checkpoint: str | Path | None = None, train: bool = False, device: str | None = None,
        *, base_model: str | None = None, revision: str | None = None, gradient_checkpointing: bool = False,
        cpu_threads: int = 8, cache_dir: str | Path | None = None,
    ) -> None:
        super().__init__()
        torch.set_num_threads(cpu_threads)
        self.device_name = device or ("cuda" if torch.cuda.is_available() else "cpu")
        saved: dict[str, JSONValue] | None = None
        if checkpoint is not None:
            saved = json.loads((Path(checkpoint) / "decision_config.json").read_text())
            if saved["format_version"] != 1 or saved.get("architecture") != self.architecture:
                raise ValueError(f"{checkpoint} is not a generic decoder decision checkpoint")
        elif base_model is None or revision is None:
            raise ValueError("A generic decoder needs a checkpoint, or a base_model and revision")
        self.base_model = str(saved["base_model"]) if saved else str(base_model)
        self.revision = str(saved["revision"]) if saved else str(revision)
        if len(self.revision) != 40 or any(character not in string.hexdigits for character in self.revision):
            raise ValueError("Use an immutable, 40-character model revision.")
        cache = str(cache_dir) if cache_dir else None
        local_base = Path(self.base_model).is_dir()
        source, pinned = (str(checkpoint), None) if checkpoint else (self.base_model, None if local_base else self.revision)
        self.tokenizer = AutoTokenizer.from_pretrained(source, revision=pinned, cache_dir=cache)
        self.tokenizer.padding_side = "left"
        if not self.tokenizer.chat_template:
            raise ValueError(f"{self.base_model} has no chat template; use its instruction-tuned variant")
        self.codes, self.token_ids = self.answer_codes()
        dtype = torch.bfloat16 if self.device_name.startswith(("cuda", "mps")) else torch.float32  # half precision on GPUs
        if checkpoint is None:
            architectures = AutoConfig.from_pretrained(source, revision=pinned, cache_dir=cache).architectures or []
            loader = AutoModelForImageTextToText if any("ConditionalGeneration" in a for a in architectures) else AutoModelForCausalLM
            full = loader.from_pretrained(source, revision=pinned, cache_dir=cache, dtype=dtype, attn_implementation="sdpa")
            text = getattr(full.model, "language_model", full.model)  # multimodal models keep the text decoder inside
            hidden = text.config.hidden_size
            self.readout = torch.nn.Linear(hidden, MAX_OPTIONS, bias=False, dtype=dtype)
            with torch.no_grad():
                self.readout.weight.copy_(full.get_output_embeddings().weight[self.token_ids])
            self.backbone = text
            del full  # vision and audio towers are not used for text decisions
        else:
            self.backbone = AutoModel.from_pretrained(str(checkpoint), dtype=dtype, attn_implementation="sdpa")
            self.readout = torch.nn.Linear(self.backbone.config.hidden_size, MAX_OPTIONS, bias=False, dtype=dtype)
            self.readout.load_state_dict(load_file(str(Path(checkpoint) / "readout.safetensors")))
            if saved and (saved["codes"] != self.codes or saved["token_ids"] != self.token_ids):
                raise ValueError("Checkpoint answer vocabulary differs from its tokenizer.")
        self.requires_grad_(train)
        if train:
            frozen = DECODER_MODELS.get(self.base_model, ("", ()))[1]
            for name, parameter in self.backbone.named_parameters():
                if any(fragment in name for fragment in frozen):
                    parameter.requires_grad_(False)
            if gradient_checkpointing:
                self.backbone.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
        self.to(self.device_name)
        self.temperature = float(saved["temperature"]) if saved else 1.0  # type: ignore[arg-type]
        if not math.isfinite(self.temperature) or self.temperature <= 0:
            raise ValueError("Temperature must be positive and finite.")
        self.train(train)

    def answer_codes(self) -> tuple[list[str], list[int]]:
        """The first 255 codes (A..Z, then AA, AB, ...) that are single tokens, also right after the chat prefix."""
        tokenizer = self.tokenizer
        candidates = list(string.ascii_uppercase) + ["".join(pair) for pair in itertools.product(string.ascii_uppercase, repeat=2)]
        codes = [code for code in candidates if len(tokenizer.encode(code, add_special_tokens=False)) == 1][:MAX_OPTIONS]
        token_ids = [tokenizer.encode(code, add_special_tokens=False)[0] for code in codes]
        if len(set(token_ids)) != MAX_OPTIONS:
            raise ValueError("Tokenizer must provide 255 distinct single-token answer codes.")
        prefix = self.chat_text([{"role": "user", "content": "Choose an option."}])
        prefix_ids = tokenizer.encode(prefix, add_special_tokens=False)
        if any(tokenizer.encode(prefix + code, add_special_tokens=False) != prefix_ids + [token_id]
               for code, token_id in zip(codes, token_ids)):
            raise ValueError("Answer codes must remain single tokens after the chat prefix.")
        return codes, token_ids

    def chat_text(self, messages: list[dict[str, object]]) -> str:
        return str(self.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True))

    def prepare(self, rows: Sequence[DecisionInput], max_length: int = 8192) -> PreparedBatch:
        if not rows:
            raise ValueError("A batch must contain at least one decision.")
        texts, counts = [], []
        for row in rows:
            if row.get("images"):
                raise ValueError("The generic decoder decision model reads text only")
            counts.append(len(options(row["question"])[0]))
            messages = decision_messages(row, self.codes)
            user = messages[1]["content"]
            text_only = [{"role": "system", "content": messages[0]["content"]},
                         {"role": "user", "content": user[-1]["text"] if isinstance(user, list) else user}]  # type: ignore[index]
            texts.append(self.chat_text(text_only))
        encoded = self.tokenizer(texts, padding=True, return_tensors="pt", add_special_tokens=False)
        if encoded["input_ids"].shape[1] > max_length:
            raise ValueError(f"Question branch exceeds the {max_length}-token limit; no input was truncated.")
        inputs = {name: tensor.to(self.device_name) for name, tensor in encoded.items() if name in ("input_ids", "attention_mask")}
        return PreparedBatch(inputs, tuple(counts), int(encoded["attention_mask"].sum()))

    def forward(self, batch: PreparedBatch) -> torch.Tensor:
        hidden: torch.Tensor = self.backbone(**batch.inputs, use_cache=False).last_hidden_state[:, -1]
        logits: torch.Tensor = self.readout(hidden).float()
        mask = torch.arange(MAX_OPTIONS, device=logits.device)[None] >= torch.tensor(batch.counts, device=logits.device)[:, None]
        return logits.masked_fill(mask, -1e9)

    @torch.inference_mode()
    def predict(self, rows: Sequence[DecisionInput], batch_size: int = 8, temperature: float | None = None) -> list[list[float]]:
        scale = self.temperature if temperature is None else temperature
        if not math.isfinite(scale) or scale <= 0 or batch_size < 1:
            raise ValueError("Temperature and batch size must be positive.")
        was_training = self.training
        self.eval()
        distributions: list[list[float]] = []
        try:
            for start in range(0, len(rows), batch_size):
                batch = self.prepare(rows[start:start + batch_size])
                probabilities: list[list[float]] = (self(batch) / scale).softmax(-1).cpu().tolist()
                distributions.extend(values[:count] for values, count in zip(probabilities, batch.counts))
        finally:
            self.train(was_training)
        return distributions

    def save(self, directory: str | Path, temperature: float | None = None, **metadata: JSONValue) -> None:
        if hasattr(self.backbone, "peft_config") or getattr(self, "merged_adapter", None) is not None \
                or any(type(module).__name__ == "LoraLinear" for module in self.backbone.modules()):  # not a plain base
            raise ValueError("This model has LoRA adapters; save a trained adapter with jeff.lora.save_adapter")
        destination = Path(directory)
        if destination.exists() and any(destination.iterdir()):
            raise FileExistsError(f"Refusing to overwrite checkpoint contents: {destination}")
        scale = self.temperature if temperature is None else temperature
        if not math.isfinite(scale) or scale <= 0:
            raise ValueError("Temperature must be positive and finite.")
        destination.mkdir(parents=True, exist_ok=True)
        self.backbone.save_pretrained(str(destination), max_shard_size="5GB")
        self.tokenizer.save_pretrained(str(destination))
        save_file({"weight": self.readout.weight.detach().cpu().contiguous()}, str(destination / "readout.safetensors"))
        config: dict[str, JSONValue] = dict(metadata)
        config.update({"format_version": 1, "architecture": self.architecture, "base_model": self.base_model,
                       "revision": self.revision, "codes": list(self.codes), "token_ids": list(self.token_ids), "temperature": scale})
        (destination / "decision_config.json").write_text(json.dumps(config, indent=2) + "\n")
