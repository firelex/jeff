import json
from pathlib import Path

import pytest
from huggingface_hub import snapshot_download

from jeff.errors import InputTooLong
from jeff_runtime import LocalEngine, select_backend

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def engine() -> LocalEngine:
    base = Path(snapshot_download("mstrasser/jeff-base", revision="v1.3"))
    triage = Path(snapshot_download("mstrasser/jeff-adapter-triage", revision="v1.3"))
    # the fast backend (auto on an NVIDIA GPU) needs room for its adapters and prefixes; the others refuse them
    sized = {"max_adapters": 1, "prefix_pool_mb": 2048} if select_backend("auto") == "fast" else {}
    return LocalEngine(base, {"jeff.support.triage": triage}, backend="auto", prepare_workers=1, **sized)


def test_example_request_answers_deliveries(engine: LocalEngine) -> None:
    triage = Path(snapshot_download("mstrasser/jeff-adapter-triage", revision="v1.3"))
    example = json.loads((triage / "example.json").read_text())
    row = {"state": example["state"], "question": example["questions"]["route"]}
    result = engine.predict("jeff.support.triage", [row])
    probabilities = result.probabilities[0]
    assert len(probabilities) == 5 and abs(sum(probabilities) - 1) < 1e-4
    assert probabilities.index(max(probabilities)) == 1  # k1: Deliveries (crushed wardrobe)
    assert result.timing["path"] in {"mlx", "standard", "fast"} and result.timing["model_ms"] > 0


def test_installed(engine: LocalEngine) -> None:
    assert engine.installed() == {"base", "jeff.support.triage"}  # the base is always served, as "base"


def test_the_base_answers_with_its_own_option_limit_and_counts_tokens(engine: LocalEngine) -> None:
    from jeff.errors import TooManyLabels

    row = {"state": "My parcel arrived crushed.",
           "question": {"type": "choice", "instructions": "Which team?",
                        "criteria": {"refunds": "Refunds", "parcels": "Damaged or lost parcels"}}}
    result = engine.predict("base", [row, row])
    assert len(result.probabilities) == 2 and abs(sum(result.probabilities[0]) - 1) < 1e-4
    assert result.input_tokens is not None and result.input_tokens[0] == result.input_tokens[1] > 10
    too_many = {"state": "s", "question": {"type": "choice", "criteria": {f"o{i}": None for i in range(255)}}}
    with pytest.raises(TooManyLabels, match="at most 254"):  # the v1.3 base's max_options
        engine.check("base", [too_many])
    with pytest.raises(TooManyLabels, match="at most 252"):  # the triage adapter's
        engine.check("jeff.support.triage", [{**too_many, "question": {
            "type": "choice", "criteria": {f"o{i}": None for i in range(253)}}}])


def test_too_long_input_is_refused(engine: LocalEngine) -> None:
    row = {"state": {"company": "c", "channel": "e", "message": "word " * 20000},
           "question": {"type": "noul", "instructions": "Is this long?"}}
    with pytest.raises(InputTooLong, match="no input was truncated"):
        engine.predict("jeff.support.triage", [row])


def test_torch_backend_agrees_with_mlx_and_refuses_long_input() -> None:
    """The non-mlx standard path (DecisionModel.prepare/forward in LocalEngine._standard), run alone since it loads
    the model again on CPU/MPS."""
    base = Path(snapshot_download("mstrasser/jeff-base", revision="v1.3"))
    triage = Path(snapshot_download("mstrasser/jeff-adapter-triage", revision="v1.3"))
    engine = LocalEngine(base, {"jeff.support.triage": triage}, backend="torch", prepare_workers=1)
    example = json.loads((triage / "example.json").read_text())
    row = {"state": example["state"], "question": example["questions"]["route"]}
    result = engine.predict("jeff.support.triage", [row])
    probabilities = result.probabilities[0]
    assert len(probabilities) == 5 and abs(sum(probabilities) - 1) < 1e-4
    assert probabilities.index(max(probabilities)) == 1  # k1: Deliveries (crushed wardrobe), as the mlx backend
    assert result.timing["path"] == "standard" and result.timing["model_ms"] > 0
    long_row = {"state": {"company": "c", "channel": "e", "message": "word " * 20000},
                "question": {"type": "noul", "instructions": "Is this long?"}}
    with pytest.raises(InputTooLong, match="no input was truncated"):
        engine.predict("jeff.support.triage", [long_row])


def test_check_refuses_too_long_without_running_the_model(engine: LocalEngine) -> None:
    row = {"state": {"company": "c", "channel": "e", "message": "word " * 20000},
           "question": {"type": "noul", "instructions": "Is this long?"}}
    with pytest.raises(InputTooLong, match="no input was truncated"):
        engine.check("jeff.support.triage", [row])


def test_check_has_its_own_processor(engine: LocalEngine) -> None:
    with engine._preparers.borrow() as processor:  # never the one the model uses on the GPU thread
        assert processor is not engine.model.processor
    triage = Path(snapshot_download("mstrasser/jeff-adapter-triage", revision="v1.3"))
    example = json.loads((triage / "example.json").read_text())
    engine.check("jeff.support.triage", [{"state": example["state"], "question": example["questions"]["route"]}])


def test_adapters_can_be_removed_and_added_back(engine: LocalEngine) -> None:
    triage = Path(snapshot_download("mstrasser/jeff-adapter-triage", revision="v1.3"))
    engine.remove_adapter("jeff.support.triage")
    assert engine.installed() == {"base"}
    engine.add_adapter("jeff.support.triage", triage)
    assert engine.installed() == {"base", "jeff.support.triage"}


@pytest.mark.cuda
def test_fast_path_agrees_with_standard_path() -> None:
    import torch
    if not torch.cuda.is_available():
        pytest.skip("needs an NVIDIA GPU; run on a CUDA machine with the owner's approval")
    base = Path(snapshot_download("mstrasser/jeff-base", revision="v1.3"))
    triage = Path(snapshot_download("mstrasser/jeff-adapter-triage", revision="v1.3"))
    rows = [json.loads(line) for line in (triage / "test.jsonl").read_text().splitlines()[:200]]
    rows = [{"state": row["state"], "question": row["question"]} for row in rows]
    fast = LocalEngine(base, {"jeff.support.triage": triage}, backend="fast", prepare_workers=1, max_adapters=1,
                       prefix_pool_mb=2048).predict(
        "jeff.support.triage", rows)
    standard = LocalEngine(base, {"jeff.support.triage": triage}, backend="torch", prepare_workers=1).predict("jeff.support.triage", rows)
    agree = sum(a.index(max(a)) == b.index(max(b)) for a, b in zip(fast.probabilities, standard.probabilities))
    assert agree / len(rows) >= 0.99
