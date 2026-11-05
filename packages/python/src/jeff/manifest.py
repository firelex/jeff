"""Adapter manifests: what an adapter is, its context and input, and the functions callers call."""

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from jeff import contract
from jeff.errors import ManifestError


CODE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_NOT_CODE = re.compile(r"[^a-z0-9]+")
_VERSION_PART = re.compile(r"v[0-9]+")  # matched with fullmatch, so "v14" and a line break is not one


def _version_parts(adapter_id: str) -> tuple[str, list[str], list[int]]:
    """The ID's prefix (jeff.<domain>), its name's hyphen-separated segments, and the indexes of those that are a
    version part (v and digits, the whole segment)."""
    prefix, _, name = adapter_id.rpartition(".")
    segments = name.split("-")
    return prefix, segments, [index for index, segment in enumerate(segments) if _VERSION_PART.fullmatch(segment)]


def unversioned_id(adapter_id: str) -> str | None:
    """The adapter ID without its version part (contract/schemas/manifest.schema.json, aliases): the one
    hyphen-separated segment of its name (the part after the second dot) that is v and digits, removed with one
    hyphen, as jeff.routing.router-open-v14 -> jeff.routing.router-open. None when the name has no such segment,
    more than one, or nothing besides it."""
    prefix, segments, versions = _version_parts(adapter_id)
    if len(versions) != 1 or len(segments) == 1:
        return None
    del segments[versions[0]]
    return f"{prefix}.{'-'.join(segments)}"


def _check_aliases(where: str, adapter_id: str, aliases: Sequence[str]) -> None:
    """No alias is listed twice, and an alias of the ID form (the schema allows it as jeff.<domain>.<name>) must be
    the adapter's unversioned ID."""
    seen: set[str] = set()
    for alias in aliases:
        if alias in seen:
            raise ManifestError(f"{where}: the alias {alias} is listed twice")
        seen.add(alias)
        if not alias.startswith("jeff."):
            continue
        _, segments, versions = _version_parts(adapter_id)
        form = (f"{where}: the alias {alias} has the form of an adapter ID, which only the adapter's ID without its "
                "version part may have, and ")
        if not versions:
            raise ManifestError(f"{form}{adapter_id} has no version part (one segment v<digits> in its name besides "
                                "others, as in jeff.routing.router-open-v14)")
        if len(versions) > 1:
            raise ManifestError(f"{form}{adapter_id} has more than one version part "
                                f"({', '.join(segments[index] for index in versions)}), so it has no ID without one")
        if len(segments) == 1:
            raise ManifestError(f"{form}the name of {adapter_id} is only a version part, so it has no ID without one")
        expected = unversioned_id(adapter_id)
        if alias != expected:
            raise ManifestError(f"{where}: the alias {alias} has the form of an adapter ID, so it must be {expected}, "
                                f"the ID {adapter_id} without its version part")


def code_name_of(key: str) -> str:
    """The name callers use for a field whose key is `key`: a key that already is a code name (^[a-z][a-z0-9_]*$) is
    its own, unchanged; any other key is lower-cased, every run of characters other than a-z and 0-9 replaced by one
    underscore, underscores trimmed from both ends. That may still not be a code name (empty, or starting with a
    digit); the manifest then declares one."""
    if CODE_NAME.fullmatch(key):
        return key
    return _NOT_CODE.sub("_", key.lower()).strip("_")


@dataclass(frozen=True)
class ContextField:
    key: str  # exactly as trained, and as the prompt shows it
    description: str
    required: bool
    code_name: str  # what callers pass it as


@dataclass(frozen=True)
class InputField:
    """The input a caller passes with every call. form "fields": the state Jeff sends is an object, the bound context
    and then the input under `key`; form "text": the state is the input string itself, unwrapped (the adapter was
    trained on a plain-text state), and there is no context."""
    key: str  # with form fields, exactly as trained and as the prompt shows it; with form text, only names the input
    description: str
    code_name: str
    form: Literal["fields", "text"]


