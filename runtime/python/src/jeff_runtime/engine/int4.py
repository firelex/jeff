"""The INT4 base folder (step 2b-2 design, Sections 4.2 and 4.3): reading, checking and writing it.

An INT4 base is a base checkpoint whose 186 matrices of the text decoder's 24 layers (every linear layer under
`language_model.layers`) are stored as symmetric 4-bit integers in [-8, 7] with one bfloat16 scale per 64 inputs and
no zero point (the format "int4-g64"). Everything else (the token embedding, the vision tower, the norms, the readout,
the tokenizer) is the source base's, unchanged. The folder:

    config.json, tokenizer.json, tokenizer_config.json, chat_template.jinja, processor_config.json   the source's
    readout.safetensors      the source's
    model.safetensors        every tensor of the source base except the 186 matrices
    int4.safetensors         per matrix: <name>.qweight uint8 (out, in/2), two integers per byte, low nibble first,
                             each stored as q + 8; <name>.scales bfloat16 (out, in/64)
    decision_config.json     the source's, plus the "quantization" object (QUANTIZATION_KEYS)
    quantization.json        the record (RECORD_KEYS)

Loading is strict and nothing falls back to bfloat16: a folder that is wrong in any way (an unknown format, a
missing, extra or misshapen tensor, a changed int4.safetensors, records that disagree) is refused with an
EngineError naming the problem. The model's weights are bf16(q * s), exactly what the standard path multiplies by."""

import dataclasses
import hashlib
import json
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import torch
from safetensors import safe_open
from safetensors.torch import load_file, save_file

from jeff.errors import EngineError

BF16 = "bf16"  # the format of a base whose weights are all bfloat16 (every base before step 2b-2)
FORMAT = "int4-g64"
FORMATS = (FORMAT,)
GROUP = 64  # inputs per scale
LOW, HIGH = -8, 7  # the integers' range
METHODS = ("gptq", "rtn")  # "awq-gptq" is specified in the design but not built (design Section 1)
INT4_FILE = "int4.safetensors"
RECORD_FILE = "quantization.json"
MATRICES_UNDER = "language_model.layers."
# The files an INT4 base copies unchanged from its source base.
COPIED_FILES = ("config.json", "tokenizer.json", "tokenizer_config.json", "chat_template.jinja",
                "processor_config.json", "readout.safetensors")
QUANTIZATION_KEYS = frozenset({"format", "format_id", "method", "source_base", "record"})
RECORD_KEYS = frozenset({"format", "method", "group", "symmetric", "scale_dtype", "source_base", "calibration", "gptq",
                         "awq", "scale_exponent_spans", "tool", "int4_sha256"})
SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Quantization:
    """A checked "quantization" object of an INT4 base's decision_config.json, with its record."""
    format: str
    format_id: str
    method: str
    source_hashes: dict[str, str]  # the source base's weights_sha256: what an adapter must have been trained on
    record: dict[str, Any]
    folder: Path  # the INT4 base folder these records are of


def format_id(source_id: str, source_version: str, method: str, int4_sha256: str) -> str:
    """The name of one exact INT4 base: <source id>@<source version>+int4-g64-<method>.<first 8 hex of the sha256 of
    int4.safetensors>, for example jeff-base@v1.3+int4-g64-gptq.3f9a1c2e."""
    return f"{source_id}@{source_version}+{FORMAT}-{method}.{int4_sha256[:8]}"


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _hashes(value: object, where: str) -> dict[str, str]:
    if not isinstance(value, dict) or not value or not all(
            isinstance(name, str) and isinstance(digest, str) and SHA256.fullmatch(digest)
            for name, digest in value.items()):
        raise EngineError(f"{where} must map each weight file of the source base to its sha256 (64 lower-case hex "
                          f"digits); got {value!r}")
    return cast(dict[str, str], dict(value))


