"""The INT4 quantiser (engine/quantize.py) and `jeff runtime quantize` (step 2b-2 design, Sections 1 and 4), on the
Mac: GPTQ on a random 128 x 256 matrix against an independent NumPy reference, the drawing of the calibration rows,
and the command up to the GPU work (its refusals, and the folder and record it writes around the quantiser, which is
replaced here by round to nearest). The quantiser on the real model is tested on CUDA (test_quantize_cuda.py)."""

from __future__ import annotations

import hashlib
import json
import random
import re
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import torch
import yaml
from safetensors.torch import load_file

import jeff_runtime.engine.quantize as quantize
from jeff.cli import main
from jeff.errors import EngineError
from jeff_runtime.engine.int4 import read_quantization, unpack_int4
from jeff_runtime.engine.quantize import (CLIPS, check_not_calibration_rows, check_not_test_rows, gptq, group_scales,
                                          quantiser_rows, training_rows)
from jeff_runtime.engine.quantize import round_to_nearest
from test_int4 import matrices, source, write  # noqa: F401  (fixtures and helpers)

REPO = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------- GPTQ against a NumPy reference


def bf16(x: np.ndarray) -> np.ndarray:
    """float32 -> the nearest bfloat16 (ties to even), as float32."""
    bits = x.astype(np.float32).view(np.uint32)
    rounded = (bits + np.uint32(0x7FFF) + ((bits >> 16) & np.uint32(1))) & np.uint32(0xFFFF0000)
    return rounded.view(np.float32)


def reference_scales(w: np.ndarray) -> np.ndarray:
    groups = w.reshape(w.shape[0], -1, 64).astype(np.float32)
    top = np.abs(groups).max(-1)
    best, best_err = bf16(np.where(top == 0, 1, top / np.float32(7))), np.full(top.shape, np.inf, np.float32)
    for clip in CLIPS:
        s = bf16(np.where(top == 0, 1, top * np.float32(clip) / np.float32(7)))
        err = ((np.clip(np.round(groups / s[..., None]), -8, 7) * s[..., None] - groups) ** 2).sum(-1)
        best, best_err = np.where(err < best_err, s, best), np.minimum(err, best_err)
    return best