@dataclass(frozen=True)
class LabelSpec:
    source: Literal["fixed", "caller", "subset", "describe"]
    fixed: dict[str, str | None] = field(default_factory=dict)
    levels: tuple[str, ...] = ()
    fixed_first: dict[str, str | None] = field(default_factory=dict)
    caller_keys: str | None = None
    max: int | None = None
    required: tuple[str, ...] = ()
    keys: tuple[str, ...] = ()
    # fixed labels of a choice function: whether the caller may offer a subset of them (None everywhere else)
    subset: bool | None = None


@dataclass(frozen=True)
class Calibration:
    data: str
    rows: int
    target_precision: float
    threshold: float
    precision_at_threshold: float
    coverage_at_threshold: float
    # {"path", "sha256"} of the JEFF_DATA_MIRROR file the rows were read from instead of `data`; None (absent from
    # the block): read from `data` itself
    data_mirror: dict[str, str] | None = field(default=None, kw_only=True)


@dataclass(frozen=True)
class FormatCalibration(Calibration):
    """A function's calibration on one INT4 base (an entry of calibration_by_format): `excluded` says which
    calibration rows were left out of the fit, and why."""
    excluded: str


@dataclass(frozen=True)
class FunctionSpec:
    adapter_id: str
    key: str
    interface: str
    type: Literal["choice", "noul", "score"]
    instructions: str
    labels: LabelSpec
    abstain_labels: tuple[str, ...]
    calibration: Calibration | None
    # the request shape the function takes: its own context and input when its manifest entry declares them (an
    # adapter trained on several request shapes), else the adapter's, resolved once when the manifest is loaded
    context: tuple[ContextField, ...]
    input: InputField
    # format_id of an INT4 base -> the function's calibration on it; `calibration` is the bfloat16 base's
    calibration_by_format: dict[str, FormatCalibration] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.adapter_id}.{self.key}"


def calibration_for(function: FunctionSpec, format_id: str | None) -> Calibration | None:
    """The calibration a function decides with on a base of `format_id` (resolve_calibration over its manifest's
    blocks)."""
    return resolve_calibration(function.id, function.calibration, function.calibration_by_format, format_id)


def resolve_calibration(function_id: str, calibration: Calibration | None,
                        by_format: Mapping[str, FormatCalibration], format_id: str | None) -> Calibration | None:
    """The calibration a function decides with on a base of `format_id`, from its bfloat16 block `calibration` and
    its calibration_by_format entries `by_format`: `calibration` for a bfloat16 base (format_id None; None when the
    function is uncalibrated). On an INT4 base an uncalibrated function stays uncalibrated; a calibrated one needs its
    entry for exactly that format_id, and without one this raises ManifestError naming the function, the format and
    the command that writes the entry (the bfloat16 threshold is never used in its place)."""
    if format_id is None or calibration is None:
        return calibration
    if format_id not in by_format:
        calibrated_on = ", ".join(by_format) or "no INT4 base"
        raise ManifestError(f"{function_id} has no calibration for the INT4 base this engine serves ({format_id}); "
                            f"it is calibrated on the bfloat16 base and on {calibrated_on}. Fit its threshold on "
                            f"this base with `jeff calibrate {function_id} --base <the INT4 base folder>` (for an "
                            "adapter served from an adapter folder, `jeff calibrate <that folder> --base <the INT4 "
                            "base folder>`, which writes into the folder's manifest.yaml)")
    return by_format[format_id]


def has_threshold(function: FunctionSpec, format_id: str | None) -> bool:
    """Whether `function` has a threshold for a base of `format_id` (None: bfloat16): its `calibration` block on a
    bfloat16 base, its calibration_by_format entry for exactly that format_id on an INT4 one. False is no threshold
    for that base: uncalibrated, or, on an INT4 base, calibrated on other bases only (which calibration_for
    refuses)."""
    if format_id is None:
        return function.calibration is not None
    return format_id in function.calibration_by_format