def read_quantization(folder: Path) -> Quantization | None:
    """The checked "quantization" object of a base folder's decision_config.json and its record, or None for a
    bfloat16 base (no such object). Reads the two JSON files only; the tensors are checked by load_int4_weights."""
    config = json.loads((folder / "decision_config.json").read_text())
    if "quantization" not in config:
        return None
    where = f"{folder / 'decision_config.json'} \"quantization\""
    quantization = config["quantization"]
    if not isinstance(quantization, dict):
        raise EngineError(f"{where} must be an object; got {quantization!r}")
    if set(quantization) != QUANTIZATION_KEYS:
        raise EngineError(f"{where} must have exactly the keys {sorted(QUANTIZATION_KEYS)}; it has "
                          f"{sorted(quantization)}")
    if quantization["format"] not in FORMATS:
        raise EngineError(f"{where} names the unknown format {quantization['format']!r}; this runtime reads "
                          f"{', '.join(repr(name) for name in FORMATS)}")
    method = quantization["method"]
    if method == "awq-gptq":
        raise EngineError(f"{where} names the method 'awq-gptq': AWQ bases need their adapters converted at install, "
                          "which this runtime does not do (AWQ is not built; use a GPTQ base)")
    if method not in METHODS:
        raise EngineError(f"{where} names the unknown method {method!r}; this runtime reads "
                          f"{', '.join(repr(name) for name in METHODS)}")
    source = quantization["source_base"]
    if not isinstance(source, dict) or set(source) != {"weights_sha256"}:
        raise EngineError(f"{where} \"source_base\" must be exactly {{\"weights_sha256\": {{...}}}}; got {source!r}")
    source_hashes = _hashes(source["weights_sha256"], f"{where} \"source_base\" \"weights_sha256\"")
    if quantization["record"] != RECORD_FILE:
        raise EngineError(f"{where} \"record\" must be {RECORD_FILE!r}; got {quantization['record']!r}")
    record_path = folder / RECORD_FILE
    if not record_path.is_file():
        raise EngineError(f"{folder} is an INT4 base but has no {RECORD_FILE} (its record)")
    record = json.loads(record_path.read_text())
    if not isinstance(record, dict) or set(record) != RECORD_KEYS:
        raise EngineError(f"{record_path} must have exactly the keys {sorted(RECORD_KEYS)}; it has "
                          f"{sorted(record) if isinstance(record, dict) else record!r}")
    expected = {"format": quantization["format"], "method": method, "group": GROUP, "symmetric": True,
                "scale_dtype": "bfloat16", "awq": None}
    for key, value in expected.items():
        if record[key] != value or type(record[key]) is not type(value):
            raise EngineError(f"{record_path} \"{key}\" is {record[key]!r}; for this base it must be {value!r} "
                              f"(decision_config.json says format {quantization['format']!r}, method {method!r})")
    settings = record["gptq"]
    if (settings is None) != (method != "gptq") or (settings is not None and not isinstance(settings, dict)):
        raise EngineError(f"{record_path} \"gptq\" must be the GPTQ settings for a GPTQ base and null otherwise; "
                          f"got {record['gptq']!r} for method {method!r}")
    record_source = record["source_base"]
    if not isinstance(record_source, dict) or set(record_source) != {"id", "version", "weights_sha256"} \
            or not isinstance(record_source["id"], str) or not isinstance(record_source["version"], str):
        raise EngineError(f"{record_path} \"source_base\" must be {{\"id\", \"version\", \"weights_sha256\"}}; got "
                          f"{record_source!r}")
    if _hashes(record_source["weights_sha256"], f"{record_path} \"source_base\" \"weights_sha256\"") != source_hashes:
        raise EngineError(f"The source base hashes differ: decision_config.json \"quantization\" \"source_base\" says "
                          f"{source_hashes}, {RECORD_FILE} says {record_source['weights_sha256']}; one of the two "
                          "files is not this INT4 base's")
    int4_sha256 = record["int4_sha256"]
    if not isinstance(int4_sha256, str) or not SHA256.fullmatch(int4_sha256):
        raise EngineError(f"{record_path} \"int4_sha256\" must be 64 lower-case hex digits; got {int4_sha256!r}")
    wanted_id = format_id(record_source["id"], record_source["version"], method, int4_sha256)
    if quantization["format_id"] != wanted_id:
        raise EngineError(f"{where} \"format_id\" is {quantization['format_id']!r}; this INT4 base's is {wanted_id!r} "
                          f"(from {RECORD_FILE}: source {record_source['id']}@{record_source['version']}, method "
                          f"{method}, int4_sha256 {int4_sha256[:8]}...)")
    return Quantization(quantization["format"], wanted_id, method, source_hashes, record, folder)