def reference_gptq(w: np.ndarray, h: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Textbook GPTQ in float64, one column at a time, every remaining column updated at once (no blocks): the
    inverse Hessian's upper Cholesky factor U, columns in order of decreasing diag(H), each column rounded with its
    group's fixed scale and its error spread over the later columns by U's row."""
    s = reference_scales(w).astype(np.float64)
    W, H = w.astype(np.float64).copy(), h.astype(np.float64).copy()
    dead = np.diag(H) == 0
    H[dead, dead] = 1
    W[:, dead] = 0
    H += 0.01 * np.mean(np.diag(H)) * np.eye(H.shape[0])
    order = np.argsort(-np.diag(H), kind="stable")
    W, H = W[:, order], H[order][:, order]
    U = np.linalg.cholesky(np.linalg.inv(H)).T
    q = np.zeros_like(W)
    for i in range(W.shape[1]):
        scale = s[:, order[i] // 64]
        q[:, i] = np.clip(np.round(W[:, i] / scale), -8, 7)
        err = (W[:, i] - q[:, i] * scale) / U[i, i]
        W[:, i:] -= np.outer(err, U[i, i:])
    integers = np.zeros_like(q)
    integers[:, order] = q
    return integers, s.astype(np.float32)


@pytest.fixture(scope="module")
def problem() -> tuple[torch.Tensor, torch.Tensor]:
    """A random 128 x 256 matrix and the Hessian of 512 random inputs whose columns differ in size, one input never
    active (a zero column: GPTQ gives its weights 0)."""
    generator = torch.Generator().manual_seed(0)
    w = (torch.randn(128, 256, generator=generator) * 0.05).to(torch.bfloat16).float()
    x = torch.randn(512, 256, generator=generator, dtype=torch.float64) * torch.linspace(0.1, 3, 256,
                                                                                         dtype=torch.float64)
    x[:, 17] = 0
    return w, x.T @ x


def test_gptq_matches_a_numpy_reference_on_a_random_matrix(problem: tuple[torch.Tensor, torch.Tensor]) -> None:
    w, h = problem
    integers, scales = gptq(w, h)
    assert integers.dtype == torch.int8 and scales.dtype == torch.bfloat16
    assert tuple(integers.shape) == (128, 256) and tuple(scales.shape) == (128, 4)
    expected_integers, expected_scales = reference_gptq(w.numpy(), h.numpy())
    assert np.array_equal(scales.float().numpy(), expected_scales)
    assert np.array_equal(integers.numpy().astype(np.float64), expected_integers)
    assert int(integers.min()) >= -8 and int(integers.max()) <= 7
    assert not integers[:, 17].any()


def test_gptq_beats_round_to_nearest_on_the_inputs_it_saw(problem: tuple[torch.Tensor, torch.Tensor]) -> None:
    """GPTQ's point: a smaller output error, sum over inputs of |x (W - Q)^T|^2 = trace((W - Q) H (W - Q)^T), than
    rounding each weight to nearest with the same scales."""
    w, h = problem
    integers, scales = gptq(w, h)
    s = group_scales(w)
    assert torch.equal(scales.float(), s)
    nearest = torch.clamp(torch.round(w.reshape(128, -1, 64) / s[..., None]), -8, 7)

    def error(q: torch.Tensor) -> float:
        d = (w.double() - (q.reshape(128, -1, 64) * scales.double()[..., None]).reshape(128, 256))
        return float(torch.trace(d @ h @ d.T))
    assert error(integers.double()) < 0.8 * error(nearest.double())


def test_group_scales_pick_the_clip_with_the_least_error() -> None:
    w = torch.zeros(2, 128)
    w[0, :64] = torch.linspace(-1, 1, 64)
    w[0, 3] = 4.0  # one outlier: clipping it costs less than spreading the integers over [-4, 4]
    scales = group_scales(w)
    assert float(scales[1, 0]) == 1.0 and float(scales[0, 1]) == 1.0  # all-zero groups get the scale 1

    def error(scale: float) -> float:
        q = torch.clamp(torch.round(w[0, :64] / scale), -8, 7)
        return float(((q * scale - w[0, :64]) ** 2).sum())
    candidates = [float(torch.tensor(4.0 * clip / 7).to(torch.bfloat16)) for clip in CLIPS]
    assert float(scales[0, 0]) == min(candidates, key=error) != candidates[0]


def test_round_to_nearest_is_the_prototypes_and_quantize_rtn_covers_the_layers_asked(
        source: Path, tmp_path: Path) -> None:  # noqa: F811
    """quantize.round_to_nearest against the prototype's rtn() (2b2-prototypes/quantize.py: group_scales without
    clip search, then round and clamp), computed independently in NumPy; quantize_rtn of the tiny base reads every
    INT4 matrix of the layers asked, from model.safetensors, the same bytes twice."""
    w = torch.randn(8, 128, generator=torch.Generator().manual_seed(3))
    w[1, 64:] = 0  # an all-zero group: scale 1
    integers, scales = quantize.round_to_nearest(w)
    groups = w.numpy().reshape(8, 2, 64)
    top = np.abs(groups).max(-1)
    expected_s = bf16(np.where(top == 0, 1.0, top / 7).astype(np.float32))
    expected_q = np.clip(np.round(groups / expected_s[..., None]), -8, 7).reshape(8, 128)
    assert np.array_equal(scales.float().numpy(), expected_s)
    assert np.array_equal(integers.numpy(), expected_q.astype(np.int8))
    assert integers.dtype == torch.int8 and scales.dtype == torch.bfloat16

    state = load_file(str(source / "model.safetensors"))
    first = quantize.quantize_rtn(source, layers=1)
    assert first and all(name.startswith("language_model.layers.0.") for name in first)
    everything = quantize.quantize_rtn(source)
    assert set(first) < set(everything) and any(name.startswith("language_model.layers.1.") for name in everything)
    for name, (q, s) in everything.items():
        expected = quantize.round_to_nearest(state[f"{name}.weight"])
        assert torch.equal(q, expected[0]) and torch.equal(s, expected[1]), name
    again = quantize.quantize_rtn(source)
    assert all(torch.equal(again[name][0], q) and torch.equal(again[name][1], s) for name, (q, s) in everything.items())
    with pytest.raises(EngineError, match="layers must be from 1 to 2"):
        quantize.quantize_rtn(source, layers=3)


# ---------------------------------------------------------------- the calibration rows


def calibration_rows(name: str, count: int) -> list[dict[str, Any]]:
    return [{"id": f"{name}:cal:{number}", "state": {"text": f"row {number}"},
             "question": {"type": "noul", "text": "Is it?"}} for number in range(count)]


def test_the_quantiser_rows_are_drawn_outside_the_temperature_rows() -> None:
    rows = calibration_rows("guard", 400)
    drawn = quantiser_rows("guard", rows, 64)
    temperature = {row["id"] for row in random.Random("20261001-guard-calibration").sample(rows, 300)}
    assert len(drawn) == 64 and len({row["id"] for row in drawn}) == 64
    assert not {row["id"] for row in drawn} & temperature
    assert drawn == quantiser_rows("guard", list(rows), 64)  # the same rows, in the same order, every time
    assert drawn != quantiser_rows("spam", rows, 64)  # each adapter its own draw


@pytest.mark.parametrize(("rows", "message"), [
    (calibration_rows("guard", 363), "only 63 calibration rows outside the 300 temperature rows; the quantiser needs 64"),
    (calibration_rows("guard", 400) + calibration_rows("guard", 1), "repeats row ids \\(guard:cal:0\\)"),
    ([{"id": "a", "state": {}}], "calibration row 1 has no question"),
])
def test_bad_calibration_sets_are_refused(rows: list[dict[str, Any]], message: str) -> None:
    with pytest.raises(EngineError, match=message):
        quantiser_rows("guard", rows, 64)


def test_drawn_rows_that_are_test_rows_are_refused() -> None:
    drawn = calibration_rows("guard", 3)
    others = [{**row, "id": f"other:{number}", "state": {"text": "something else"}}
              for number, row in enumerate(drawn)]
    check_not_test_rows("jeff.safety.guard", drawn, others)  # the same question alone is fine
    with pytest.raises(EngineError, match="jeff.safety.guard: calibration rows drawn for the quantiser are test rows: "
                                          "1 with an id the test set has \\(guard:cal:1\\)"):
        check_not_test_rows("jeff.safety.guard", drawn, [*others, {**others[0], "id": "guard:cal:1"}])
    with pytest.raises(EngineError, match="2 with the state of a test row \\(guard:cal:0, guard:cal:2\\)"):
        check_not_test_rows("jeff.safety.guard", drawn, [{**drawn[0], "id": "t0"}, {**drawn[2], "id": "t2"}])


def test_rows_with_the_same_state_are_the_same_input_whatever_the_question() -> None:
    drawn = [{"id": 7, "state": {"text": "the document", "meta": {"a": 1, "b": [1, 2]}},
              "question": {"type": "noul", "text": "Is it?"}},
             {"id": "s", "state": "a plain-text document", "question": {"type": "noul", "text": "Is it?"}}]
    # another question on the same document; keys in another order and other whitespace: the same state
    same_document = {"id": "t1", "state": json.loads('{ "meta" : {"b": [1,2], "a": 1}, "text": "the document" }'),
                     "question": {"type": "score", "text": "How much?"}}
    with pytest.raises(EngineError, match="1 with the state of a test row \\(7\\)"):
        check_not_test_rows("jeff.safety.guard", drawn, [same_document])
    with pytest.raises(EngineError, match="1 with the state of a calibration row \\(s\\)"):
        check_not_calibration_rows("jeff.safety.guard", drawn,
                                   [{"id": "c", "state": "a plain-text document", "question": {}}])
    # a string state is compared as it is: other whitespace is another document
    check_not_test_rows("jeff.safety.guard", drawn, [{"id": "t", "state": "a plain-text  document", "question": {}},
                                                     {"id": "u", "state": {"text": "the document"}, "question": {}}])
    # ids compare as strings: 7 and "7" are the same id
    with pytest.raises(EngineError, match="1 with an id the test set has \\(7\\)"):
        check_not_test_rows("jeff.safety.guard", drawn, [{"id": "7", "state": {"other": 1}, "question": {}}])


def test_training_rows_are_a_seeded_sample_of_the_whole_file() -> None:
    rows = calibration_rows("guard", 200)
    drawn = training_rows("guard", rows, 128, source="t.jsonl")
    assert len(drawn) == 128 and len({row["id"] for row in drawn}) == 128
    assert drawn == random.Random("quant-train-guard").sample(rows, 128)  # no temperature rows set aside
    assert drawn == training_rows("guard", list(rows), 128, source="t.jsonl")  # the same rows, same order, every time
    assert drawn != training_rows("spam", rows, 128, source="t.jsonl")  # each adapter its own draw
    assert len(training_rows("guard", calibration_rows("guard", 128), 128, source="t.jsonl")) == 128  # exactly enough


@pytest.mark.parametrize(("rows", "lines", "message"), [
    (calibration_rows("guard", 127), None, "guard: only 127 training rows in t.jsonl; the quantiser needs 128"),
    (calibration_rows("guard", 200) + calibration_rows("guard", 1), None,
     "t.jsonl repeats row id 'guard:cal:0' \\(lines 1 and 201\\)"),
    ([{"id": 5, "state": {}, "question": {}}, {"id": "5", "state": {}, "question": {}}], [2, 9],
     "t.jsonl repeats row id '5' \\(lines 2 and 9\\)"),  # ids compare as strings
    ([{"id": "a", "question": {}}], [4], "t.jsonl line 4 has no state"),
    ([["not", "an", "object"]], [3], "t.jsonl line 3 is not a JSON object \\(list\\)"),
    ([{"id": 1.5, "state": {}, "question": {}}], [6], "t.jsonl line 6 has an id of type float"),
    ([{"id": True, "state": {}, "question": {}}], None, "t.jsonl line 1 has an id of type bool"),
    ([{"id": None, "state": {}, "question": {}}], None, "t.jsonl line 1 has an id of type NoneType"),
])
def test_bad_training_files_are_refused(rows: list[Any], lines: list[int] | None, message: str) -> None:
    with pytest.raises(EngineError, match=message):
        training_rows("guard", rows, 128, source="t.jsonl", lines=lines)


def test_drawn_training_rows_that_are_calibration_rows_are_refused() -> None:
    drawn = calibration_rows("guard", 3)
    others = [{**row, "id": f"other:{number}", "state": {"text": "something else"}}
              for number, row in enumerate(drawn)]
    check_not_calibration_rows("jeff.safety.guard", drawn, others)
    with pytest.raises(EngineError, match="jeff.safety.guard: training rows drawn for the quantiser are calibration "
                                          "rows: 1 with an id the calibration set has \\(guard:cal:1\\)"):
        check_not_calibration_rows("jeff.safety.guard", drawn, [*others, {**others[0], "id": "guard:cal:1"}])
    with pytest.raises(EngineError, match="1 with the state of a calibration row \\(guard:cal:2\\)"):
        check_not_calibration_rows("jeff.safety.guard", drawn, [{**drawn[2], "id": "c2"}])


# ---------------------------------------------------------------- jeff runtime quantize, up to the GPU work


def held_out_rows(name: str, count: int) -> list[dict[str, Any]]:
    """Rows of a test set: their own ids and contents, none a calibration row's."""
    return [{"id": f"{name}:test:{number}", "state": {"text": f"test row {number}"},
             "question": {"type": "noul", "text": "Is it?"}} for number in range(count)]


