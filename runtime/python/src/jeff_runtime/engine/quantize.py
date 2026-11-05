"""The INT4 quantiser behind `jeff runtime quantize --method gptq` (step 2b-2 design, Sections 1 and 4).

It turns a bfloat16 base into the integers and scales of an INT4 base ("int4-g64": symmetric 4-bit integers in
[-8, 7], one bfloat16 scale per 64 inputs, for every linear layer of the text decoder's layers), with GPTQ. A port of
the prototype that measured the design (the design study's quantize.py), GPTQ only (AWQ is not
built, design Section 1); the same arithmetic, so the same inputs give the prototype's integers and scales exactly.

What GPTQ needs, and where it comes from:
- Calibration rows: `rows_per_adapter` rows of each named adapter's calibration set (never its test rows), drawn by
  `quantiser_rows`: jeff-asic's 300 temperature rows are set aside first, then the quantiser's rows are a seeded
  sample of the rest. `check_not_test_rows` refuses any drawn row that is in the adapter's test set. The caller records
  the rows' ids (quantization.json), so thresholds refitted for the INT4 format can leave them out.
- Or, with explicit training rows per adapter (`jeff runtime quantize --calibrate-on-rows`), `rows_per_adapter` rows
  drawn by `training_rows` (a seeded sample of the adapter's training file); `check_not_test_rows` and
  `check_not_calibration_rows` refuse any drawn row that is in the adapter's test or calibration set.
- Per matrix, the Hessian of its inputs, H = sum of x^T x over every token of every calibration row, computed on the
  runtime's own model with the adapter of each row active (DecisionModel + AdapterSet on the GPU: bfloat16 activations
  and LoRA, the arithmetic the standard path serves in). Layer by layer: layer i's inputs are computed through layers
  0..i-1 already quantised.

Determinism. Each row's x^T x is one float32 matrix product (TF32 off), added into a float64 sum; the rows are always
taken in the same order (the caller's, which `jeff runtime quantize` fixes); GPTQ itself runs in float64. So two runs
on the same GPU type and software give the same bytes. (Products of the per-row activations in float64 would take
hours on a GPU without fast float64; the float32 product of bfloat16 inputs is what the prototype measured.)"""

import json
import random
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import torch

from jeff.errors import EngineError
from jeff_runtime.engine.int4 import GROUP, HIGH, LOW, int4_matrices
from jeff_runtime.engine.types import DecisionInput

# The GPTQ settings (recorded in quantization.json): 1% damping, blocks of 128 columns, columns in order of
# decreasing Hessian diagonal ("activation order"), scales fixed per 64-input group from the original weights before
# GPTQ runs ("static groups"), and each group's scale chosen as the one with the least squared error among its
# largest magnitude / 7 times 1.0, 0.975, ..., 0.7 ("clip search": start, end, step).
DAMP = 0.01
BLOCK = 128
CLIPS = tuple(1.0 - 0.025 * step for step in range(13))
GPTQ_SETTINGS: dict[str, Any] = {"damp": DAMP, "block": BLOCK, "act_order": True, "static_groups": True,
                                 "clip_search": [1.0, 0.7, 0.025]}
TEMPERATURE_SEED = 20261001  # the seed of the 300 temperature rows (as jeff-asic's quantiser drew them)
TEMPERATURE_ROWS = 300
RULE = ("rows_per_adapter rows of each adapter's calibration set, never its test rows (checked: no drawn row's id "
        "or state (canonical JSON, a string state as it is) is in the test set): jeff-asic's 300 temperature rows "
        "(Random('20261001-<name>-calibration').sample) are set aside, and the quantiser's rows are "
        "Random('quant-<name>').sample(rest, rows_per_adapter), <name> being the last part of the adapter's ID; "
        "adapters in order of <name>, each adapter's rows in drawn order. The rows are listed here so that thresholds "
        "refitted for this format (jeff calibrate --base) leave them out")
