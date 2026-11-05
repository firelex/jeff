"""The INT4 accuracy gate (step 2b-2 design, Section 6): what it requires, how it judges, and how serving checks it.

`jeff runtime gate` (jeff.gate_run) measures an INT4 base against the bfloat16 base it was quantised from, for a set
of adapters, and writes the record, gate.json, into the INT4 folder. Nothing serves an INT4 base until that record
passes: `jeff runtime serve` calls check_served at start and on every `serve` and `reload`, and refuses the base, or
any adapter on it, that gate.json does not record as passing.

What passes (the step 2 spec's gate, design Section 6), per adapter and over the adapters:
- accuracy on every published test row, in engine mode (each row's own state and question) and in template mode
  (each row rebuilt as the call a user makes), on the same rows in both bases: the mean change over the adapters at
  least MEAN_LIMIT points, and no adapter's change below ADAPTER_LIMIT points, in each mode;
- the fast path against the standard path on the INT4 base, on up to AGREEMENT_ROWS test rows per adapter (all that
  took the fast path when fewer): the same top option on at least AGREEMENT of them. A row whose top two
  standard-path options are exactly equal agrees when the fast path picks either (the 2b-1 gate's tie rule);
- every calibrated function's threshold refitted on the INT4 base reaches its target precision;
- the quantiser is deterministic: quantising the first layers twice gives the same bytes.

The arithmetic is exact (fractions of whole counts), so a change of exactly -0.5 points passes and one row less does
not. Nothing here needs a GPU."""

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from jeff.errors import EngineError

GATE_FILE = "gate.json"
GATE_FORMAT = "jeff-int4-gate"
GATE_VERSION = 1
MEAN_LIMIT = Fraction(-3, 10)  # points: the mean accuracy change over the adapters, in each mode
ADAPTER_LIMIT = Fraction(-1, 2)  # points: any one adapter's accuracy change, in each mode
AGREEMENT = Fraction(99, 100)  # the fast path's share of the standard path's top options, per adapter
AGREEMENT_ROWS = 1000  # test rows per adapter compared (all that took the fast path when fewer)
SEED = 7  # the agreement rows' sample and the order the test rows are served in
MODES = ("engine", "template")


def points(value: Fraction) -> str:
    return f"{float(value):+.3f}"


@dataclass(frozen=True)
class Counts:
    """One adapter in one mode: the rows scored on both bases (rows refused as too long on either are left out of
    both) and how many each base got right."""
    rows: int
    reference: int
    int4: int

    def change(self) -> Fraction:
        """The INT4 base's accuracy minus the reference's, in points (hundredths)."""
        if self.rows < 1:
            raise EngineError("an accuracy change needs at least one row scored on both bases")
        return Fraction(100 * (self.int4 - self.reference), self.rows)


@dataclass(frozen=True)
class Agreement:
    """The fast path against the standard path on one adapter's agreement rows (agreement_counts)."""
    compared: int
    agreeing: int  # with the tie rule
    raw_agreeing: int  # top options equal
    exact_ties: int  # rows whose top two standard-path options are exactly equal
    largest_difference: float  # the largest probability difference on any row


@dataclass(frozen=True)
class AdapterMeasures:
    """What the gate measured for one adapter. calibration_misses: each calibrated function whose INT4 refit reached
    no threshold at its target precision, with the reason."""
    engine: Counts
    template: Counts
    agreement: Agreement
    calibration_misses: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Verdict:
    passed: bool
    adapters: dict[str, list[str]]  # per adapter (id@version), why it fails; empty when it passes
    overall: list[str]  # why the set fails as a whole (a mean change, determinism); empty when it passes
    mean_change: dict[str, Fraction]  # per mode


def top(probabilities: Sequence[float]) -> int:
    return max(range(len(probabilities)), key=probabilities.__getitem__)


def agreement_counts(pairs: Iterable[tuple[Sequence[float], Sequence[float]]]) -> Agreement:
    """(standard-path probabilities, fast-path probabilities) per row, counted with the 2b-1 gate's tie rule: a row
    agrees when the fast path's top option is the standard path's, or is one of its exactly equal top options."""
    compared = agreeing = raw = ties = 0
    largest = 0.0
    for standard, fast in pairs:
        if len(standard) != len(fast):
            raise EngineError(f"the standard path gave {len(standard)} probabilities and the fast path {len(fast)} "
                              "for one row")
        compared += 1
        largest = max(largest, max(abs(a - b) for a, b in zip(standard, fast, strict=True)))
        tied = [option for option, p in enumerate(standard) if p == max(standard)]
        ties += len(tied) > 1
        if top(fast) == top(standard):
            agreeing += 1
            raw += 1
        elif top(fast) in tied:
            agreeing += 1
    return Agreement(compared, agreeing, raw, ties, largest)