def base_format(folder: Path) -> dict[str, str]:
    """What an engine serving this base reports in every predict timing and in health.base: {"format": "bf16"} for a
    bfloat16 base, {"format": "int4-g64", "format_id": ...} for an INT4 one."""
    quantization = read_quantization(folder)
    if quantization is None:
        return {"format": BF16}
    return {"format": quantization.format, "format_id": quantization.format_id}


def is_int4_matrix(name: str, module: torch.nn.Module) -> bool:
    return isinstance(module, torch.nn.Linear) and name.startswith(MATRICES_UNDER)


def int4_matrices(backbone: torch.nn.Module) -> dict[str, tuple[int, int]]:
    """The matrices an INT4 base stores as integers, by module name: every linear layer of the text decoder's layers
    (186 in the v1.3 base), with its (out, in) shape. Each must have a whole number of 64-input groups."""
    found: dict[str, tuple[int, int]] = {}
    for name, module in backbone.named_modules():
        if not is_int4_matrix(name, module):
            continue
        linear = cast(torch.nn.Linear, module)
        if linear.in_features % GROUP:
            raise EngineError(f"{name} has {linear.in_features} inputs, not a multiple of {GROUP}; it cannot be held "
                              f"in {FORMAT}")
        if linear.bias is not None:
            raise EngineError(f"{name} has a bias; {FORMAT} holds matrices without one")
        found[name] = (linear.out_features, linear.in_features)
    if not found:
        raise EngineError(f"{type(backbone).__name__} has no linear layers under {MATRICES_UNDER!r} to hold in INT4")
    return found


def pack_int4(q: torch.Tensor) -> torch.Tensor:
    """Integers in [-8, 7], shape (out, in), as the folder's uint8 (out, in / 2): q + 8 per nibble, low nibble
    first. Integers out of the range are refused (packing would wrap them)."""
    if q.dtype.is_floating_point or q.dtype == torch.bool or q.dim() != 2 or q.shape[1] % 2:
        raise EngineError(f"pack_int4 takes a 2-D integer tensor with an even number of columns; got {q.dtype} "
                          f"{tuple(q.shape)}")
    low, high = int(q.min()), int(q.max())
    if low < LOW or high > HIGH:
        raise EngineError(f"Integers from {low} to {high}; {FORMAT} holds integers in [{LOW}, {HIGH}] only")
    stored = (q.to(torch.int16) - LOW).to(torch.uint8)
    return (stored[:, 0::2] | (stored[:, 1::2] << 4)).contiguous()


def unpack_int4(packed: torch.Tensor) -> torch.Tensor:
    """The folder's uint8 (out, in / 2) as int8 integers (out, in) in [-8, 7]."""
    low = (packed & 0xF).to(torch.int8) + LOW
    high = (packed >> 4).to(torch.int8) + LOW
    return torch.stack((low, high), dim=-1).reshape(packed.shape[0], packed.shape[1] * 2)