@dataclass(frozen=True)
class Manifest:
    id: str
    version: str
    aliases: tuple[str, ...]
    licence_kind: Literal["open", "proprietary"]
    weights: dict[str, Any]
    base_id: str
    base_version: str
    max_options: int
    context: tuple[ContextField, ...]
    input: InputField
    functions: dict[str, FunctionSpec]
    routes_to: tuple[str, ...]
    tests: dict[str, Any]
    raw: dict[str, Any]
    path: Path

    @property
    def is_router(self) -> bool:
        return bool(self.routes_to)


def load_manifest(path: Path) -> Manifest:
    return manifest_from_dict(yaml.safe_load(path.read_text(encoding="utf-8")), path)


def _labels(raw: dict[str, Any]) -> LabelSpec:
    return LabelSpec(source=raw["source"], fixed=dict(raw.get("fixed") or {}), levels=tuple(raw.get("levels") or ()),
                     fixed_first=dict(raw.get("fixed_first") or {}), caller_keys=raw.get("caller_keys"),
                     max=raw.get("max"), required=tuple(raw.get("required") or ()), keys=tuple(raw.get("keys") or ()),
                     subset=raw.get("subset"))


def _check_function(spec: FunctionSpec, max_options: int, where: str) -> None:
    interfaces = contract.interfaces()
    name = f"{where}: function {spec.key}"
    if spec.interface not in interfaces:
        raise ManifestError(f"{name} names interface {spec.interface!r}, which is not in the interface catalogue "
                            f"({', '.join(interfaces)})")
    wanted = interfaces[spec.interface].question_type
    if spec.type != wanted:
        raise ManifestError(f"{name} has type {spec.type} but interface {spec.interface} compiles to {wanted}")
    labels = spec.labels
    if spec.type != "choice" and (labels.required or labels.keys):
        raise ManifestError(f"{name}: only choice functions have 'required' or 'keys'")
    if spec.type == "choice":
        if labels.levels:
            raise ManifestError(f"{name}: choice functions have no levels")
        if labels.source == "fixed" and not labels.fixed:
            raise ManifestError(f"{name}: fixed labels need a non-empty 'fixed'")
        if labels.source == "caller" and labels.max is None:
            raise ManifestError(f"{name}: caller labels need 'max', the most labels the adapter was trained on")
        if labels.required and labels.source != "subset":
            raise ManifestError(f"{name}: only subset labels have 'required'")
        if labels.keys and labels.source != "describe":
            raise ManifestError(f"{name}: only describe labels have 'keys'")
        if labels.source == "subset":
            if not labels.fixed or labels.fixed_first or labels.caller_keys or labels.max is not None:
                raise ManifestError(f"{name}: subset labels need a non-empty 'fixed' (every allowed label) and no "
                                    f"fixed_first, caller_keys or max")
            stray = [key for key in labels.required if key not in labels.fixed]
            if stray:
                raise ManifestError(f"{name}: required labels {stray} are not in 'fixed' ({list(labels.fixed)})")
        if labels.source == "describe":
            if not labels.keys or labels.fixed or labels.fixed_first or labels.caller_keys or labels.max is not None:
                raise ManifestError(f"{name}: describe labels need non-empty 'keys' and no fixed, fixed_first, "
                                    f"caller_keys or max")
            if len(set(labels.keys)) != len(labels.keys):
                raise ManifestError(f"{name}: describe keys {list(labels.keys)} repeat a key")
        count = len(labels.fixed) + len(labels.fixed_first) + (labels.max or 0) + len(labels.keys)
        if count > max_options:
            raise ManifestError(f"{name} allows {count} options, more than the base's max_options {max_options}")
        values = interfaces[spec.interface].values
        if values is not None and tuple(labels.fixed) != values:
            raise ManifestError(f"{name}: interface {spec.interface} requires labels {list(values)}, "
                                f"got {list(labels.fixed)}")
    elif spec.type == "score":
        if len(labels.levels) < 2 or labels.fixed or labels.source != "fixed":
            raise ManifestError(f"{name}: score functions need fixed 'levels' (2 to 10) and nothing else")
    else:
        if labels.levels or set(labels.fixed) - {"true", "false"} or labels.source != "fixed":
            raise ManifestError(f"{name}: noul functions may only describe 'true' and 'false' in 'fixed'")
    known = set(labels.fixed) | set(labels.fixed_first) | set(labels.keys)
    unknown = [label for label in spec.abstain_labels if label not in known]
    if unknown:
        raise ManifestError(f"{name}: abstain_labels {unknown} are not among its labels {sorted(known)}")