ECE_BINS = 15


def calibration_error(pairs: Sequence[tuple[float, bool]], bins: int = ECE_BINS) -> float:
    """Expected calibration error of (confidence, correct) pairs: over `bins` equal-width bins of confidence, the
    gap between each bin's accuracy and its mean confidence, weighted by the bin's share of the rows."""
    if not pairs:
        raise EngineError("calibration error needs at least one row")
    grouped: dict[int, list[tuple[float, bool]]] = {}
    for confidence, ok in pairs:
        grouped.setdefault(min(int(confidence * bins), bins - 1), []).append((confidence, ok))
    return sum(abs(sum(ok for _, ok in members) / len(members) - sum(c for c, _ in members) / len(members))
               * len(members) for members in grouped.values()) / len(pairs)


def judge(measures: Mapping[str, AdapterMeasures], determinism: Sequence[str]) -> Verdict:
    """The gate's verdict on per-adapter measures (keyed id@version). determinism: why the quantiser's determinism
    check failed (empty when it passed)."""
    if not measures:
        raise EngineError("the gate needs at least one adapter")
    failures: dict[str, list[str]] = {key: [] for key in measures}
    for key, measured in measures.items():
        for mode in MODES:
            change = getattr(measured, mode).change()
            if change < ADAPTER_LIMIT:
                failures[key].append(f"{mode}-mode accuracy change {points(change)} points, below "
                                     f"{points(ADAPTER_LIMIT)}")
        agreement = measured.agreement
        if agreement.compared == 0:
            failures[key].append("agreement: no test row took the fast path")
        elif agreement.agreeing < AGREEMENT * agreement.compared:
            failures[key].append(f"agreement {agreement.agreeing}/{agreement.compared} "
                                 f"({agreement.agreeing / agreement.compared:.2%}), below {float(AGREEMENT):.0%}")
        for function, reason in sorted(measured.calibration_misses.items()):
            failures[key].append(f"calibration refit: {function} {reason}")
    overall: list[str] = []
    means: dict[str, Fraction] = {}
    for mode in MODES:
        means[mode] = sum((getattr(m, mode).change() for m in measures.values()), Fraction(0)) / len(measures)
        if means[mode] < MEAN_LIMIT:
            overall.append(f"mean {mode}-mode accuracy change {points(means[mode])} points over {len(measures)} "
                           f"adapters, below {points(MEAN_LIMIT)}")
    overall.extend(f"determinism: {reason}" for reason in determinism)
    passed = not overall and not any(failures.values())
    return Verdict(passed, failures, overall, means)


# ---------------------------------------------------------------- serving: gate.json against the base and adapters


@dataclass(frozen=True)
class Int4Identity:
    """Which exact INT4 base a folder holds: its format_id (decision_config.json), the sha256 of its
    int4.safetensors (the record, which the engine checks the file against when it loads) and its source base (id,
    version), from the record."""
    folder: Path
    format_id: str
    int4_sha256: str
    source: tuple[str, str]


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EngineError(f"{path}: cannot read it as JSON ({error})") from error


def int4_identity(folder: Path) -> Int4Identity | None:
    """The INT4 identity of a base folder, or None for a bfloat16 base (no quantization in its decision_config.json).
    Reads the two JSON files only; the engine checks the tensors when it loads them."""
    config = _json(folder / "decision_config.json")
    if not isinstance(config, dict):
        raise EngineError(f"{folder / 'decision_config.json'} does not hold a JSON object")
    if "quantization" not in config:
        return None
    quantization = config["quantization"]
    if not isinstance(quantization, dict) or not isinstance(quantization.get("format_id"), str) \
            or not isinstance(quantization.get("record"), str):
        raise EngineError(f"{folder / 'decision_config.json'}: its quantization names no format_id and record; it is "
                          "not an INT4 base as jeff runtime quantize writes it")
    record_path = folder / quantization["record"]
    record = _json(record_path)
    source = record.get("source_base") if isinstance(record, dict) else None
    if not isinstance(record, dict) or not isinstance(record.get("int4_sha256"), str) or not isinstance(source, dict) \
            or not isinstance(source.get("id"), str) or not isinstance(source.get("version"), str):
        raise EngineError(f"{record_path}: no int4_sha256 and source_base id and version; it is not an INT4 base's "
                          "record")
    return Int4Identity(folder, quantization["format_id"], record["int4_sha256"], (source["id"], source["version"]))


def gate_command(folder: Path, adapter_ids: Iterable[str]) -> str:
    ids = " ".join(sorted(set(adapter_ids))) or "<the adapters to serve>"
    return (f"jeff runtime gate --base {folder} --reference <the bfloat16 base it was quantised from> "
            f"--adapters {ids}")


