"""The INT4 quantiser on the v1.3 base (plan 2b-2, Task 2), on datigator's GPU.

- Two layers, twice: quantising the first two decoder layers of the v1.3 base on the nine adapters' calibration rows
  (64 each, the published rule) gives identical bytes both times, and the integers and scales of the prototype that
  measured the design (~/jeff-2b-scratch/2b2/results/q_gptq.pt) for those layers: layer i's calibration depends only
  on layers 0..i, so the first two layers are already the full run's. About two minutes.
- The full run (marked slow too: about 12 minutes): `jeff runtime quantize --method gptq` on the nine v1.3 adapters
  reproduces q_gptq.pt's integers and scales for all 186 matrices, so its int4.safetensors is byte for byte the one
  Task 1 built from q_gptq.pt (~/jeff-2b-scratch/2b2/int4-base/ll-v12-final-int4-gptq), with the same format_id, and
  its record lists the prototype's calibration rows.

Adapter weights come from datigator's v1.3 checkpoints (--path). The full run reads the calibration rows from the data
the manifests name (Hugging Face downloads); the two-layer test reads datigator's copies of the same files
(/mnt/d/jeff-models/v1.3/casdgx01/testsets, the same sha256). Run on datigator, writing only under ~/jeff-2b-scratch:

  HF_HOME=~/jeff-2b-scratch/hf-home .venv/bin/python -m pytest -m cuda runtime/python/tests/test_quantize_cuda.py \\
      -s --basetemp ~/jeff-2b-scratch/2b2/task2/pytest            # add -m 'cuda and slow' for the full run"""

from __future__ import annotations

import gc
import glob
import json
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.cuda

REPO = Path(__file__).resolve().parents[3]
BASE = Path("/mnt/d/jeff-models/v1.3/v13base/ll-v12-final")
QUANT = Path("~/jeff-2b-scratch/2b2/results/q_gptq.pt").expanduser()
TASK1_INT4_BASE = Path("~/jeff-2b-scratch/2b2/int4-base/ll-v12-final-int4-gptq").expanduser()
TEST_SETS = Path("/mnt/d/jeff-models/v1.3/casdgx01/testsets")
CHECKPOINTS = "/mnt/d/jeff-models/v1.3/v13adapters/checkpoints/0.8b-{name}-*/final"
ADAPTERS = ("emotion", "ground", "guard", "legal-clauses", "nav", "spam", "support-intents", "tools", "triage")
ROWS_PER_ADAPTER = 64


def needs_datigator() -> dict[str, Path]:
    """The nine v1.3 adapters' checkpoint folders, by alias; skips when this is not datigator."""
    import torch
    if not torch.cuda.is_available():
        pytest.skip("needs an NVIDIA GPU (torch.cuda.is_available() is False)")
    missing = [str(path) for path in (BASE, QUANT, TASK1_INT4_BASE, TEST_SETS) if not path.exists()]
    if missing:
        pytest.skip(f"needs datigator's v1.3 base, the prototype output, Task 1's INT4 folder and the test sets; "
                    f"missing here: {missing}")
    found = {}
    for name in ADAPTERS:
        matches = glob.glob(CHECKPOINTS.format(name=name))
        assert len(matches) == 1, f"expected one v1.3 {name} checkpoint, found {matches}"
        found[name] = Path(matches[0])
    return found


@pytest.fixture(scope="module")
def prototype() -> dict[str, Any]:
    import torch
    needs_datigator()
    return torch.load(QUANT, map_location="cpu", weights_only=False)


def assert_same_as_prototype(matrices: dict[str, Any], prototype: dict[str, Any]) -> None:
    import torch
    for name, (q, s) in matrices.items():
        theirs_q, theirs_s = prototype["quant"][name]
        assert q.dtype == theirs_q.dtype == torch.int8 and s.dtype == theirs_s.dtype == torch.bfloat16, name
        assert torch.equal(q, theirs_q), f"{name}: {int((q != theirs_q).sum())} integers differ"
        assert torch.equal(s, theirs_s), f"{name}: {int((s != theirs_s).sum())} scales differ"