def _check_finite(value: Any, pointer: str, where: str) -> None:
    """Refuses the first number in the manifest that is not finite (nan, inf, -inf, which YAML's .nan and .inf give),
    by its place (a JSON pointer) and value. JSON has no such number, and a calibration threshold of nan would turn
    calibration off without a word: `confidence < nan` is always false. The walk keeps its own stack, so a deep
    manifest cannot exhaust Python's recursion limit here (the schema check refuses it by name)."""
    pending: list[tuple[Any, str]] = [(value, pointer)]
    while pending:
        item, at = pending.pop()
        if isinstance(item, float) and not math.isfinite(item):
            raise ManifestError(f"{where}: {at or '/'} is {item!r}, not a finite number, which JSON cannot hold; "
                                "write a finite number")
        if isinstance(item, dict):
            pending.extend(reversed([(child, f"{at}/{key}") for key, child in item.items()]))
        elif isinstance(item, list):
            pending.extend(reversed([(child, f"{at}/{index}") for index, child in enumerate(item)]))


def manifest_from_dict(raw: dict[str, Any], path: Path) -> Manifest:
    where = str(path)
    _check_finite(raw, "", where)
    contract.validate("manifest", raw, where)
    adapter_id = raw["id"]
    _check_aliases(where, adapter_id, raw["aliases"])
    context, input = request_fields(where, raw["context"], raw["input"])
    functions: dict[str, FunctionSpec] = {}
    for key, body in raw["functions"].items():
        calibration = Calibration(**body["calibration"]) if body["calibration"] else None
        by_format = {format_id: FormatCalibration(**entry)
                     for format_id, entry in body.get("calibration_by_format", {}).items()}
        if by_format and calibration is None:
            raise ManifestError(f"{where}: function {key} has calibration_by_format entries ({', '.join(by_format)}) "
                                "but calibration null; a function uncalibrated on the bfloat16 base stays "
                                "uncalibrated in every format")
        # the schema makes a function declare both context and input, or neither
        own = request_fields(f"{where}: function {key}", body["context"], body["input"]) if "input" in body \
            else (context, input)
        spec = FunctionSpec(adapter_id, key, body["interface"], body["type"], body["instructions"],
                            _labels(body["labels"]), tuple(body["abstain_labels"]), calibration, *own, by_format)
        _check_function(spec, raw["base"]["max_options"], where)
        functions[key] = spec
    if raw["tests"]["function_by"] == "question_type":
        types = [spec.type for spec in functions.values()]
        if len(set(types)) != len(types):
            raise ManifestError(f"{where}: tests.function_by question_type needs one function per question type; "
                                f"the functions have types {types}")
    _check_kinds(where, raw["tests"], functions)
    _check_id_patterns(where, raw["tests"], functions)
    routes_to = tuple(raw["routes_to"])
    if routes_to:
        only = list(functions.values())
        if len(only) != 1 or only[0].interface != "route" or only[0].labels.source != "subset" \
                or tuple(only[0].labels.fixed) != routes_to:
            raise ManifestError(f"{where}: a router has exactly one route function with subset labels whose keys "
                                f"equal routes_to {list(routes_to)} (dispatch offers the targets that lead to the "
                                "requested interface)")
        if only[0].labels.required:
            raise ManifestError(f"{where}: a router's labels have no required keys; dispatch offers only the "
                                "targets that lead to the requested interface")
    return Manifest(
        id=adapter_id, version=raw["version"], aliases=tuple(raw["aliases"]), licence_kind=raw["licence"]["kind"],
        weights=raw["weights"], base_id=raw["base"]["id"], base_version=raw["base"]["version"],
        max_options=raw["base"]["max_options"], context=context, input=input, functions=functions,
        routes_to=routes_to, tests=raw["tests"], raw=raw, path=path)


