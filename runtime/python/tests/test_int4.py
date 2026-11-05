"""The INT4 base folder and its loader (engine/int4.py; step 2b-2 design Sections 4.2 and 4.3), on a tiny synthetic
Qwen3.5 base: two decoder layers (one Gated DeltaNet, one full attention), 15 INT4 matrices, a one-block vision tower.
No tokenizer is needed: the backbone loader reads config.json, model.safetensors and int4.safetensors only.

Every way a folder can be wrong is refused at load with an EngineError naming the problem: a missing matrix, an extra
tensor, integers stored so they can be out of range, a wrong source base hash, an unknown format, a changed
int4.safetensors, wrong shapes. Adapters trained on the source base are accepted on the INT4 base; others are not."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import torch
from safetensors.torch import load_file, save_file
from transformers.models.qwen3_5.configuration_qwen3_5 import Qwen3_5Config
from transformers.models.qwen3_5.modeling_qwen3_5 import Qwen3_5Model

from jeff.errors import EngineError
from jeff_runtime.engine.int4 import (
    COPIED_FILES, GROUP, base_format, dequantize, file_sha256, int4_matrices, load_int4_backbone, pack_group,
    pack_int4, pack_kernel, read_int4_file, read_quantization, unpack_int4, unpack_kernel, write_int4_base,
)
from jeff_runtime.engine.lora import adapter_base_hashes, check_base, weights_sha256
from jeff_runtime.engine.quantize import round_to_nearest
from jeff_runtime.local_engine import check_base_format

TEXT = {"hidden_size": 64, "intermediate_size": 128, "num_hidden_layers": 2,
        "layer_types": ["linear_attention", "full_attention"], "full_attention_interval": 2,
        "num_attention_heads": 2, "num_key_value_heads": 1, "head_dim": 32, "linear_key_head_dim": 32,
        "linear_value_head_dim": 32, "linear_num_key_heads": 2, "linear_num_value_heads": 2, "vocab_size": 512,
        "attn_output_gate": True, "partial_rotary_factor": 0.25,
        "rope_parameters": {"mrope_interleaved": True, "mrope_section": [2, 1, 1], "partial_rotary_factor": 0.25,
                            "rope_theta": 10000000, "rope_type": "default"}}
VISION = {"depth": 1, "hidden_size": 32, "intermediate_size": 64, "num_heads": 2, "out_hidden_size": 64,
          "num_position_embeddings": 16, "deepstack_visual_indexes": []}
GPTQ = {"damp": 0.01, "block": 128, "act_order": True, "static_groups": True, "clip_search": [1.0, 0.7, 0.025]}
CALIBRATION = {"rows_per_adapter": 0, "rule": "none (a synthetic test base)", "rows": {}, "data": {},
               "arithmetic": "none"}


@pytest.fixture(scope="module")
def source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A tiny bfloat16 base folder, as a Jeff base has it (tokenizer files are placeholders: nothing here reads
    them)."""
    folder = tmp_path_factory.mktemp("source")
    torch.manual_seed(0)
    model = Qwen3_5Model(Qwen3_5Config(text_config=TEXT, vision_config=VISION)).to(torch.bfloat16)
    model.save_pretrained(str(folder))
    for name in ("tokenizer.json", "tokenizer_config.json", "chat_template.jinja", "processor_config.json"):
        (folder / name).write_text(f"placeholder {name}\n")
    save_file({"weight": torch.randn(255, 64).to(torch.bfloat16)}, str(folder / "readout.safetensors"))
    (folder / "decision_config.json").write_text(json.dumps({"format_version": 1, "max_options": 26}))
    return folder


@pytest.fixture(scope="module")
def matrices(source: Path) -> dict[str, tuple[torch.Tensor, torch.Tensor]]:
    state = load_file(str(source / "model.safetensors"))
    with torch.device("meta"):
        skeleton = Qwen3_5Model(Qwen3_5Config.from_pretrained(str(source)))
    return {name: round_to_nearest(state[f"{name}.weight"]) for name in int4_matrices(skeleton)}


def write(source: Path, destination: Path, matrices: dict[str, tuple[torch.Tensor, torch.Tensor]]) -> Path:
    write_int4_base(source, destination, matrices, method="gptq", source_id="jeff-base", source_version="v9.9",
                    calibration=CALIBRATION, gptq=GPTQ, tool={"jeff_runtime": "test"})
    return destination