def test_quantising_two_layers_twice_gives_identical_bytes_and_the_prototypes(prototype: dict[str, Any]) -> None:
    import torch

    from jeff.catalogue import Catalogue
    from jeff_runtime.engine.int4 import pack_int4
    from jeff_runtime.engine.quantize import quantize_gptq, quantiser_rows, short_name

    checkpoints = needs_datigator()
    catalogue = Catalogue.from_dir(REPO / "catalogue")
    ids = sorted((catalogue.adapter(name).id for name in ADAPTERS), key=short_name)
    rows: list[tuple[str, dict[str, Any]]] = []
    for adapter_id in ids:
        lines = (TEST_SETS / short_name(adapter_id) / "calibration.jsonl").read_text().splitlines()
        drawn = quantiser_rows(short_name(adapter_id), [json.loads(line) for line in lines if line.strip()],
                               ROWS_PER_ADAPTER)
        assert [row["id"] for row in drawn] == prototype["meta"]["calibration_row_ids"][short_name(adapter_id)]
        rows.extend((adapter_id, {"state": row["state"], "question": row["question"]}) for row in drawn)
    folders = {adapter_id: checkpoints[short_name(adapter_id)] for adapter_id in ids}

    runs = []
    for _ in range(2):
        runs.append(quantize_gptq(BASE, folders, rows, layers=2))
        gc.collect()
        torch.cuda.empty_cache()
    one, two = runs
    assert sorted(one) == sorted(name for name in prototype["quant"]
                                 if name.startswith(("language_model.layers.0.", "language_model.layers.1.")))
    assert len(one) == 16
    for name in one:
        (q1, s1), (q2, s2) = one[name], two[name]
        assert pack_int4(q1).numpy().tobytes() == pack_int4(q2).numpy().tobytes(), name
        assert s1.view(torch.int16).numpy().tobytes() == s2.view(torch.int16).numpy().tobytes(), name
    assert_same_as_prototype(one, prototype)


@pytest.mark.slow
def test_the_full_v13_run_reproduces_the_prototype(tmp_path: Path, prototype: dict[str, Any]) -> None:
    from safetensors.torch import load_file

    from jeff.catalogue import Catalogue
    from jeff.cli import main
    from jeff_runtime.engine.int4 import read_quantization, unpack_int4

    checkpoints = needs_datigator()
    catalogue = Catalogue.from_dir(REPO / "catalogue")
    out = tmp_path / "ll-v12-final-int4-gptq"
    arguments = ["runtime", "quantize", "--base", str(BASE), "--method", "gptq", "--rows-per-adapter",
                 str(ROWS_PER_ADAPTER), "--out", str(out)]
    for name, folder in checkpoints.items():
        arguments += ["--calibrate-on", name, "--path", f"{name}={folder}"]
    assert main(arguments) == 0

    packed = load_file(str(out / "int4.safetensors"))
    matrices = {name: (unpack_int4(packed[f"{name}.qweight"]), packed[f"{name}.scales"])
                for name in prototype["quant"]}
    assert len(packed) == 2 * 186
    assert_same_as_prototype(matrices, prototype)

    ours, task1 = read_quantization(out), read_quantization(TASK1_INT4_BASE)
    assert ours is not None and task1 is not None
    assert ours.format_id == task1.format_id
    assert (out / "int4.safetensors").read_bytes() == (TASK1_INT4_BASE / "int4.safetensors").read_bytes()
    assert ours.record["calibration"]["rows"] == {
        f"{catalogue.adapter(name).id}@{catalogue.adapter(name).version}": ids
        for name, ids in prototype["meta"]["calibration_row_ids"].items()}
    assert ours.record["scale_exponent_spans"] == task1.record["scale_exponent_spans"]
    print(f"\n{ours.format_id}: 186 matrices equal to the prototype's, {ours.record['tool']['seconds']} s")