def request_fields(where: str, raw_context: list[Mapping[str, Any]], raw_input: Mapping[str, Any]
                   ) -> tuple[tuple[ContextField, ...], InputField]:
    """A context list and an input block (schema-checked; the adapter's, or a function's own) as fields, refused with
    ManifestError when keys repeat, the input key is also a context key, two fields share a code name, or a text input
    has context."""
    context = tuple(ContextField(item["key"], item["description"], item["required"], _code_name(where, item))
                    for item in raw_context)
    keys = [item.key for item in context]
    if len(set(keys)) != len(keys) or raw_input["key"] in keys:
        raise ManifestError(f"{where}: context keys must be unique and differ from the input key ({keys}, "
                            f"input {raw_input['key']})")
    input = input_field(where, raw_input, context)
    _check_code_names(where, [*((field.key, field.code_name) for field in context), (input.key, input.code_name)])
    return context, input


def input_field(where: str, raw: Mapping[str, Any], context: tuple[ContextField, ...]) -> InputField:
    """An input block (schema-checked) as an InputField. A plain-text input is the whole state the adapter saw, so
    there is nowhere for context to go: a manifest with form text and context fields is refused."""
    if raw["form"] == "text" and context:
        raise ManifestError(f"{where}: input {raw['key']!r} has form text (the state is the plain input string), so "
                            f"there can be no context fields, but it has {[field.key for field in context]}")
    return InputField(raw["key"], raw["description"], _code_name(where, raw), raw["form"])


def _code_name(where: str, field: Mapping[str, Any]) -> str:
    if "code_name" in field:
        return str(field["code_name"])
    code = code_name_of(field["key"])
    if not CODE_NAME.fullmatch(code):
        raise ManifestError(f"{where}: field {field['key']!r} gives no code name by the rule (lower-case, other "
                            f"characters to '_': {code!r}, which is not of the form {CODE_NAME.pattern}); declare "
                            "its code_name")
    return code


def _check_code_names(where: str, fields: list[tuple[str, str]]) -> None:
    by_code: dict[str, list[str]] = {}
    for key, code in fields:
        by_code.setdefault(code, []).append(key)
    shared = {code: keys for code, keys in by_code.items() if len(keys) > 1}
    if shared:
        raise ManifestError(f"{where}: fields share a code name, which callers could not tell apart: "
                            + "; ".join(f"{', '.join(repr(key) for key in keys)} -> {code}"
                                        for code, keys in shared.items())
                            + "; declare a distinct code_name for each")


def _criteria(function_by: str | list[str]) -> list[str]:
    """tests.function_by as its list of criteria (a single value is the list holding only it)."""
    return [function_by] if isinstance(function_by, str) else list(function_by)