TRAINING_RULE = ("rows_per_adapter rows of each adapter's training rows, from the file named for it "
                 "(--calibrate-on-rows; listed with its SHA-256 in training_rows), never its test rows nor its "
                 "calibration rows (checked: no drawn row's id or state (canonical JSON, a string state as it is) is in "
                 "either set): "
                 "Random('quant-train-<name>').sample(rows, rows_per_adapter), <name> being the last part of the "
                 "adapter's ID; adapters in order of <name>, each adapter's rows in drawn order. None of these rows "
                 "is in the calibration set, so thresholds refitted for this format (jeff calibrate --base) leave "
                 "none out")
ARITHMETIC = ("bfloat16 activations and LoRA (the runtime's standard path); each row's x^T x a float32 product, "
              "summed in float64; GPTQ in float64")


def short_name(adapter_id: str) -> str:
    """The name an adapter's calibration rows are drawn with: the last part of its ID (jeff.safety.guard -> guard)."""
    return adapter_id.rsplit(".", 1)[-1]


def quantiser_rows(name: str, rows: Sequence[dict[str, Any]], per_adapter: int) -> list[dict[str, Any]]:
    """The quantiser's `per_adapter` rows of one adapter's calibration set (RULE): the 300 temperature rows set aside
    (a seeded sample of all rows), then a seeded sample of the rest, in drawn order. Every row must have a unique
    "id"; EngineError when fewer than `per_adapter` rows remain."""
    if per_adapter < 1:
        raise EngineError(f"rows per adapter must be at least 1; got {per_adapter}")
    for number, row in enumerate(rows, 1):
        absent = [key for key in ("id", "state", "question") if key not in row]
        if absent:
            raise EngineError(f"{name}: calibration row {number} has no {', '.join(absent)}; each row needs an id, a "
                              "state and a question")
    ids = [row["id"] for row in rows]
    if len(set(ids)) != len(ids):
        repeated = sorted({row_id for row_id in ids if ids.count(row_id) > 1})
        raise EngineError(f"{name}: the calibration set repeats row ids ({', '.join(map(str, repeated[:3]))}); each "
                          "row needs its own id")
    temperature = {row["id"] for row in random.Random(f"{TEMPERATURE_SEED}-{name}-calibration").sample(
        list(rows), min(TEMPERATURE_ROWS, len(rows)))}
    rest = [row for row in rows if row["id"] not in temperature]
    if len(rest) < per_adapter:
        raise EngineError(f"{name}: only {len(rest)} calibration rows outside the {TEMPERATURE_ROWS} temperature rows; "
                          f"the quantiser needs {per_adapter}")
    return random.Random(f"quant-{name}").sample(rest, per_adapter)


def _check_row_fields(name: str, rows: Sequence[Any], which: str, source: str,
                      lines: Sequence[int] | None = None) -> None:
    """EngineError unless every row is a JSON object with an id (a string or an integer), a state and a question, and
    no two rows share an id (compared as strings). `source` names the file and `lines` each row's line number in it
    (default: 1, 2, ...), for the messages."""
    numbers = list(lines) if lines is not None else list(range(1, len(rows) + 1))
    if len(numbers) != len(rows):
        raise EngineError(f"{name}: {len(rows)} {which} rows but {len(numbers)} line numbers for {source}")
    for number, row in zip(numbers, rows, strict=True):
        if not isinstance(row, dict):
            raise EngineError(f"{name}: {source} line {number} is not a JSON object ({type(row).__name__}); each "
                              f"{which} row is an object with an id, a state and a question")
        absent = [key for key in ("id", "state", "question") if key not in row]
        if absent:
            raise EngineError(f"{name}: {source} line {number} has no {', '.join(absent)}; each {which} row needs an "
                              "id, a state and a question")
        if isinstance(row["id"], bool) or not isinstance(row["id"], (str, int)):
            raise EngineError(f"{name}: {source} line {number} has an id of type {type(row['id']).__name__}; a row id "
                              "is a string or an integer")
    seen: dict[str, int] = {}
    for number, row in zip(numbers, rows, strict=True):
        row_id = str(row["id"])
        if row_id in seen:
            raise EngineError(f"{name}: {source} repeats row id {row_id!r} (lines {seen[row_id]} and {number}); each "
                              f"{which} row needs its own id")
        seen[row_id] = number