@pytest.fixture
def folder(tmp_path: Path, source: Path, matrices: dict[str, tuple[torch.Tensor, torch.Tensor]]) -> Path:
    return write(source, tmp_path / "int4", matrices)


def load(folder: Path) -> torch.nn.Module:
    quantization = read_quantization(folder)
    assert quantization is not None
    return load_int4_backbone(quantization, torch.float32)


def edit_json(path: Path, change: Callable[[dict[str, Any]], None]) -> None:
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))


def rewrite_int4(folder: Path, change: Callable[[dict[str, torch.Tensor]], None]) -> None:
    """Change int4.safetensors and update the record's int4_sha256 and the format_id to match, so the loader's later
    checks (not the file hash) see the change."""
    tensors = load_file(str(folder / "int4.safetensors"))
    change(tensors)
    save_file(tensors, str(folder / "int4.safetensors"), metadata={"format": "pt"})
    digest = file_sha256(folder / "int4.safetensors")
    edit_json(folder / "quantization.json", lambda record: record.update(int4_sha256=digest))
    edit_json(folder / "decision_config.json", lambda config: config["quantization"].update(
        format_id=f"jeff-base@v9.9+int4-g64-gptq.{digest[:8]}"))


def rewrite_model(folder: Path, change: Callable[[dict[str, torch.Tensor]], None]) -> None:
    tensors = load_file(str(folder / "model.safetensors"))
    change(tensors)
    save_file(tensors, str(folder / "model.safetensors"), metadata={"format": "pt"})


FIRST = "language_model.layers.0.mlp.up_proj"


# ---- the folder as written, and loaded


def test_the_folder_has_the_designed_layout(folder: Path, source: Path) -> None:
    assert sorted(path.name for path in folder.iterdir()) == sorted(
        [*COPIED_FILES, "model.safetensors", "int4.safetensors", "decision_config.json", "quantization.json"])
    for name in COPIED_FILES:
        assert (folder / name).read_bytes() == (source / name).read_bytes()
    config = json.loads((folder / "decision_config.json").read_text())
    record = json.loads((folder / "quantization.json").read_text())
    source_hashes = weights_sha256(source)
    assert config["quantization"] == {
        "format": "int4-g64", "format_id": f"jeff-base@v9.9+int4-g64-gptq.{record['int4_sha256'][:8]}",
        "method": "gptq", "source_base": {"weights_sha256": source_hashes}, "record": "quantization.json"}
    assert config["max_options"] == 26  # the rest is the source's
    assert record["int4_sha256"] == file_sha256(folder / "int4.safetensors")
    assert record["source_base"] == {"id": "jeff-base", "version": "v9.9", "weights_sha256": source_hashes}
    assert (record["format"], record["group"], record["symmetric"], record["scale_dtype"], record["awq"]) == (
        "int4-g64", 64, True, "bfloat16", None)
    assert len(record["scale_exponent_spans"]["by_matrix"]) == 15
    packed = load_file(str(folder / "int4.safetensors"))
    assert len(packed) == 30 and packed[f"{FIRST}.qweight"].dtype == torch.uint8
    assert tuple(packed[f"{FIRST}.qweight"].shape) == (128, 32) and tuple(packed[f"{FIRST}.scales"].shape) == (128, 1)
    assert not any(name.removesuffix(".weight") in int4_matrices_of(source)
                   for name in load_file(str(folder / "model.safetensors")))


def int4_matrices_of(source: Path) -> dict[str, tuple[int, int]]:
    with torch.device("meta"):
        return int4_matrices(Qwen3_5Model(Qwen3_5Config.from_pretrained(str(source))))


def test_writing_twice_gives_the_same_bytes(tmp_path: Path, source: Path,
                                            matrices: dict[str, tuple[torch.Tensor, torch.Tensor]]) -> None:
    one, two = write(source, tmp_path / "one", matrices), write(source, tmp_path / "two", matrices)
    for name in ("int4.safetensors", "model.safetensors", "decision_config.json", "quantization.json"):
        assert (one / name).read_bytes() == (two / name).read_bytes()