def read_gate(path: Path) -> dict[str, Any]:
    """A gate.json, checked for the fields serving reads: EngineError naming the file otherwise."""
    gate = _json(path)
    problem = None
    if not isinstance(gate, dict) or gate.get("format") != GATE_FORMAT or gate.get("version") != GATE_VERSION:
        problem = f"not a {GATE_FORMAT} file of version {GATE_VERSION}"
    elif not isinstance(gate.get("passed"), bool) or not isinstance(gate.get("overall_failures"), list):
        problem = "no passed and overall_failures"
    elif not isinstance(gate.get("base"), dict) or not all(
            isinstance(gate["base"].get(key), str) for key in ("format_id", "int4_sha256")):
        problem = "no base format_id and int4_sha256"
    elif not isinstance(gate.get("adapters"), dict) or not all(
            isinstance(entry, dict) and isinstance(entry.get("passed"), bool)
            and isinstance(entry.get("failures"), list) for entry in gate["adapters"].values()):
        problem = "no adapters entries, each with passed and failures"
    elif any(entry["passed"] != (entry["failures"] == []) for entry in gate["adapters"].values()):
        problem = "an adapter entry's passed disagrees with its failures (passed exactly when it lists none)"
    elif gate["passed"] != (gate["overall_failures"] == [] and all(
            entry["failures"] == [] for entry in gate["adapters"].values())):
        problem = ("passed disagrees with the failures it records (passed exactly when overall_failures and every "
                   "adapter's failures are empty)")
    if problem is not None:
        raise EngineError(f"{path}: {problem}; refusing to serve the INT4 base on it")
    return gate


@dataclass(frozen=True)
class GatedBase:
    """An INT4 base being served (jeff runtime serve): its identity, and the version of each adapter the engine may
    serve that is not in an adapters dir (from the catalogue or --path), by adapter ID. Versions of adapters found
    in an adapters dir come from their own manifests."""
    identity: Int4Identity
    fixed_versions: Mapping[str, str]


def check_served(identity: Int4Identity, adapters: Mapping[str, str]) -> None:
    """Refuses (EngineError naming each adapter, why, and the command to run) to serve the INT4 base `identity` with
    `adapters` (adapter ID -> version; empty for the base alone) unless the folder's gate.json is this exact base's
    (format_id and int4 sha256), passed as a whole, and records a passing entry for every adapter at that version."""
    folder = identity.folder
    path = folder / GATE_FILE
    names = ", ".join(f"{adapter_id}@{version}" for adapter_id, version in sorted(adapters.items())) \
        or "the base alone"
    command = gate_command(folder, adapters)
    if not path.is_file():
        raise EngineError(f"The INT4 base {folder} ({identity.format_id}) has no {GATE_FILE}: nothing serves an INT4 "
                          f"base until it passes its accuracy gate. To serve {names} on it, run on this GPU type: "
                          f"{command}")
    gate = read_gate(path)
    rerun = f"move {path} aside and run: {command}"
    base = gate["base"]
    if (base["format_id"], base["int4_sha256"]) != (identity.format_id, identity.int4_sha256):
        raise EngineError(f"{path} is the gate of {base['format_id']} (int4.safetensors "
                          f"{base['int4_sha256'][:12]}...), "
                          f"not of this base, {identity.format_id} ({identity.int4_sha256[:12]}...): it belongs to "
                          f"another INT4 base. To serve {names}, {rerun}")
    if not gate["passed"]:
        reasons = [*gate["overall_failures"],
                   *(f"{key}: {'; '.join(entry['failures'])}" for key, entry in sorted(gate["adapters"].items())
                     if entry["failures"])]
        raise EngineError(f"The INT4 base {identity.format_id} failed its gate ({'; '.join(reasons)}), so it serves "
                          f"no adapter: refusing {names}. Quantise again (jeff runtime quantize --method gptq, with "
                          f"more --rows-per-adapter for the adapter that misses: design Risk 1) and gate the new "
                          f"folder ({gate_command(Path('<new INT4 folder>'), adapters)}), or serve the bfloat16 base")
    entries: dict[str, dict[str, Any]] = gate["adapters"]
    problems = []
    for adapter_id, version in sorted(adapters.items()):
        key = f"{adapter_id}@{version}"
        if key in entries:  # passed: read_gate holds a passed gate to every entry passing
            continue
        others = sorted(name for name in entries if name.rsplit("@", 1)[0] == adapter_id)
        if others:
            problems.append(f"{key} is not in the gate of {identity.format_id}, which gated {', '.join(others)} "
                            "(another version)")
        else:
            problems.append(f"{key} is not in the gate of {identity.format_id} (it gated "
                            f"{', '.join(sorted(entries))})")
    if problems:
        raise EngineError(f"Refusing to serve on the INT4 base {folder}: {'; '.join(problems)}. Every adapter served "
                          f"on an INT4 base must pass its gate there; {rerun}")