def training_rows(name: str, rows: Sequence[Any], per_adapter: int, *, source: str,
                  lines: Sequence[int] | None = None) -> list[dict[str, Any]]:
    """The quantiser's `per_adapter` rows of one adapter's training rows (TRAINING_RULE): a seeded sample of them, in
    drawn order. `source` names the file the rows were read from and `lines` their line numbers in it, for the
    messages. Every row must be an object with a unique id (a string or an integer), a state and a question;
    EngineError when there are fewer than `per_adapter` rows."""
    if per_adapter < 1:
        raise EngineError(f"rows per adapter must be at least 1; got {per_adapter}")
    _check_row_fields(name, rows, "training", source, lines)
    if len(rows) < per_adapter:
        raise EngineError(f"{name}: only {len(rows)} training rows in {source}; the quantiser needs {per_adapter}")
    return random.Random(f"quant-train-{name}").sample(list(rows), per_adapter)


def state_key(state: Any) -> str:
    """A row's state, normalised for comparison: a string state as it is; any other state as canonical JSON (keys
    sorted, no whitespace). GPTQ sees the document, so two rows with the same state are the same input to it, whatever
    their questions."""
    if isinstance(state, str):
        return "text:" + state
    return "json:" + json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _overlap(drawn: Sequence[dict[str, Any]], others: Sequence[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """The drawn rows' ids whose id (as a string) is an id of `others`, and those whose state (state_key) is."""
    ids = {str(row["id"]) for row in others}
    states = {state_key(row["state"]) for row in others}
    return ([str(row["id"]) for row in drawn if str(row["id"]) in ids],
            [str(row["id"]) for row in drawn if state_key(row["state"]) in states])


def _problems(same_id: list[str], same_state: list[str], what: str) -> str:
    problems = []
    if same_id:
        problems.append(f"{len(same_id)} with an id the {what} set has ({', '.join(same_id[:5])})")
    if same_state:
        problems.append(f"{len(same_state)} with the state of a {what} row ({', '.join(same_state[:5])})")
    return "; ".join(problems)


def check_not_test_rows(adapter: str, drawn: Sequence[dict[str, Any]], test_rows: Sequence[dict[str, Any]], *,
                        training: bool = False) -> None:
    """Refuses (EngineError naming the adapter and the rows) any drawn calibration row (training row, with
    `training`) whose id, or whose state (state_key: the document GPTQ sees, whatever the question), is in the
    adapter's test set: the quantiser must never see a row the base is tested on."""
    same_id, same_state = _overlap(drawn, test_rows)
    if same_id or same_state:
        kind, fix = ("training", "training rows file") if training else ("calibration", "calibration set")
        raise EngineError(f"{adapter}: {kind} rows drawn for the quantiser are test rows: "
                          f"{_problems(same_id, same_state, 'test')}. The quantiser never uses test rows; fix the "
                          f"adapter's {fix}")


def check_not_calibration_rows(adapter: str, drawn: Sequence[dict[str, Any]],
                               calibration_rows: Sequence[dict[str, Any]]) -> None:
    """Refuses (EngineError naming the adapter and the rows) any drawn training row whose id, or whose state
    (state_key), is in the adapter's calibration set: rows drawn from training data must leave the calibration set
    untouched, so thresholds fitted on it see no row the quantiser saw."""
    same_id, same_state = _overlap(drawn, calibration_rows)
    if same_id or same_state:
        raise EngineError(f"{adapter}: training rows drawn for the quantiser are calibration rows: "
                          f"{_problems(same_id, same_state, 'calibration')}. Rows drawn from training data must not "
                          "be in the calibration set")


# ---------------------------------------------------------------- GPTQ on one matrix


def group_scales(w: torch.Tensor) -> torch.Tensor:
    """Each 64-input group's scale (out, in / 64), float32 holding bfloat16 values: of the candidates
    bf16(largest magnitude * c / 7) for c in CLIPS, the one whose round-to-nearest integers give the least squared
    error on the group (the first such in CLIPS order on ties). An all-zero group gets the scale 1."""
    groups = w.reshape(w.shape[0], -1, GROUP)
    top = groups.abs().amax(-1)
    best_s = _bf16(torch.where(top == 0, torch.ones_like(top), top / HIGH))
    best_err = torch.full_like(top, float("inf"))
    for clip in CLIPS:
        s = _bf16(torch.where(top == 0, torch.ones_like(top), top * clip / HIGH))
        q = torch.clamp(torch.round(groups / s[..., None]), LOW, HIGH)
        err = ((q * s[..., None] - groups) ** 2).sum(-1)
        better = err < best_err
        best_err = torch.where(better, err, best_err)
        best_s = torch.where(better, s, best_s)
    return best_s


def _bf16(x: torch.Tensor) -> torch.Tensor:
    return x.to(torch.bfloat16).float()


def gptq(w: torch.Tensor, hessian: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
    """GPTQ of one matrix w (out, in; float32) with the Hessian of its inputs (in, in): the integers (int8, in
    [-8, 7]) and the bfloat16 scales (out, in / 64). Static groups (group_scales of the original w), activation order,
    DAMP damping, blocks of BLOCK columns, all in float64. Inputs the Hessian never saw (a zero diagonal) get weight
    0. The matrix's device is used (a GPU for the real base)."""
    W = w.clone().double()
    H = hessian.clone().double()
    out_f, in_f = W.shape
    s = group_scales(w).double()
    dead = torch.diag(H) == 0
    H[dead, dead] = 1
    W[:, dead] = 0
    H += DAMP * torch.mean(torch.diag(H)) * torch.eye(in_f, device=H.device, dtype=H.dtype)
    perm = torch.argsort(torch.diag(H), descending=True)
    W, H = W[:, perm], H[perm][:, perm]
    Hinv = torch.linalg.cholesky(torch.cholesky_inverse(torch.linalg.cholesky(H)), upper=True)
    Q = torch.zeros_like(W)
    col_group = perm // GROUP
    for i1 in range(0, in_f, BLOCK):
        i2 = min(i1 + BLOCK, in_f)
        W1 = W[:, i1:i2].clone()
        Q1 = torch.zeros_like(W1)
        Err1 = torch.zeros_like(W1)
        Hinv1 = Hinv[i1:i2, i1:i2]
        for i in range(i2 - i1):
            col = W1[:, i]
            sc = s[:, col_group[i1 + i]]
            q = torch.clamp(torch.round(col / sc), LOW, HIGH) * sc
            Q1[:, i] = q
            err = (col - q) / Hinv1[i, i]
            W1[:, i:] -= err[:, None] * Hinv1[i, i:][None, :]
            Err1[:, i] = err
        Q[:, i1:i2] = Q1
        W[:, i2:] -= Err1 @ Hinv[i1:i2, i2:]
    Q = Q[:, torch.argsort(perm)].float()
    sf = s.float()
    integers = torch.round(Q.reshape(out_f, -1, GROUP) / sf[..., None]).reshape(out_f, in_f)
    if int(integers.min()) < LOW or int(integers.max()) > HIGH:
        raise EngineError(f"GPTQ gave integers from {int(integers.min())} to {int(integers.max())}, outside "
                          f"[{LOW}, {HIGH}]")
    return integers.to(torch.int8), sf.to(torch.bfloat16)


def round_to_nearest(w: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
    """Round to nearest, the design's baseline that fails the gate (Section 1: -0.64 points on v1.3): each 64-input
    group's scale bf16(largest magnitude / 7) (1 for an all-zero group), no clip search, and each weight's integer
    round(w / s) clamped to [-8, 7]. The integers (int8) and the bfloat16 scales; the prototype's rtn() exactly."""
    groups = w.float().reshape(w.shape[0], -1, GROUP)
    top = groups.abs().amax(-1)
    scales = _bf16(torch.where(top == 0, torch.ones_like(top), top / HIGH))
    integers = torch.clamp(torch.round(groups / scales[..., None]), LOW, HIGH).reshape(w.shape)
    return integers.to(torch.int8), scales.to(torch.bfloat16)


def quantize_rtn(base: Path, *, layers: int | None = None) -> dict[str, tuple[torch.Tensor, torch.Tensor]]:
    """Round to nearest (round_to_nearest) of the bfloat16 base folder `base`, on the CPU, from its
    model.safetensors: {matrix name: (integers int8, scales bfloat16)} for every INT4 matrix (int4_matrices) of the
    first `layers` decoder layers (all when None). It needs no calibration rows. `jeff runtime gate` runs it to check
    a round-to-nearest base's determinism; such a base exists only to show the gate failing."""
    from safetensors import safe_open
    from transformers.models.qwen3_5.configuration_qwen3_5 import Qwen3_5Config
    from transformers.models.qwen3_5.modeling_qwen3_5 import Qwen3_5Model

    from jeff_runtime.engine.int4 import read_quantization

    if read_quantization(base) is not None:
        raise EngineError(f"{base} is already an INT4 base; quantise its bfloat16 source base instead")
    with torch.device("meta"):
        skeleton = Qwen3_5Model(Qwen3_5Config.from_pretrained(str(base)))
    names = sorted(int4_matrices(skeleton))
    count = len(skeleton.language_model.layers) if layers is None else layers
    if not 1 <= count <= len(skeleton.language_model.layers):
        raise EngineError(f"layers must be from 1 to {len(skeleton.language_model.layers)}; got {count}")
    del skeleton
    chosen = [name for name in names if int(name.split(".")[2]) < count]
    quantized: dict[str, tuple[torch.Tensor, torch.Tensor]] = {}
    with safe_open(str(base / "model.safetensors"), "pt") as handle:
        stored = set(handle.keys())
        missing = [name for name in chosen if f"{name}.weight" not in stored]
        if missing:
            raise EngineError(f"{base}/model.safetensors has no {missing[0]}.weight")
        for name in chosen:
            quantized[name] = round_to_nearest(handle.get_tensor(f"{name}.weight"))
    return quantized


# ---------------------------------------------------------------- the calibration on the runtime's model


class _Stop(Exception):
    """Raised by a hook at the layer after the one being calibrated: nothing past it needs to run."""


def quantize_gptq(base: Path, adapters: Mapping[str, Path], rows: Sequence[tuple[str, DecisionInput]], *,
                  layers: int | None = None,
                  log: Callable[[str], None] = print) -> dict[str, tuple[torch.Tensor, torch.Tensor]]:
    """GPTQ of the bfloat16 base folder `base` on the GPU: {matrix name: (integers int8, scales bfloat16)}, on the
    CPU, for every INT4 matrix (int4_matrices) of the first `layers` decoder layers (all layers when None).

    `adapters` maps each adapter's name to its folder (each checked against the base, as for serving); `rows` are the
    calibration rows in the order they are used, each (adapter name, {"state", "question"}), run with that adapter
    active, in its own prompt layout. `log` gets one line per layer."""
    from jeff_runtime.engine.int4 import read_quantization
    from jeff_runtime.engine.lora import AdapterSet, LoraLinear
    from jeff_runtime.engine.model import DecisionModel

    if not torch.cuda.is_available():
        raise EngineError("The quantiser needs an NVIDIA GPU: it calibrates in the CUDA standard path's bfloat16 "
                          "arithmetic (torch.cuda.is_available() is False)")
    if read_quantization(base) is not None:
        raise EngineError(f"{base} is already an INT4 base; quantise its bfloat16 source base instead")
    if not rows:
        raise EngineError("The quantiser needs calibration rows; none were given")
    unknown = sorted({name for name, _ in rows} - set(adapters))
    if unknown:
        raise EngineError(f"Calibration rows name adapters that are not loaded: {', '.join(unknown)}")
    torch.cuda.reset_peak_memory_stats()
    tf32 = torch.backends.cuda.matmul.allow_tf32
    torch.backends.cuda.matmul.allow_tf32 = False
    try:
        model = DecisionModel(checkpoint=base, device="cuda")
        model.eval()
        backbone: Any = model.backbone
        shapes = int4_matrices(backbone)  # named before the adapters wrap some of them (LoraLinear)
        adapter_set = AdapterSet(model, base, precision="model")
        for name, folder in adapters.items():
            adapter_set.add_adapter(name, folder)
        decoder_layers = backbone.language_model.layers
        count = len(decoder_layers) if layers is None else layers
        if not 1 <= count <= len(decoder_layers):
            raise EngineError(f"layers must be from 1 to {len(decoder_layers)}; got {count}")
        layout: dict[str, str] = {}
        for name in adapters:
            chosen = adapter_set.heads[name].prompt_layout or adapter_set.heads[None].prompt_layout
            if chosen is None:
                raise EngineError(f"{name}: neither the adapter nor the base names a prompt layout")
            layout[name] = chosen
        batches = [(name, model.prepare([row], layout=layout[name])) for name, row in rows]

        def linear(full: str) -> torch.nn.Linear:
            module = backbone.get_submodule(full)
            return module.base if isinstance(module, LoraLinear) else module

        quantized: dict[str, tuple[torch.Tensor, torch.Tensor]] = {}
        with torch.no_grad():
            for i in range(count):
                started = time.time()
                prefix = f"language_model.layers.{i}."
                names = sorted(name for name in shapes if name.startswith(prefix))
                hessians = _hessians(model, adapter_set, backbone, decoder_layers, i, names, batches)
                for full in names:
                    weight = linear(full).weight
                    integers, scales = gptq(weight.float(), hessians.pop(full))
                    product = integers.float().reshape(integers.shape[0], -1, GROUP) * scales.float()[..., None]
                    weight.copy_(product.reshape(weight.shape).to(weight.dtype))
                    quantized[full] = (integers.cpu(), scales.cpu())
                torch.cuda.empty_cache()
                log(f"layer {i:2d}: {len(names)} matrices, {time.time() - started:6.1f} s")
    finally:
        torch.backends.cuda.matmul.allow_tf32 = tf32
    return quantized


def _hessians(model: Any, adapter_set: Any, backbone: Any, decoder_layers: Any, i: int, names: list[str],
              batches: Sequence[tuple[str, Any]]) -> dict[str, torch.Tensor]:
    """Per matrix of layer i, sum over the calibration rows of x^T x (float64), x its inputs (one row per token):
    every row run, with its adapter active, up to layer i + 1 (a hook stops it there)."""
    hessians: dict[str, torch.Tensor] = {}
    hooks = []
    for full in names:
        def capture(module: Any, args: tuple[Any, ...], full: str = full) -> None:
            x = args[0].reshape(-1, args[0].shape[-1]).float()
            if full not in hessians:
                hessians[full] = torch.zeros(x.shape[1], x.shape[1], device=x.device, dtype=torch.float64)
            hessians[full] += (x.T @ x).double()
        hooks.append(backbone.get_submodule(full).register_forward_pre_hook(capture))
    if i + 1 < len(decoder_layers):
        def stop(module: Any, args: tuple[Any, ...]) -> None:
            raise _Stop
        hooks.append(decoder_layers[i + 1].register_forward_pre_hook(stop))
    try:
        for name, batch in batches:
            adapter_set.use(name)
            try:
                model(batch)
            except _Stop:
                pass
    finally:
        for hook in hooks:
            hook.remove()
    missing = sorted(set(names) - set(hessians))
    if missing:
        raise EngineError(f"layer {i}: the calibration rows never reached {', '.join(missing)}")
    return hessians