def test_the_writer_refuses_an_existing_folder_and_a_wrong_matrix_set(
        tmp_path: Path, source: Path, matrices: dict[str, tuple[torch.Tensor, torch.Tensor]]) -> None:
    (tmp_path / "there").mkdir()
    with pytest.raises(EngineError, match="already exists"):
        write(source, tmp_path / "there", matrices)
    fewer = {name: value for name, value in matrices.items() if name != FIRST}
    with pytest.raises(EngineError, match=f"missing \\['{FIRST}'\\]"):
        write(source, tmp_path / "fewer", fewer)
    assert not (tmp_path / "fewer").exists()


def test_a_failed_write_leaves_no_folder(tmp_path: Path, source: Path,
                                         matrices: dict[str, tuple[torch.Tensor, torch.Tensor]],
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """The folder is written under a hidden sibling name and renamed at the end: a write that fails after some
    files are out leaves neither the folder nor the sibling. A sibling left by a killed write is refused, named."""
    import jeff_runtime.engine.int4 as int4

    def fail(_: object) -> None:
        raise RuntimeError("disk full")
    monkeypatch.setattr(int4, "scale_exponent_spans", fail)  # runs after both safetensors files are written
    with pytest.raises(RuntimeError, match="disk full"):
        write(source, tmp_path / "int4", matrices)
    assert sorted(tmp_path.iterdir()) == []
    monkeypatch.undo()
    (tmp_path / ".int4.partial").mkdir()
    with pytest.raises(EngineError, match="\\.int4\\.partial exists, left by a write that was killed; remove it first"):
        write(source, tmp_path / "int4", matrices)
    assert not (tmp_path / "int4").exists()


def test_the_writer_refuses_a_source_that_is_not_bfloat16(tmp_path: Path, source: Path,
                                                          matrices: dict[str, tuple[torch.Tensor, torch.Tensor]]) -> None:
    float32 = tmp_path / "float32"
    float32.mkdir()
    for path in source.iterdir():
        (float32 / path.name).write_bytes(path.read_bytes())
    tensors = load_file(str(source / "model.safetensors"))
    tensors["language_model.norm.weight"] = tensors["language_model.norm.weight"].float()
    save_file(tensors, str(float32 / "model.safetensors"), metadata={"format": "pt"})
    with pytest.raises(EngineError, match="holds 1 tensors that are not bfloat16 \\(language_model.norm.weight\\); an "
                                          "INT4 base is made from a bfloat16 base"):
        write(float32, tmp_path / "int4", matrices)


def test_the_loaded_matrices_are_bf16_of_q_times_s_and_the_rest_is_the_sources(
        folder: Path, source: Path, matrices: dict[str, tuple[torch.Tensor, torch.Tensor]]) -> None:
    backbone = load(folder)
    weights = backbone.state_dict()
    original = load_file(str(source / "model.safetensors"))
    assert set(weights) == set(original)
    for name, (q, scales) in matrices.items():
        expected = (q.float().reshape(q.shape[0], -1, GROUP) * scales.float()[..., None]).reshape(q.shape)
        assert torch.equal(weights[f"{name}.weight"], expected.to(torch.bfloat16).float())
    for name, tensor in original.items():
        if name.removesuffix(".weight") not in matrices:
            assert torch.equal(weights[name], tensor.float()), name


def test_packing_round_trips_and_refuses_integers_out_of_range() -> None:
    q = torch.arange(-8, 8, dtype=torch.int8).repeat(3, 8)  # (3, 128): every value, in both nibbles
    packed = pack_int4(q)
    assert packed.dtype == torch.uint8 and tuple(packed.shape) == (3, 64)
    assert int(packed[0, 0]) == ((-8 + 8) | ((-7 + 8) << 4))  # low nibble first, each stored as q + 8
    assert torch.equal(unpack_int4(packed), q)
    for bad in (8, -9):
        with pytest.raises(EngineError, match="holds integers in \\[-8, 7\\] only"):
            pack_int4(torch.full((2, 64), bad, dtype=torch.int16))


def jeff_asic_pack(q: torch.Tensor) -> torch.Tensor:
    """jeff-asic's packing (benchmark/gpu_int4_fused.py pack_int4), as the design's kernel reads it: (out, in) ->
    (in / 8, out) int32, eight values q + 8 per word along the input."""
    u = (q.to(torch.int32) + 8).T.contiguous()
    packed = torch.zeros(u.shape[0] // 8, u.shape[1], dtype=torch.int32)
    for j in range(8):
        packed |= u[j::8] << (4 * j)
    return packed


def random_int4(out: int, inputs: int, seed: int) -> tuple[torch.Tensor, torch.Tensor]:
    generator = torch.Generator().manual_seed(seed)
    q = torch.randint(-8, 8, (out, inputs), generator=generator, dtype=torch.int8)
    scales = (torch.rand(out, inputs // GROUP, generator=generator) + 0.01).to(torch.bfloat16)
    return q, scales


def test_the_kernel_layout_is_jeff_asics_and_round_trips() -> None:
    q, scales = random_int4(128, 256, 0)
    q[0, :8] = torch.tensor([-8, -7, -1, 0, 1, 5, 6, 7])
    packed = pack_kernel(q, scales)
    assert packed.out == 128 and packed.words.dtype == torch.int32 and packed.scales.dtype == torch.bfloat16
    assert tuple(packed.words.shape) == (32, 128) and tuple(packed.scales.shape) == (4, 128)
    assert packed.words.is_contiguous() and packed.scales.is_contiguous()
    assert torch.equal(packed.words, jeff_asic_pack(q))
    assert torch.equal(packed.scales, scales.T)  # the scale of inputs 64g .. 64g + 63 of column c at (g, c)
    # word 0 of column 0: input j in bits 4j .. 4j + 3, as q + 8
    assert int(packed.words[0, 0]) & 0xFFFFFFFF == 0xFED98710
    assert packed.nbytes() == 32 * 128 * 4 + 4 * 128 * 2
    back_q, back_scales = unpack_kernel(packed)
    assert back_q.dtype == torch.int8 and torch.equal(back_q, q) and torch.equal(back_scales, scales)


def test_the_kernel_layout_pads_the_outputs_to_64_with_zero_weights() -> None:
    q, scales = random_int4(70, 128, 1)
    packed = pack_kernel(q, scales)
    assert packed.out == 70 and tuple(packed.words.shape) == (16, 128) and tuple(packed.scales.shape) == (2, 128)
    assert bool((packed.words[:, 70:] == 0x88888888 - (1 << 32)).all())  # every nibble 0 + 8
    assert bool((packed.scales[:, 70:] == 1).all())
    padded = torch.cat([q, torch.zeros(58, 128, dtype=torch.int8)])
    assert torch.equal(packed.words, jeff_asic_pack(padded))
    back_q, back_scales = unpack_kernel(packed)
    assert torch.equal(back_q, q) and torch.equal(back_scales, scales)


def test_the_kernel_layout_refuses_what_it_cannot_hold() -> None:
    q, scales = random_int4(64, 128, 2)
    for bad in (8, -9):
        wrong = q.to(torch.int16)
        wrong[3, 5] = bad
        with pytest.raises(EngineError, match="holds integers in \\[-8, 7\\] only"):
            pack_kernel(wrong, scales)
    with pytest.raises(EngineError, match="inputs are a multiple of 64"):
        pack_kernel(q[:, :96], scales)
    with pytest.raises(EngineError, match="the scales must be bfloat16 \\(64, 2\\)"):
        pack_kernel(q, scales.float())
    with pytest.raises(EngineError, match="the scales must be bfloat16 \\(64, 2\\)"):
        pack_kernel(q, scales[:, :1])


def test_a_fused_group_packs_the_folders_integers_and_checks_its_bfloat16_copy(
        folder: Path, matrices: dict[str, tuple[torch.Tensor, torch.Tensor]]) -> None:
    """As the fast path fuses them: the matrices that read the same input, stacked along the output."""
    quantization = read_quantization(folder)
    assert quantization is not None and quantization.folder == folder
    backbone = load(folder)
    shapes = int4_matrices(backbone)
    stored = read_int4_file(quantization, shapes)
    assert set(stored) == set(matrices)
    group = [f"language_model.layers.0.linear_attn.{name}"
             for name in ("in_proj_qkv", "in_proj_z", "in_proj_b", "in_proj_a")]
    weights = backbone.state_dict()
    copy = torch.cat([weights[f"{name}.weight"] for name in group]).to(torch.bfloat16)
    packed = pack_group(stored, group, copy)
    assert packed.out == copy.shape[0] and packed.words.shape[1] % 64 == 0
    q, scales = unpack_kernel(packed)
    assert torch.equal(q, torch.cat([matrices[name][0] for name in group]))
    assert torch.equal(scales, torch.cat([matrices[name][1] for name in group]))
    changed = copy.clone()
    changed[1, 2] += 1
    with pytest.raises(EngineError, match="The bfloat16 weights of language_model.layers.0.linear_attn.in_proj_qkv, "
                                          ".*in_proj_a are not bf16\\(q \\* s\\) of their integers and scales"):
        pack_group(stored, group, changed)
    with pytest.raises(EngineError, match="are not bf16\\(q \\* s\\)"):
        pack_group(stored, group, copy.float())
    with pytest.raises(EngineError, match="lacks 2 tensors \\(language_model.layers.9.mlp.up_proj.qweight, "
                                          "language_model.layers.9.mlp.up_proj.scales\\)"):
        read_int4_file(quantization, {**shapes, "language_model.layers.9.mlp.up_proj": (128, 64)})


def test_dequantize_rounds_once_to_bfloat16() -> None:
    q = torch.full((1, 64), 7, dtype=torch.int8)
    scales = torch.tensor([[1.0078125]], dtype=torch.bfloat16)  # 1 + 2^-7: 7 s needs 11 bits, rounded to 8
    assert dequantize(q, scales).dtype == torch.bfloat16
    assert float(dequantize(q, scales)[0, 0]) == float(torch.tensor(7 * 1.0078125).to(torch.bfloat16))


def test_base_format(folder: Path, source: Path) -> None:
    record = json.loads((folder / "quantization.json").read_text())
    assert base_format(source) == {"format": "bf16"}
    assert base_format(folder) == {"format": "int4-g64",
                                   "format_id": f"jeff-base@v9.9+int4-g64-gptq.{record['int4_sha256'][:8]}"}


# ---- every way a folder can be wrong is refused, naming it


def test_a_missing_matrix_is_refused(folder: Path) -> None:
    rewrite_int4(folder, lambda tensors: [tensors.pop(f"{FIRST}.qweight"), tensors.pop(f"{FIRST}.scales")])
    with pytest.raises(EngineError, match=f"int4.safetensors lacks 2 tensors \\({FIRST}.qweight, {FIRST}.scales\\)"):
        load(folder)


def test_a_missing_other_tensor_is_refused(folder: Path) -> None:
    rewrite_model(folder, lambda tensors: tensors.pop("language_model.norm.weight"))
    with pytest.raises(EngineError, match="model.safetensors lacks 1 tensors of the model \\(language_model.norm.weight\\)"):
        load(folder)


def test_an_extra_tensor_in_int4_safetensors_is_refused(folder: Path) -> None:
    rewrite_int4(folder, lambda tensors: tensors.update({"language_model.norm.qweight": torch.zeros(1, 1, dtype=torch.uint8)}))
    with pytest.raises(EngineError, match="holds 1 tensors that are not an INT4 matrix's qweight or scales "
                                          "\\(language_model.norm.qweight\\)"):
        load(folder)


def test_an_extra_tensor_in_model_safetensors_is_refused(folder: Path) -> None:
    rewrite_model(folder, lambda tensors: tensors.update({"language_model.extra": torch.zeros(2)}))
    with pytest.raises(EngineError, match="holds 1 tensors the model does not have \\(language_model.extra\\)"):
        load(folder)


def test_a_matrix_left_in_model_safetensors_is_refused(folder: Path) -> None:
    rewrite_model(folder, lambda tensors: tensors.update({f"{FIRST}.weight": torch.zeros(128, 64)}))
    with pytest.raises(EngineError, match=f"holds 1 of the INT4 matrices \\({FIRST}.weight\\); an INT4 base keeps "
                                          "them only in int4.safetensors"):
        load(folder)


def test_integers_stored_so_they_can_be_out_of_range_are_refused(folder: Path) -> None:
    """The folder's nibbles hold q + 8, so a uint8 qweight cannot hold an integer out of [-8, 7]; integers stored any
    other way (here unpacked int8, one with the value 9) are refused for their type, before any is read."""
    def unpacked(tensors: dict[str, torch.Tensor]) -> None:
        q = unpack_int4(tensors[f"{FIRST}.qweight"]).clone()
        q[0, 0] = 9
        tensors[f"{FIRST}.qweight"] = q
    rewrite_int4(folder, unpacked)
    with pytest.raises(EngineError, match=f"{FIRST}.qweight is torch.int8; it must be uint8, two 4-bit integers per "
                                          "byte stored as q \\+ 8 .*can be out of range"):
        load(folder)


def test_a_misshapen_qweight_or_scale_is_refused(folder: Path) -> None:
    rewrite_int4(folder, lambda tensors: tensors.update({f"{FIRST}.scales": tensors[f"{FIRST}.scales"][:64]}))
    with pytest.raises(EngineError, match=f"{FIRST}.scales has shape \\(64, 1\\); the 128 x 64 matrix needs "
                                          "\\(128, 1\\)"):
        load(folder)


def test_scales_of_another_type_are_refused(folder: Path) -> None:
    rewrite_int4(folder, lambda tensors: tensors.update({f"{FIRST}.scales": tensors[f"{FIRST}.scales"].float()}))
    with pytest.raises(EngineError, match=f"{FIRST}.scales is torch.float32; it must be bfloat16"):
        load(folder)


def test_a_misshapen_other_tensor_is_refused(folder: Path) -> None:
    rewrite_model(folder, lambda tensors: tensors.update(
        {"language_model.norm.weight": tensors["language_model.norm.weight"][:32].clone()}))
    with pytest.raises(EngineError, match="model.safetensors: language_model.norm.weight has shape \\(32,\\); the "
                                          "model's is \\(64,\\)"):
        load(folder)


def test_another_tensor_not_in_bfloat16_is_refused(folder: Path) -> None:
    rewrite_model(folder, lambda tensors: tensors.update(
        {"language_model.norm.weight": tensors["language_model.norm.weight"].float()}))
    with pytest.raises(EngineError, match="model.safetensors: language_model.norm.weight is torch.float32; every "
                                          "tensor of an INT4 base's model.safetensors is bfloat16"):
        load(folder)


def test_a_scale_that_is_not_positive_is_refused(folder: Path) -> None:
    rewrite_int4(folder, lambda tensors: tensors[f"{FIRST}.scales"].__setitem__((0, 0), 0.0))
    with pytest.raises(EngineError, match=f"{FIRST}.scales holds a scale that is not finite and positive"):
        load(folder)


def test_a_missing_int4_file_is_refused(folder: Path) -> None:
    (folder / "int4.safetensors").unlink()
    with pytest.raises(EngineError, match="is an INT4 base but has no int4.safetensors"):
        load(folder)


def test_a_changed_int4_file_is_refused(folder: Path) -> None:
    tensors = load_file(str(folder / "int4.safetensors"))
    tensors[f"{FIRST}.qweight"][0, 0] ^= 1
    save_file(tensors, str(folder / "int4.safetensors"), metadata={"format": "pt"})
    with pytest.raises(EngineError, match="int4.safetensors has sha256 [0-9a-f]{64}, but quantization.json records "
                                          "[0-9a-f]{64}: the file is not the one"):
        load(folder)


def test_a_wrong_source_hash_is_refused(folder: Path) -> None:
    edit_json(folder / "decision_config.json", lambda config: config["quantization"]["source_base"].update(
        weights_sha256={"model.safetensors": "0" * 64}))
    with pytest.raises(EngineError, match="The source base hashes differ: decision_config.json .* says "
                                          "\\{'model.safetensors': '0000"):
        read_quantization(folder)


def test_an_unknown_format_is_refused(folder: Path) -> None:
    edit_json(folder / "decision_config.json", lambda config: config["quantization"].update(format="int4-g128"))
    with pytest.raises(EngineError, match="names the unknown format 'int4-g128'; this runtime reads 'int4-g64'"):
        read_quantization(folder)


def test_decision_model_refuses_an_unknown_format_before_loading_anything(folder: Path) -> None:
    from jeff_runtime.engine.model import DecisionModel

    edit_json(folder / "decision_config.json", lambda config: config["quantization"].update(format="nf4"))
    with pytest.raises(EngineError, match="unknown format 'nf4'"):
        DecisionModel(checkpoint=folder, device="cpu")


@pytest.mark.parametrize(("change", "message"), [
    (lambda q: q.update(method="awq-gptq"), "AWQ is not built"),
    (lambda q: q.update(method="hqq"), "unknown method 'hqq'"),
    (lambda q: q.update(format_id="jeff-base@v9.9+int4-g64-gptq.00000000"), "\"format_id\" is "
                                                                              "'jeff-base@v9.9\\+int4-g64-gptq.00000000'"),
    (lambda q: q.update(extra=1), "must have exactly the keys"),
    (lambda q: q.update(record="other.json"), "\"record\" must be 'quantization.json'"),
])
def test_other_wrong_quantization_objects_are_refused(folder: Path, change: Callable[[dict[str, Any]], None],
                                                      message: str) -> None:
    edit_json(folder / "decision_config.json", lambda config: change(config["quantization"]))
    with pytest.raises(EngineError, match=message):
        read_quantization(folder)


def test_a_record_that_disagrees_is_refused(folder: Path) -> None:
    edit_json(folder / "quantization.json", lambda record: record.update(group=128))
    with pytest.raises(EngineError, match="quantization.json \"group\" is 128; for this base it must be 64"):
        read_quantization(folder)
    edit_json(folder / "quantization.json", lambda record: [record.update(group=64), record.pop("tool")])
    with pytest.raises(EngineError, match="must have exactly the keys"):
        read_quantization(folder)
    (folder / "quantization.json").unlink()
    with pytest.raises(EngineError, match="has no quantization.json"):
        read_quantization(folder)


def test_the_gptq_settings_are_there_exactly_for_a_gptq_base(tmp_path: Path, source: Path,
                                                             matrices: dict[str, tuple[torch.Tensor, torch.Tensor]],
                                                             folder: Path) -> None:
    edit_json(folder / "quantization.json", lambda record: record.update(gptq=None))
    with pytest.raises(EngineError, match="\"gptq\" must be the GPTQ settings for a GPTQ base and null otherwise; got "
                                          "None for method 'gptq'"):
        read_quantization(folder)
    rtn = tmp_path / "rtn"
    write_int4_base(source, rtn, matrices, method="rtn", source_id="jeff-base", source_version="v9.9",
                    calibration=CALIBRATION, gptq=None, tool={"jeff_runtime": "test"})
    assert read_quantization(rtn) is not None
    edit_json(rtn / "quantization.json", lambda record: record.update(gptq=GPTQ))
    with pytest.raises(EngineError, match="\"gptq\" must be the GPTQ settings for a GPTQ base and null otherwise; got "
                                          "\\{'damp'.* for method 'rtn'"):
        read_quantization(rtn)


# ---- adapters, and the backends that serve an INT4 base


def decoder(quantization: object) -> SimpleNamespace:
    return SimpleNamespace(base_model="Qwen/Qwen3.5-0.8B", revision="r" * 40, codes=["A"], token_ids=[1],
                           quantization=quantization)


def adapter_config(trained_on: dict[str, str]) -> dict[str, Any]:
    return {"base_model": "Qwen/Qwen3.5-0.8B", "revision": "r" * 40, "architecture": None, "codes": ["A"],
            "token_ids": [1], "adapter": {"base_checkpoint": {"path": "/somewhere/base",
                                                              "weights_sha256": trained_on}}}


def test_adapters_trained_on_the_source_base_are_accepted_others_refused(folder: Path, source: Path) -> None:
    source_hashes = weights_sha256(source)
    hashes = adapter_base_hashes(folder)
    assert hashes == source_hashes != weights_sha256(folder)  # the source's, not the INT4 folder's own file
    assert adapter_base_hashes(source) == source_hashes
    model = decoder(read_quantization(folder))
    check_base(adapter_config(source_hashes), Path("adapter"), model, hashes)  # accepted
    with pytest.raises(ValueError, match="trained on the base checkpoint /somewhere/base .* which is not the source "
                                         "base the loaded INT4 base was quantised from"):
        check_base(adapter_config(weights_sha256(folder)), Path("adapter"), model, hashes)


def test_only_mlx_refuses_an_int4_base(folder: Path, source: Path) -> None:
    weights = base_format(folder)
    check_base_format("torch", folder, weights)
    check_base_format("fast", folder, weights)
    for backend in ("torch", "fast", "mlx"):
        check_base_format(backend, source, base_format(source))
    with pytest.raises(EngineError, match="the MLX backend serves bfloat16 bases only"):
        check_base_format("mlx", folder, weights)