def adapter_folder(root: Path, catalogue_name: str, rows: int, *, test: list[dict[str, Any]] | None = None,
                   folder_name: str | None = None, base_version: str | None = None,
                   test_is_calibration: bool = False) -> Path:
    """An adapter folder (as --adapters-dir finds one) with the catalogue's manifest, its calibration and test sets
    local files; its weights are never read here (the quantiser is replaced)."""
    manifest = yaml.safe_load((REPO / "catalogue" / catalogue_name / "manifest.yaml").read_text())
    name = manifest["id"].rsplit(".", 1)[1]
    folder = root / (folder_name or name)
    folder.mkdir()
    data, test_data = folder / "calibration.jsonl", folder / "test.jsonl"
    data.write_text("".join(json.dumps(row) + "\n" for row in calibration_rows(name, rows)))
    held_out = held_out_rows(name, 50) if test is None else test
    test_data.write_text("".join(json.dumps(row) + "\n" for row in held_out))
    manifest["tests"]["calibration"] = str(data)
    manifest["tests"]["published"] = str(data if test_is_calibration else test_data)
    if base_version is not None:
        manifest["base"]["version"] = base_version
    (folder / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    (folder / "adapter_config.json").write_text("{}")
    return folder


def run(*arguments: str) -> int:
    return main(["runtime", "quantize", "--method", "gptq", "--rows-per-adapter", "64", *arguments])


@pytest.fixture
def fake_gpu(monkeypatch: pytest.MonkeyPatch, source: Path) -> list[dict[str, Any]]:
    """The quantiser replaced by round to nearest of the source's matrices, recording what it was given; a GPU
    reported present."""
    calls: list[dict[str, Any]] = []

    def quantize_gptq(base: Path, adapters: dict[str, Path], rows: list[Any], **_: Any) -> dict[str, Any]:
        calls.append({"base": base, "adapters": dict(adapters), "rows": list(rows)})
        state = load_file(str(base / "model.safetensors"))
        return {name.removesuffix(".weight"): round_to_nearest(tensor) for name, tensor in state.items()
                if name.startswith("language_model.layers.") and tensor.dim() == 2}
    monkeypatch.setattr(quantize, "quantize_gptq", quantize_gptq)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "get_device_name", lambda: "a test GPU")
    monkeypatch.setattr(torch.cuda, "max_memory_allocated", lambda: 123)
    return calls