def _check_kinds(where: str, tests: Mapping[str, Any], functions: Mapping[str, FunctionSpec]) -> None:
    if "kinds" not in tests:
        return
    if "source_kind" not in _criteria(tests["function_by"]):
        raise ManifestError(f"{where}: tests.kinds maps source.kind values to functions, so tests.function_by "
                            f"must use source_kind (it is {tests['function_by']!r})")
    named = [key for value in tests["kinds"].values() for key in ([value] if isinstance(value, str) else value)]
    stray = sorted({key for key in named if key not in functions})
    if stray:
        raise ManifestError(f"{where}: tests.kinds names {stray}, which are not its functions ({list(functions)})")
    several = [kind for kind, value in tests["kinds"].items() if not isinstance(value, str)]
    if several and _criteria(tests["function_by"]) == ["source_kind"]:
        raise ManifestError(f"{where}: tests.kinds maps {several} to several functions each, but tests.function_by is "
                            "source_kind alone, so no other criterion can leave one of them: every row of those kinds "
                            "would match several functions")


def _check_id_patterns(where: str, tests: Mapping[str, Any], functions: Mapping[str, FunctionSpec]) -> None:
    """tests.id_patterns is given exactly when tests.function_by uses id_pattern, names every function and nothing
    else, and each pattern is a valid regular expression."""
    uses = "id_pattern" in _criteria(tests["function_by"])
    if "id_patterns" not in tests:
        if uses:
            raise ManifestError(f"{where}: tests.function_by uses id_pattern, so tests.id_patterns must give every "
                                f"function's row id pattern ({', '.join(functions)})")
        return
    if not uses:
        raise ManifestError(f"{where}: tests.id_patterns is given, but tests.function_by does not use id_pattern (it "
                            f"is {tests['function_by']!r})")
    if list(tests["id_patterns"]) != list(functions):
        raise ManifestError(f"{where}: tests.id_patterns must name every function, in the functions' order "
                            f"({list(functions)}); it names {list(tests['id_patterns'])}")
    for key, pattern in tests["id_patterns"].items():
        try:
            re.compile(pattern)
        except re.error as error:
            raise ManifestError(f"{where}: tests.id_patterns {key}: {pattern!r} is not a regular expression ({error})"
                                ) from error


def _kind_functions(where: str, kinds: Mapping[str, str | list[str]] | None, row: dict[str, Any]) -> list[str]:
    """The function keys a row's source.kind names: through `kinds` when the manifest gives that map (one key, or a
    list of keys when the kind holds rows of several functions), else the kind itself."""
    source = row.get("source")
    if not isinstance(source, dict) or "kind" not in source:
        raise ManifestError(f"{where}: test row {row['id']} has no source.kind, which tests.function_by "
                            "source_kind needs to name its function")
    kind = source["kind"]
    if kinds is None:
        return [kind]
    if kind not in kinds:
        raise ManifestError(f"{where}: test row {row['id']} has source.kind {kind!r}, which tests.kinds does not "
                            f"map to a function (it maps {', '.join(map(repr, kinds))})")
    named = kinds[kind]
    return [named] if isinstance(named, str) else list(named)


# A function's request shape: the exact state keys a row of it has, in order (its context keys, then its input key),
# or TEXT when its input form is text and the state is one plain string.
TEXT: Literal["text"] = "text"
type StateShape = tuple[str, ...] | Literal["text"]


def state_shape(function: FunctionSpec) -> StateShape:
    """The request shape of `function` (its own context and input, or the adapter's), as the state_shape criterion
    compares it with a test row's state."""
    return request_shape(function.context, function.input)


def request_shape(context: Sequence[ContextField], input: InputField) -> StateShape:
    """The request shape of a context and an input: TEXT for a text input, else the context keys and then the input
    key."""
    if input.form == "text":
        return TEXT
    return (*(field.key for field in context), input.key)


def _shape_of_state(state: Any) -> StateShape | None:
    """A row's state as a request shape: TEXT for a string, the keys in order for an object, None for anything else
    (which no function has)."""
    if isinstance(state, str):
        return TEXT
    if isinstance(state, dict):
        return tuple(state)
    return None