def dequantize(q: torch.Tensor, scales: torch.Tensor) -> torch.Tensor:
    """bf16(q * s): each integer times its group's scale, in float32 (exact: a 4-bit integer times a bfloat16),
    rounded once to bfloat16."""
    out, inputs = q.shape
    product = q.to(torch.float32).reshape(out, inputs // GROUP, GROUP) * scales.to(torch.float32)[..., None]
    return product.reshape(out, inputs).to(torch.bfloat16)


KERNEL_COLUMNS = 64  # the INT4 kernel's output tile: a fused group's outputs are padded to a multiple of it
WORD = 8  # integers per int32 word of the kernel layout


@dataclass(frozen=True)
class KernelInt4:
    """The integers and scales of a fused group of matrices (fast_path.Lin: matrices that read the same input, stacked
    along the output) in the layout the fast path's INT4 kernel reads (design Section 3.1; jeff-asic's
    benchmark/gpu_int4_fused.py):
    - words: int32 (in / 8, columns); word i of column c holds the integers of inputs 8i .. 8i + 7, input 8i + j as
      q + 8 in bits 4j .. 4j + 3;
    - scales: bfloat16 (in / 64, columns), the scale of inputs 64g .. 64g + 63 of column c at (g, c);
    - columns: out rounded up to a multiple of 64; the padding columns hold q = 0 and scale 1 (zero weights).
    out: the group's real outputs."""
    words: torch.Tensor
    scales: torch.Tensor
    out: int

    def nbytes(self) -> int:
        return self.words.numel() * self.words.element_size() + self.scales.numel() * self.scales.element_size()


def pack_kernel(q: torch.Tensor, scales: torch.Tensor) -> KernelInt4:
    """Integers (out, in) in [-8, 7] and their bfloat16 scales (out, in / 64), as the folder holds them once unpacked,
    in the kernel's layout (KernelInt4), on q's device."""
    if q.dtype.is_floating_point or q.dtype == torch.bool or q.dim() != 2 or q.shape[1] % GROUP:
        raise EngineError(f"pack_kernel takes a 2-D integer tensor whose inputs are a multiple of {GROUP}; got "
                          f"{q.dtype} {tuple(q.shape)}")
    out, inputs = q.shape
    if scales.dtype != torch.bfloat16 or tuple(scales.shape) != (out, inputs // GROUP):
        raise EngineError(f"pack_kernel: the scales must be bfloat16 ({out}, {inputs // GROUP}); got {scales.dtype} "
                          f"{tuple(scales.shape)}")
    low, high = int(q.min()), int(q.max())
    if low < LOW or high > HIGH:
        raise EngineError(f"Integers from {low} to {high}; {FORMAT} holds integers in [{LOW}, {HIGH}] only")
    columns = -(-out // KERNEL_COLUMNS) * KERNEL_COLUMNS
    stored = torch.full((columns, inputs), -LOW, dtype=torch.int32, device=q.device)  # padding columns: q = 0
    stored[:out] = q.to(torch.int32) - LOW
    stored = stored.T.contiguous()  # (in, columns)
    words = torch.zeros(inputs // WORD, columns, dtype=torch.int32, device=q.device)
    for j in range(WORD):
        words |= stored[j::WORD] << (4 * j)  # j = 7 sets the sign bit when q + 8 >= 8; the kernel masks each nibble
    padded = torch.ones(columns, inputs // GROUP, dtype=torch.bfloat16, device=q.device)
    padded[:out] = scales
    return KernelInt4(words, padded.T.contiguous(), out)


def unpack_kernel(packed: KernelInt4) -> tuple[torch.Tensor, torch.Tensor]:
    """pack_kernel's inverse: the integers (out, in) as int8 and the scales (out, in / 64), padding columns dropped."""
    nibbles = torch.stack([(packed.words >> (4 * j)) & 0xF for j in range(WORD)], dim=1)  # (in / 8, 8, columns)
    q = (nibbles.reshape(-1, packed.words.shape[1]).T[:packed.out] + LOW).to(torch.int8)
    return q.contiguous(), packed.scales.T[:packed.out].contiguous()


def pack_group(stored: Mapping[str, tuple[torch.Tensor, torch.Tensor]], names: Sequence[str],
               dequantised: torch.Tensor) -> KernelInt4:
    """The kernel layout of a fused group: the matrices `names`, stacked along the output in that order, from
    `stored` (read_int4_file: {name: (qweight, scales)}), packed on the device of `dequantised`, the group's bfloat16
    weights that cuBLAS multiplies by. Those must be bf16(q * s) of the same integers and scales, bit for bit (the
    standard path's weights); EngineError naming the group otherwise."""
    device = dequantised.device
    q = torch.cat([unpack_int4(stored[name][0].to(device)) for name in names])
    scales = torch.cat([stored[name][1].to(device) for name in names])
    if dequantised.dtype != torch.bfloat16 or not torch.equal(dequantize(q, scales), dequantised):
        raise EngineError(f"The bfloat16 weights of {', '.join(names)} are not bf16(q * s) of their integers and "
                          f"scales in {INT4_FILE}")
    return pack_kernel(q, scales)


def _check_matrix(name: str, shape: tuple[int, int], qweight: torch.Tensor, scales: torch.Tensor) -> None:
    out, inputs = shape
    if qweight.dtype != torch.uint8:
        raise EngineError(f"{INT4_FILE}: {name}.qweight is {qweight.dtype}; it must be uint8, two 4-bit integers per "
                          f"byte stored as q + 8 (so every integer is in [{LOW}, {HIGH}]): integers stored any other "
                          "way can be out of range")
    if tuple(qweight.shape) != (out, inputs // 2):
        raise EngineError(f"{INT4_FILE}: {name}.qweight has shape {tuple(qweight.shape)}; the {out} x {inputs} "
                          f"matrix needs ({out}, {inputs // 2})")
    if scales.dtype != torch.bfloat16:
        raise EngineError(f"{INT4_FILE}: {name}.scales is {scales.dtype}; it must be bfloat16")
    if tuple(scales.shape) != (out, inputs // GROUP):
        raise EngineError(f"{INT4_FILE}: {name}.scales has shape {tuple(scales.shape)}; the {out} x {inputs} matrix "
                          f"needs ({out}, {inputs // GROUP}), one scale per {GROUP} inputs")
    if not bool(torch.isfinite(scales).all()) or not bool((scales > 0).all()):
        raise EngineError(f"{INT4_FILE}: {name}.scales holds a scale that is not finite and positive")


def read_int4_file(quantization: Quantization, matrices: Mapping[str, tuple[int, int]]
                   ) -> dict[str, tuple[torch.Tensor, torch.Tensor]]:
    """{matrix name: (qweight, scales)} from the INT4 folder's int4.safetensors, every tensor checked: the file must be
    the one the record names (its sha256) and hold exactly a qweight (uint8, (out, in / 2)) and scales (bfloat16,
    (out, in / 64)) for each of `matrices` (name -> (out, in)), and nothing else."""
    int4_path = quantization.folder / INT4_FILE
    if not int4_path.is_file():
        raise EngineError(f"{quantization.folder} is an INT4 base but has no {INT4_FILE}")
    digest = file_sha256(int4_path)
    if digest != quantization.record["int4_sha256"]:
        raise EngineError(f"{int4_path} has sha256 {digest}, but {RECORD_FILE} records "
                          f"{quantization.record['int4_sha256']}: the file is not the one this INT4 base was made with")
    packed = load_file(str(int4_path))
    wanted = {f"{name}.{part}" for name in matrices for part in ("qweight", "scales")}
    absent = sorted(wanted - set(packed))
    if absent:
        raise EngineError(f"{int4_path} lacks {len(absent)} tensors ({', '.join(absent[:3])}"
                          f"{', ...' if len(absent) > 3 else ''}): each INT4 matrix needs its qweight and scales")
    surplus = sorted(set(packed) - wanted)
    if surplus:
        raise EngineError(f"{int4_path} holds {len(surplus)} tensors that are not an INT4 matrix's qweight or scales "
                          f"({', '.join(surplus[:3])}{', ...' if len(surplus) > 3 else ''})")
    found = {}
    for name, shape in matrices.items():
        qweight, scales = packed[f"{name}.qweight"], packed[f"{name}.scales"]
        _check_matrix(name, shape, qweight, scales)
        found[name] = (qweight, scales)
    return found


def load_int4_state_dict(quantization: Quantization, expected: Mapping[str, tuple[int, ...]],
                         matrices: Mapping[str, tuple[int, int]]) -> dict[str, torch.Tensor]:
    """The backbone's full state dict from an INT4 folder, every tensor checked: model.safetensors must hold every
    tensor of `expected` (name -> shape, the backbone's state dict) except the matrices' weights, and nothing else;
    int4.safetensors is checked by read_int4_file. Each matrix weight is bf16(q * s)."""
    folder = quantization.folder
    weights = {f"{name}.weight" for name in matrices}
    model_path = folder / "model.safetensors"
    if not model_path.is_file():
        raise EngineError(f"{folder} has no model.safetensors (an INT4 base keeps its other tensors there)")
    with safe_open(str(model_path), "pt") as handle:
        stored = set(handle.keys())
    in_both = sorted(stored & weights)
    if in_both:
        raise EngineError(f"{model_path} holds {len(in_both)} of the INT4 matrices ({', '.join(in_both[:3])}"
                          f"{', ...' if len(in_both) > 3 else ''}); an INT4 base keeps them only in {INT4_FILE}")
    missing = sorted(set(expected) - weights - stored)
    if missing:
        raise EngineError(f"{model_path} lacks {len(missing)} tensors of the model ({', '.join(missing[:3])}"
                          f"{', ...' if len(missing) > 3 else ''})")
    extra = sorted(stored - set(expected))
    if extra:
        raise EngineError(f"{model_path} holds {len(extra)} tensors the model does not have ({', '.join(extra[:3])}"
                          f"{', ...' if len(extra) > 3 else ''})")
    state = load_file(str(model_path))
    for name, tensor in state.items():
        if tuple(tensor.shape) != tuple(expected[name]):
            raise EngineError(f"{model_path}: {name} has shape {tuple(tensor.shape)}; the model's is "
                              f"{tuple(expected[name])}")
        if tensor.dtype != torch.bfloat16:
            raise EngineError(f"{model_path}: {name} is {tensor.dtype}; every tensor of an INT4 base's "
                              "model.safetensors is bfloat16, as in its bfloat16 source base")
    for name, (qweight, scales) in read_int4_file(quantization, matrices).items():
        state[f"{name}.weight"] = dequantize(unpack_int4(qweight), scales)
    return state


def load_int4_backbone(quantization: Quantization, dtype: torch.dtype) -> torch.nn.Module:
    """The Qwen3.5 backbone of an INT4 base folder, its matrices bf16(q * s), every tensor checked
    (load_int4_state_dict), in `dtype`."""
    from transformers.models.qwen3_5.configuration_qwen3_5 import Qwen3_5Config
    from transformers.models.qwen3_5.modeling_qwen3_5 import Qwen3_5Model

    folder = quantization.folder
    config = Qwen3_5Config.from_pretrained(str(folder))
    with torch.device("meta"):
        skeleton = Qwen3_5Model(config)
    expected = {name: tuple(tensor.shape) for name, tensor in skeleton.state_dict().items()}
    matrices = int4_matrices(skeleton)
    del skeleton
    state = load_int4_state_dict(quantization, expected, matrices)
    loaded = cast(tuple[torch.nn.Module, dict[str, Any]], Qwen3_5Model.from_pretrained(
        None, config=config, state_dict=state, dtype=dtype, attn_implementation="sdpa", output_loading_info=True))
    backbone, info = loaded
    problems = {key: value for key, value in info.items() if value}
    if problems:
        raise EngineError(f"Loading the INT4 base {folder} left {problems}")
    return backbone


def scale_exponent_spans(matrices: Mapping[str, tuple[torch.Tensor, torch.Tensor]]) -> dict[str, Any]:
    """Per matrix, the largest spread of the scales' binary exponents within one row (E1's hardware limit is 7),
    and the largest over all matrices."""
    by_matrix = {}
    for name, (_, scales) in matrices.items():
        exponents = torch.floor(torch.log2(scales.float().abs()))
        by_matrix[name] = int((exponents.amax(1) - exponents.amin(1)).max())
    return {"max": max(by_matrix.values()), "by_matrix": by_matrix}


def write_int4_base(source: Path, destination: Path, matrices: Mapping[str, tuple[torch.Tensor, torch.Tensor]], *,
                    method: str, source_id: str, source_version: str, calibration: dict[str, Any],
                    gptq: dict[str, Any] | None, tool: dict[str, Any]) -> Quantization:
    """Write the INT4 base folder of the bfloat16 base `source` into the new folder `destination`: `matrices` maps
    each INT4 matrix's module name to its integers (any integer dtype, (out, in), in [-8, 7]) and bfloat16 scales
    ((out, in / 64)), and must hold exactly the matrices the base has (int4_matrices). The rest of the record
    (calibration, gptq, tool) is the caller's. Returns the written folder's checked quantization."""
    from jeff_runtime.engine.lora import weights_sha256

    if method not in METHODS:
        raise EngineError(f"Unknown method {method!r}; this runtime writes {', '.join(repr(name) for name in METHODS)}")
    if destination.exists():
        raise EngineError(f"Refusing to write the INT4 base into {destination}: it already exists")
    if read_quantization(source) is not None:
        raise EngineError(f"{source} is already an INT4 base; quantise its bfloat16 source base instead")
    for name in COPIED_FILES + ("model.safetensors", "decision_config.json"):
        if not (source / name).is_file():
            raise EngineError(f"{source} has no {name}; an INT4 base copies it from a single-file bfloat16 base")
    source_hashes = weights_sha256(source)
    if set(source_hashes) != {"model.safetensors"}:
        raise EngineError(f"{source} has its weights in {sorted(source_hashes)}; only a single model.safetensors is "
                          "supported")
    state = load_file(str(source / "model.safetensors"))
    not_bf16 = sorted(name for name, tensor in state.items() if tensor.dtype != torch.bfloat16)
    if not_bf16:
        raise EngineError(f"{source}/model.safetensors holds {len(not_bf16)} tensors that are not bfloat16 "
                          f"({', '.join(not_bf16[:3])}{', ...' if len(not_bf16) > 3 else ''}); an INT4 base is made "
                          "from a bfloat16 base")
    from transformers.models.qwen3_5.configuration_qwen3_5 import Qwen3_5Config
    from transformers.models.qwen3_5.modeling_qwen3_5 import Qwen3_5Model

    with torch.device("meta"):
        skeleton = Qwen3_5Model(Qwen3_5Config.from_pretrained(str(source)))
    shapes = int4_matrices(skeleton)
    del skeleton
    if set(matrices) != set(shapes):
        raise EngineError(f"The INT4 matrices given differ from the base's: missing "
                          f"{sorted(set(shapes) - set(matrices))[:3]}, not in the base "
                          f"{sorted(set(matrices) - set(shapes))[:3]}")
    packed: dict[str, torch.Tensor] = {}
    for name in sorted(shapes):
        q, scales = matrices[name]
        if f"{name}.weight" not in state:
            raise EngineError(f"{source}/model.safetensors has no {name}.weight")
        qweight = pack_int4(q.cpu())
        scales = scales.cpu().contiguous()
        _check_matrix(name, shapes[name], qweight, scales)
        packed[f"{name}.qweight"], packed[f"{name}.scales"] = qweight, scales
        del state[f"{name}.weight"]
    if not destination.parent.is_dir():
        raise EngineError(f"Refusing to write the INT4 base into {destination}: its parent folder does not exist")
    # Written into a hidden sibling folder and renamed into place once complete and checked, so a failed write leaves
    # no folder that looks like an INT4 base (the sibling is removed and the error raised).
    partial = destination.parent / f".{destination.name}.partial"
    if partial.exists():
        raise EngineError(f"Refusing to write the INT4 base into {destination}: {partial} exists, left by a write that "
                          "was killed; remove it first")
    partial.mkdir()
    try:
        written = _write_folder(source, partial, state, packed, matrices, method=method, source_id=source_id,
                                source_version=source_version, source_hashes=source_hashes, calibration=calibration,
                                gptq=gptq, tool=tool)
        partial.rename(destination)
    except BaseException:
        shutil.rmtree(partial)
        raise
    return dataclasses.replace(written, folder=destination)  # the folder renamed into place, not the hidden one


def _write_folder(source: Path, folder: Path, state: dict[str, torch.Tensor], packed: dict[str, torch.Tensor],
                  matrices: Mapping[str, tuple[torch.Tensor, torch.Tensor]], *, method: str, source_id: str,
                  source_version: str, source_hashes: dict[str, str], calibration: dict[str, Any],
                  gptq: dict[str, Any] | None, tool: dict[str, Any]) -> Quantization:
    """write_int4_base's files, into the empty folder `folder`, checked with read_quantization."""
    for name in COPIED_FILES:
        shutil.copyfile(source / name, folder / name)
    save_file(state, str(folder / "model.safetensors"), metadata={"format": "pt"})
    save_file(packed, str(folder / INT4_FILE), metadata={"format": "pt"})
    int4_sha256 = file_sha256(folder / INT4_FILE)
    record = {"format": FORMAT, "method": method, "group": GROUP, "symmetric": True, "scale_dtype": "bfloat16",
              "source_base": {"id": source_id, "version": source_version, "weights_sha256": source_hashes},
              "calibration": calibration, "gptq": gptq, "awq": None,
              "scale_exponent_spans": scale_exponent_spans(matrices), "tool": tool, "int4_sha256": int4_sha256}
    (folder / RECORD_FILE).write_text(json.dumps(record, indent=1) + "\n")
    config = json.loads((source / "decision_config.json").read_text())
    config["quantization"] = {"format": FORMAT, "format_id": format_id(source_id, source_version, method, int4_sha256),
                              "method": method, "source_base": {"weights_sha256": source_hashes},
                              "record": RECORD_FILE}
    (folder / "decision_config.json").write_text(json.dumps(config, indent=2) + "\n")
    written = read_quantization(folder)
    if written is None:
        raise EngineError(f"{folder}: the written decision_config.json has no quantization object")
    return written
