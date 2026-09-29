from pathlib import Path

import pytest
import torch
from transformers import AutoTokenizer, Gemma3ForCausalLM, Gemma3TextConfig, Phi3Config, Phi3ForCausalLM

from jeff import decoder
from jeff.decoder import DECODER_MODELS, GenericDecoderDecisionModel
from jeff.models import architecture, load_decision_model

BASE = "google/gemma-3-270m-it"
REVISION = DECODER_MODELS[BASE][0]
ROWS = [
    {"state": "The parcel never arrived.", "question": {"type": "choice", "instructions": "Route the message.",
                                                        "criteria": {"billing": "Charges.", "delivery": "Shipping.", "other": None}}},
    {"state": "The sky is green.", "question": {"type": "noul", "instructions": "Is the statement true?"}},
]


@pytest.fixture(scope="module")
def tiny_base(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A tiny random Gemma 3 text model with the real Gemma tokenizer and chat template (CPU, seconds)."""
    path = tmp_path_factory.mktemp("tiny-gemma")
    tokenizer = AutoTokenizer.from_pretrained(BASE, revision=REVISION)
    config = Gemma3TextConfig(vocab_size=len(tokenizer), hidden_size=32, intermediate_size=64, num_hidden_layers=2,
                              num_attention_heads=2, num_key_value_heads=1, head_dim=16, pad_token_id=tokenizer.pad_token_id)
    config.architectures = ["Gemma3ForCausalLM"]
    torch.manual_seed(0)
    Gemma3ForCausalLM(config).save_pretrained(path)
    tokenizer.save_pretrained(path)
    return path


def model(base: Path, train: bool = False) -> GenericDecoderDecisionModel:
    return GenericDecoderDecisionModel(base_model=str(base), revision=REVISION, device="cpu", train=train)


def test_logits_in_the_decision_layout_and_readout_from_output_embeddings(tiny_base: Path) -> None:
    m = model(tiny_base)
    assert m.codes[:3] == ["A", "B", "C"] and len(m.codes) == 255
    logits = m(m.prepare(ROWS))
    assert logits.shape == (2, 255)
    assert (logits[0, 3:] == -1e9).all() and (logits[1, 2:] == -1e9).all()
    probabilities = m.predict(ROWS)
    assert [len(p) for p in probabilities] == [3, 2] and all(abs(sum(p) - 1) < 1e-5 for p in probabilities)


def test_listed_fragments_are_frozen_for_training(tiny_base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(decoder.DECODER_MODELS, str(tiny_base), (REVISION, ("embed_tokens",)))
    m = model(tiny_base, train=True)
    frozen = [n for n, p in m.backbone.named_parameters() if not p.requires_grad]
    assert frozen and all("embed_tokens" in n for n in frozen)
    assert m.readout.weight.requires_grad


def test_save_load_round_trip_through_the_factory(tiny_base: Path, tmp_path: Path) -> None:
    m = model(tiny_base)
    before = m.predict(ROWS, temperature=1.0)
    m.save(tmp_path / "ckpt", temperature=1.3, step=1)
    assert architecture(tmp_path / "ckpt", None) == "decoder-generic"
    loaded = load_decision_model(checkpoint=tmp_path / "ckpt", device="cpu")
    assert isinstance(loaded, GenericDecoderDecisionModel) and loaded.temperature == 1.3
    after = loaded.predict(ROWS, temperature=1.0)
    assert all(abs(a - b) < 1e-5 for pa, pb in zip(before, after) for a, b in zip(pa, pb))


def test_factory_routes_gemma_and_phi_names_and_keeps_qwen() -> None:
    assert architecture(None, "google/gemma-4-E2B-it") == "decoder-generic"
    assert architecture(None, "microsoft/Phi-4-mini-instruct") == "decoder-generic"
    assert architecture(None, "Qwen/Qwen3.5-0.8B") == "qwen"


def test_phi4_mini_tokenizer_and_chat_template_give_a_readout(tmp_path: Path) -> None:
    """A tiny random Phi-3 architecture model with the real Phi-4-mini tokenizer and chat template."""
    base = "microsoft/Phi-4-mini-instruct"
    revision = DECODER_MODELS[base][0]
    tokenizer = AutoTokenizer.from_pretrained(base, revision=revision)
    config = Phi3Config(vocab_size=len(tokenizer), hidden_size=32, intermediate_size=64, num_hidden_layers=2,
                        num_attention_heads=2, num_key_value_heads=1, pad_token_id=tokenizer.pad_token_id,
                        tie_word_embeddings=True)
    config.architectures = ["Phi3ForCausalLM"]
    torch.manual_seed(0)
    Phi3ForCausalLM(config).save_pretrained(tmp_path)
    tokenizer.save_pretrained(tmp_path)
    m = GenericDecoderDecisionModel(base_model=str(tmp_path), revision=revision, device="cpu")
    assert m.codes[:3] == ["A", "B", "C"] and len(m.codes) == 255
    probabilities = m.predict(ROWS)
    assert [len(p) for p in probabilities] == [3, 2] and all(abs(sum(p) - 1) < 1e-5 for p in probabilities)