def function_key_of_test_row(where: str, function_by: str | list[str], types: Mapping[str, str],
                             row: dict[str, Any], kinds: Mapping[str, str | list[str]] | None,
                             shapes: Mapping[str, StateShape] | None,
                             id_patterns: Mapping[str, str] | None) -> str:
    """The key of the function a test row belongs to. `types` maps each function key to its question type, in the
    manifest's order; `kinds` is the manifest's tests.kinds (None when it has none); `shapes` maps each function key
    to its request shape (state_shape / request_shape), or is None for a caller that maps rows without shapes (then
    the state_shape criterion is refused); `id_patterns` is the manifest's tests.id_patterns (None when it has none,
    and then the id_pattern criterion is refused). Each criterion of function_by keeps the functions it allows, in
    order, and exactly one must remain. This is the one place that maps test rows to functions (the import scripts,
    the factory and `jeff test` all use it)."""
    if function_by == "only":
        if len(types) != 1:
            raise ManifestError(f"{where}: tests.function_by is only, but it has functions {list(types)}")
        return next(iter(types))
    if isinstance(function_by, str) and function_by not in ("question_type", "id_suffix", "source_kind",
                                                            "state_shape", "id_pattern"):
        raise ManifestError(f"{where}: unknown tests.function_by {function_by!r}")
    candidates = list(types)
    for criterion in _criteria(function_by):
        if criterion == "question_type":
            kind = row["question"]["type"]
            found = [key for key in candidates if types[key] == kind]
            if len(found) != 1 and function_by == "question_type":
                raise ManifestError(f"{where}: test row {row['id']} asks a {kind} question, and there is no single "
                                    f"function of type {kind} (functions: {types})")
        elif criterion == "id_suffix":
            found = [key for key in candidates if row["id"].endswith((f"-{key}", f":{key}"))]
            if len(found) != 1 and function_by == "id_suffix":
                raise ManifestError(f"{where}: test row id {row['id']} does not end with '-' or ':' and a "
                                    f"function key ({', '.join(types)})")
        elif criterion == "source_kind":
            named = _kind_functions(where, kinds, row)
            found = [key for key in candidates if key in named]
            unknown = [key for key in named if key not in types]
            if unknown:
                raise ManifestError(f"{where}: test row {row['id']} has source.kind naming {unknown[0]!r}, which is "
                                    f"not one of its functions ({', '.join(types)})")
        elif criterion == "state_shape":
            if shapes is None:
                raise ManifestError(f"{where}: tests.function_by uses state_shape, but no request shapes of its "
                                    "functions were given to map test rows by")
            shape = _shape_of_state(row["state"])
            found = [key for key in candidates if shapes[key] == shape]
        elif criterion == "id_pattern":
            if id_patterns is None:
                raise ManifestError(f"{where}: tests.function_by uses id_pattern, but no tests.id_patterns were "
                                    "given to map test rows by")
            found = [key for key in candidates if re.fullmatch(id_patterns[key], row["id"])]
        else:
            raise ManifestError(f"{where}: unknown tests.function_by criterion {criterion!r}")
        if not found:
            raise ManifestError(f"{where}: test row {row['id']} matches no function: after {criterion} (of "
                                f"tests.function_by {function_by!r}) none of {candidates} remains")
        candidates = found
    if len(candidates) != 1:
        raise ManifestError(f"{where}: test row {row['id']} matches several functions ({', '.join(candidates)}) "
                            f"by tests.function_by {function_by!r}; exactly one must remain")
    return candidates[0]


def function_of_test_row(manifest: Manifest, row: dict[str, Any]) -> FunctionSpec:
    """The function a published or internal test row belongs to, by the manifest's tests.function_by, tests.kinds and
    tests.id_patterns."""
    types = {key: spec.type for key, spec in manifest.functions.items()}
    shapes = {key: state_shape(spec) for key, spec in manifest.functions.items()}
    key = function_key_of_test_row(manifest.id, manifest.tests["function_by"], types, row,
                                   manifest.tests.get("kinds"), shapes, manifest.tests.get("id_patterns"))
    return manifest.functions[key]