def test_quantize_writes_the_int4_base_and_records_the_rows(tmp_path: Path, source: Path,  # noqa: F811
                                                            fake_gpu: list[dict[str, Any]],
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    spam = adapter_folder(tmp_path, "jeff.safety.spam", 400)
    guard = adapter_folder(tmp_path, "jeff.safety.guard", 500)
    out = tmp_path / "int4"
    assert run("--base", str(source), "--calibrate-on", str(spam), "--calibrate-on", str(guard), "--out", str(out)) == 0
    assert "Wrote" in capsys.readouterr().out
    (call,) = fake_gpu
    assert call["adapters"] == {"jeff.safety.guard": guard, "jeff.safety.spam": spam}
    guard_rows = quantiser_rows("guard", calibration_rows("guard", 500), 64)
    spam_rows = quantiser_rows("spam", calibration_rows("spam", 400), 64)
    # adapters in order of their short name (guard before spam, whatever the command line's order), rows as drawn
    assert call["rows"] == [("jeff.safety.guard", {"state": row["state"], "question": row["question"]})
                            for row in guard_rows] + [
                           ("jeff.safety.spam", {"state": row["state"], "question": row["question"]})
                           for row in spam_rows]
    quantization = read_quantization(out)
    assert quantization is not None and quantization.format_id.startswith("jeff-base@v1.4+int4-g64-gptq.")
    record = quantization.record
    assert record["calibration"]["rows"] == {"jeff.safety.guard@1.4.0": [row["id"] for row in guard_rows],
                                             "jeff.safety.spam@1.4.0": [row["id"] for row in spam_rows]}
    assert record["calibration"]["data"] == {"jeff.safety.guard@1.4.0": str(guard / "calibration.jsonl"),
                                             "jeff.safety.spam@1.4.0": str(spam / "calibration.jsonl")}
    assert record["calibration"]["not_in_test_sets"] == {
        f"jeff.safety.{name}@1.4.0": {"data": str(folder / "test.jsonl"),
                                      "sha256": hashlib.sha256((folder / "test.jsonl").read_bytes()).hexdigest()}
        for name, folder in (("guard", guard), ("spam", spam))}
    assert record["calibration"]["rows_per_adapter"] == 64
    assert record["gptq"] == quantize.GPTQ_SETTINGS
    assert record["tool"]["gpu"] == "a test GPU" and record["tool"]["peak_gpu_memory_bytes"] == 123
    packed = load_file(str(out / "int4.safetensors"))
    q, _ = round_to_nearest(load_file(str(source / "model.safetensors"))["language_model.layers.0.mlp.up_proj.weight"])
    assert torch.equal(unpack_int4(packed["language_model.layers.0.mlp.up_proj.qweight"]), q)


def test_quantize_refuses_bad_settings_before_the_gpu_work(tmp_path: Path, source: Path,  # noqa: F811
                                                           matrices: dict[str, Any],  # noqa: F811
                                                           fake_gpu: list[dict[str, Any]],
                                                           capsys: pytest.CaptureFixture[str]) -> None:
    guard = str(adapter_folder(tmp_path, "jeff.safety.guard", 500))
    int4 = write(source, tmp_path / "int4", matrices)
    (tmp_path / "taken").mkdir()
    out = str(tmp_path / "out")
    test_with_their_ids = [{**row, "id": f"guard:cal:{number}"} for number, row in enumerate(held_out_rows("g", 500))]
    with_ids = adapter_folder(tmp_path, "jeff.safety.guard", 500, folder_name="ids", test=test_with_their_ids)
    with_content = adapter_folder(tmp_path, "jeff.safety.guard", 500, folder_name="content",
                                  test=[{**row, "id": f"t{number}"} for number, row in
                                        enumerate(calibration_rows("guard", 500))])
    cases = [
        (["--base", str(source), "--calibrate-on", guard, "--out", str(tmp_path / "taken")], "already exists"),
        (["--base", str(source), "--calibrate-on", guard, "--out", str(tmp_path / "no" / "out")],
         "its parent folder .* does not exist"),
        (["--base", str(source), "--calibrate-on", guard, "--out", str(source / "int4")],
         "is inside --base .*; write the INT4 base beside its source"),
        (["--base", str(int4), "--calibrate-on", guard, "--out", out], "is already an INT4 base"),
        (["--base", str(tmp_path), "--calibrate-on", guard, "--out", out], "is not a base folder"),
        (["--base", str(source), "--calibrate-on", guard, "--rows-per-adapter", "0", "--out", out],
         "--rows-per-adapter must be at least 1; got 0"),
        (["--base", str(source), "--calibrate-on", guard, "--calibrate-on", guard, "--out", out],
         "names jeff.safety.guard twice"),
        (["--base", str(source), "--calibrate-on", str(tmp_path / "taken"), "--out", out],
         "holds manifest.yaml and adapter_config.json; this one does not"),
        (["--base", str(source), "--calibrate-on", guard, "--path", "spam=/somewhere", "--out", out],
         "--path names jeff.safety.spam, which --calibrate-on does not"),
        (["--base", str(source), "--calibrate-on", guard, "--path", "guard=/somewhere", "--out", out],
         "--path names jeff.safety.guard, which --calibrate-on .* already gives a folder"),
        (["--base", str(source), "--calibrate-on", "guard", "--path", "guard=/a", "--path", "jeff.safety.guard=/b",
          "--out", out], "--path names jeff.safety.guard twice"),
        (["--base", str(source), "--calibrate-on", guard, "--calibrate-on",
          str(adapter_folder(tmp_path, "jeff.safety.spam", 400, base_version="v9.9", folder_name="other-base")),
          "--out", out],
         "The adapters are trained on different bases \\(jeff-base@v1.4, jeff-base@v9.9\\)"),
        (["--base", str(source), "--calibrate-on",
          str(adapter_folder(tmp_path, "jeff.safety.spam", 360, folder_name="few")), "--out", out],
         "spam: only 60 calibration rows outside the 300 temperature rows"),
        (["--base", str(source), "--calibrate-on", str(adapter_folder(tmp_path, "jeff.support.triage", 400,
                                                                      test_is_calibration=True)), "--out", out],
         "jeff.support.triage: its calibration set and its test set are the same file"),
        (["--base", str(source), "--calibrate-on", str(with_ids), "--out", out],
         "jeff.safety.guard: calibration rows drawn for the quantiser are test rows: 64 with an id the test set has "
         "\\(guard:cal:"),
        (["--base", str(source), "--calibrate-on", str(with_content), "--out", out],
         "jeff.safety.guard: calibration rows drawn for the quantiser are test rows: 64 with the state of a test "
         "row"),
    ]
    for arguments, message in cases:
        assert run(*arguments) == 1, arguments
        assert re.search(message, capsys.readouterr().err), (arguments, message)
    assert fake_gpu == [] and not (tmp_path / "out").exists()


def test_quantize_refuses_without_a_gpu_before_any_download(tmp_path: Path, source: Path,  # noqa: F811
                                                            monkeypatch: pytest.MonkeyPatch,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    import jeff.cli

    def must_not_run(*_: Any) -> None:
        raise AssertionError("downloaded an adapter without a GPU")
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(jeff.cli, "adapter_folder", must_not_run)
    assert run("--base", str(source), "--calibrate-on", "guard", "--out", str(tmp_path / "out")) == 1
    assert "jeff runtime quantize needs an NVIDIA GPU" in capsys.readouterr().err


def test_quantize_has_no_defaults_for_its_settings() -> None:
    for missing in ("--base", "--method", "--calibrate-on", "--rows-per-adapter", "--out"):
        arguments = {"--base": "b", "--method": "gptq", "--calibrate-on": "guard", "--rows-per-adapter": "64",
                     "--out": "o"}
        del arguments[missing]
        with pytest.raises(SystemExit):
            main(["runtime", "quantize", *[part for pair in arguments.items() for part in pair]])
    with pytest.raises(SystemExit):
        main(["runtime", "quantize", "--base", "b", "--method", "awq-gptq", "--calibrate-on", "guard",
              "--rows-per-adapter", "64", "--out", "o"])


# ---------------------------------------------------------------- --calibrate-on-rows: training rows per adapter


def training_file(folder: Path, name: str, count: int, *, extra: list[dict[str, Any]] | None = None) -> Path:
    """A training rows file beside an adapter folder: rows with their own ids and contents, plus `extra`."""
    rows = [{"id": f"{name}:train:{number}", "state": {"text": f"training row {number}"},
             "question": {"type": "noul", "text": "Is it?"}} for number in range(count)] + (extra or [])
    path = folder.parent / f"{folder.name}-train.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def test_quantize_draws_each_adapters_rows_from_its_training_file(tmp_path: Path, source: Path,  # noqa: F811
                                                                  fake_gpu: list[dict[str, Any]],
                                                                  capsys: pytest.CaptureFixture[str]) -> None:
    spam = adapter_folder(tmp_path, "jeff.safety.spam", 20)  # too few calibration rows to draw 64 from: not used
    guard = adapter_folder(tmp_path, "jeff.safety.guard", 500)
    spam_train, guard_train = training_file(spam, "spam", 100), training_file(guard, "guard", 64)
    out = tmp_path / "int4"
    assert run("--base", str(source), "--calibrate-on", str(spam), "--calibrate-on", str(guard),
               "--calibrate-on-rows", f"{spam}={spam_train}", "--calibrate-on-rows", f"guard={guard_train}",
               "--path", f"guard={guard}", "--out", str(out)) == 1  # guard is named by its folder, not --path
    assert "already gives a folder" in capsys.readouterr().err
    assert run("--base", str(source), "--calibrate-on", str(spam), "--calibrate-on", str(guard),
               "--calibrate-on-rows", f"{spam}={spam_train}", "--calibrate-on-rows", f"{guard}={guard_train}",
               "--out", str(out)) == 0
    assert "on 128 training rows (64 each from" in capsys.readouterr().out
    (call,) = fake_gpu

    def jsonl(path: Path) -> list[dict[str, Any]]:
        return [json.loads(line) for line in path.read_text().splitlines()]
    guard_rows = training_rows("guard", jsonl(guard_train), 64, source="g")
    spam_rows = training_rows("spam", jsonl(spam_train), 64, source="s")
    assert call["rows"] == [("jeff.safety.guard", {"state": row["state"], "question": row["question"]})
                            for row in guard_rows] + [
                           ("jeff.safety.spam", {"state": row["state"], "question": row["question"]})
                           for row in spam_rows]
    calibration = read_quantization(out).record["calibration"]
    keys = {"guard": "jeff.safety.guard@1.4.0", "spam": "jeff.safety.spam@1.4.0"}
    assert calibration["rule"] == quantize.TRAINING_RULE
    assert calibration["rows"] == {keys["guard"]: [row["id"] for row in guard_rows],
                                   keys["spam"]: [row["id"] for row in spam_rows]}
    assert calibration["data"] == {keys["guard"]: str(guard_train), keys["spam"]: str(spam_train)}

    def sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    assert calibration["training_rows"] == {keys[name]: {"path": str(path), "sha256": sha(path)}
                                            for name, path in (("guard", guard_train), ("spam", spam_train))}
    assert calibration["not_in_calibration_sets"] == {
        keys[name]: {"data": str(folder / "calibration.jsonl"), "sha256": sha(folder / "calibration.jsonl")}
        for name, folder in (("guard", guard), ("spam", spam))}
    assert calibration["not_in_test_sets"] == {
        keys[name]: {"data": str(folder / "test.jsonl"), "sha256": sha(folder / "test.jsonl")}
        for name, folder in (("guard", guard), ("spam", spam))}


def test_quantize_refuses_bad_training_rows_naming_the_adapter(tmp_path: Path, source: Path,  # noqa: F811
                                                               fake_gpu: list[dict[str, Any]],
                                                               capsys: pytest.CaptureFixture[str]) -> None:
    guard = adapter_folder(tmp_path, "jeff.safety.guard", 500)
    spam = adapter_folder(tmp_path, "jeff.safety.spam", 400)
    good = training_file(guard, "guard", 100)
    out = str(tmp_path / "out")
    # every training row is also a test row (by content), or a calibration row (by id, then by content)
    as_test = training_file(adapter_folder(tmp_path, "jeff.safety.guard", 500, folder_name="as-test"), "g", 0,
                            extra=[{**row, "id": f"x{number}"} for number, row in enumerate(held_out_rows("guard", 80))])
    by_id = training_file(adapter_folder(tmp_path, "jeff.safety.guard", 500, folder_name="by-id"), "g", 0,
                          extra=[{**row, "id": f"guard:cal:{number}", "state": {"text": f"new {number}"}}
                                 for number, row in enumerate(held_out_rows("x", 80))])
    by_content = training_file(adapter_folder(tmp_path, "jeff.safety.guard", 500, folder_name="by-content"), "g", 0,
                               extra=[{**row, "id": f"y{number}"} for number, row in
                                      enumerate(calibration_rows("guard", 80))])
    cases = [
        ([f"{guard}={tmp_path / 'missing.jsonl'}"], [guard],
         "jeff.safety.guard: its training rows file .*missing.jsonl does not exist"),
        ([f"{guard}={training_file(guard, 'guard', 63)}"], [guard],
         "jeff.safety.guard \\(.*\\): guard: only 63 training rows in .*; the quantiser needs 64"),
        ([f"{guard}={good}"], [guard, spam], "Training rows must be named for exactly the adapters calibrated on; "
                                             "none named for jeff.safety.spam"),
        ([f"{guard}={good}", f"spam={good}"], [guard], "named for jeff.safety.spam, which is not calibrated on"),
        ([f"{guard}={good}", f"guard={good}"], [guard], "--calibrate-on-rows names jeff.safety.guard twice"),
        ([f"{guard}={guard / 'calibration.jsonl'}"], [guard], "its training rows file .* is its calibration or test set"),
        ([f"{as_test.parent / 'as-test'}={as_test}"], [as_test.parent / "as-test"],
         "jeff.safety.guard: training rows drawn for the quantiser are test rows: \\d+ with the state of a test "
         "row .*fix the adapter's training rows file"),
        ([f"{by_id.parent / 'by-id'}={by_id}"], [by_id.parent / "by-id"],
         "jeff.safety.guard: training rows drawn for the quantiser are calibration rows: 64 with an id the "
         "calibration set has"),
        ([f"{by_content.parent / 'by-content'}={by_content}"], [by_content.parent / "by-content"],
         "training rows drawn for the quantiser are calibration rows: 64 with the state of a calibration row"),
    ]
    for pairs, adapters, message in cases:
        arguments = ["--base", str(source), *[part for folder in adapters for part in ("--calibrate-on", str(folder))],
                     *[part for pair in pairs for part in ("--calibrate-on-rows", pair)], "--out", out]
        assert run(*arguments) == 1, arguments
        assert re.search(message, capsys.readouterr().err), (arguments, message)
    with pytest.raises(SystemExit):
        main(["runtime", "quantize", "--base", "b", "--method", "gptq", "--calibrate-on", "guard",
              "--calibrate-on-rows", "guard", "--rows-per-adapter", "64", "--out", "o"])
    assert fake_gpu == [] and not (tmp_path / "out").exists()


def test_quantize_refuses_malformed_training_files_naming_the_file_and_line(
        tmp_path: Path, source: Path, fake_gpu: list[dict[str, Any]],  # noqa: F811
        capsys: pytest.CaptureFixture[str]) -> None:
    guard = adapter_folder(tmp_path, "jeff.safety.guard", 500)
    out = str(tmp_path / "out")

    def written(name: str, text: str) -> Path:
        path = tmp_path / name
        path.write_text(text)
        return path
    good = "".join(json.dumps({"id": f"r{n}", "state": {"text": f"train {n}"}, "question": {}}) + "\n"
                   for n in range(70))
    cases = [
        (written("repeat.jsonl", good + "\n" + json.dumps({"id": "r3", "state": {"text": "x"}, "question": {}})),
         "repeat.jsonl repeats row id 'r3' \\(lines 4 and 72\\)"),
        (written("no-state.jsonl", good + json.dumps({"id": "z", "question": {}}) + "\n"),
         "no-state.jsonl line 71 has no state"),
        (written("not-json.jsonl", good + "{oops\n"), "not-json.jsonl line 71 is not valid JSON"),
        (written("array.jsonl", "[1, 2]\n" + good), "array.jsonl line 1 is not a JSON object"),
        (guard / "test.jsonl", "its training rows file .*test.jsonl is its calibration or test set"),
    ]
    for path, message in cases:
        assert run("--base", str(source), "--calibrate-on", str(guard), "--calibrate-on-rows", f"{guard}={path}",
                   "--out", out) == 1, path
        assert re.search(message, capsys.readouterr().err), (path, message)
    assert fake_gpu == []


def test_quantize_records_the_training_files_resolved_path(tmp_path: Path, source: Path,  # noqa: F811
                                                           fake_gpu: list[dict[str, Any]],
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    guard = adapter_folder(tmp_path, "jeff.safety.guard", 500)
    folder = tmp_path / "data=v2"  # an '=' in the path: the option splits at the first '='
    folder.mkdir()
    train = folder / "train.jsonl"
    train.write_text("".join(json.dumps({"id": n, "state": {"text": f"train {n}"}, "question": {}}) + "\n"
                             for n in range(70)))
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "int4"
    assert run("--base", str(source), "--calibrate-on", str(guard), "--calibrate-on-rows",
               f"{guard}=data=v2/../data=v2/train.jsonl", "--out", str(out)) == 0
    calibration = read_quantization(out).record["calibration"]
    assert calibration["training_rows"]["jeff.safety.guard@1.4.0"]["path"] == str(train.resolve())
    assert calibration["data"]["jeff.safety.guard@1.4.0"] == str(train.resolve())
