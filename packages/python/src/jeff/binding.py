"""Binding a function to its context and labels, and building the request row the model reads.

Context and labels come before the input in the prompt, so one binding gives one fixed prompt prefix that the engine
can process once and reuse; each call then only adds the input."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from jeff.errors import InvalidLabels, InvalidRequest, MissingContext, MissingLabels, TooManyLabels, UnknownContext
from jeff.manifest import Calibration, FunctionSpec, InputField, Manifest

type JSON = str | int | float | bool | None | list[JSON] | dict[str, JSON]
# What a caller passes as labels=: key -> description (caller and describe labels), or a list of keys (subset labels).
type Labels = Mapping[str, str | None] | Sequence[str]

NUMBER_LIKE = re.compile(r"^\s*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?\s*$")


@dataclass(frozen=True)
class Binding:
    function: FunctionSpec
    manifest: Manifest
    context: dict[str, JSON]
    criteria: dict[str, str | None] | None
    wire_to_caller: dict[str, str]
    # the threshold block on the engine's base format: function.calibration on bfloat16, its calibration_by_format
    # entry on an INT4 base, None when uncalibrated (make_decision abstains below its threshold)
    calibration: Calibration | None


def _context(function: FunctionSpec, given: Mapping[str, JSON] | None) -> dict[str, JSON]:
    """The caller's context, given by code name, keyed by each field's exact key (as the prompt shows it), in the
    order of the function's context fields (its own, or its adapter's)."""
    given = dict(given or {})
    fields = function.context
    if function.input.form == "text" and given:
        raise UnknownContext(f"{function.id} takes plain text only (its input {function.input.code_name} is the "
                             f"whole state the adapter sees), so it has no context fields; do not pass context "
                             f"(got {', '.join(sorted(given))})")
    known = {field.code_name for field in fields}
    unknown = sorted(set(given) - known)
    if unknown:
        code_of = {field.key: field.code_name for field in fields}
        exact = [f"{key!r} as {code_of[key]}" for key in unknown if key in code_of]
        raise UnknownContext(f"{function.id} has no context field(s) {', '.join(unknown)}; its fields are "
                             f"{', '.join(field.code_name for field in fields) or 'none'}"
                             + (f"; context is given by code name, so pass {', '.join(exact)}" if exact else ""))
    missing = [field.code_name for field in fields if field.required and field.code_name not in given]
    if missing:
        raise MissingContext(function.id, missing)
    return {field.key: given[field.code_name] for field in fields if field.code_name in given}


def _label_keys(labels: Labels, refusal: str) -> list[str]:
    """labels= given as a list (or tuple) of key strings; anything else is InvalidLabels with `refusal`."""
    if not isinstance(labels, (list, tuple)) or not all(isinstance(key, str) for key in labels):
        raise InvalidLabels(refusal)
    return list(labels)


def _subset_criteria(function: FunctionSpec, labels: Labels) -> tuple[dict[str, str | None], dict[str, str]]:
    spec = function.labels
    keys = _label_keys(labels, f"{function.id} offers a choice of its own labels; pass labels= as a list of keys "
                               f"from {', '.join(spec.fixed)}")
    if not keys:
        raise MissingLabels(function.id)
    seen: set[str] = set()
    duplicates: set[str] = set()
    for key in keys:
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    repeated = sorted(duplicates)
    if repeated:
        raise InvalidLabels(f"{function.id}: label(s) {', '.join(repeated)} given more than once")
    unknown = [key for key in keys if key not in spec.fixed]  # spec.fixed is a dict: O(1) per key
    if unknown:
        raise InvalidLabels(f"{function.id}: label(s) {', '.join(unknown)} are not among its labels "
                            f"{', '.join(spec.fixed)}")
    missing = [key for key in spec.required if key not in seen]
    if missing:
        raise InvalidLabels(f"{function.id}: label(s) {', '.join(missing)} must always be offered")
    return {key: spec.fixed[key] for key in keys}, {key: key for key in keys}


def _fixed_subset_criteria(function: FunctionSpec, labels: Labels) -> tuple[dict[str, str | None], dict[str, str]]:
    """Fixed labels with subset true, offered as the caller's subset: the keys in the manifest's order, each with its
    fixed description."""
    spec = function.labels
    keys = _label_keys(labels, f"{function.id} has fixed labels and may offer some of them; pass labels= as a list "
                               f"of keys from {', '.join(spec.fixed)}, or no labels= to offer them all")
    if not keys:
        raise InvalidLabels(f"{function.id}: labels= is an empty list; pass at least one key from "
                            f"{', '.join(spec.fixed)}, or no labels= to offer them all")
    repeated = sorted({key for key in keys if keys.count(key) > 1})
    if repeated:
        raise InvalidLabels(f"{function.id}: label(s) {', '.join(repeated)} given more than once")
    unknown = [key for key in keys if key not in spec.fixed]
    if unknown:
        raise InvalidLabels(f"{function.id}: label(s) {', '.join(unknown)} are not among its labels "
                            f"{', '.join(spec.fixed)}")
    offered = set(keys)
    criteria = {key: description for key, description in spec.fixed.items() if key in offered}
    return criteria, {key: key for key in criteria}


def _describe_criteria(function: FunctionSpec, labels: Labels) -> tuple[dict[str, str | None], dict[str, str]]:
    spec = function.labels
    if not isinstance(labels, Mapping):
        raise InvalidLabels(f"{function.id}: pass labels= as an object with exactly the keys "
                            f"{', '.join(spec.keys)}, each with its description")
    extra = [key for key in labels if key not in spec.keys]
    missing = [key for key in spec.keys if key not in labels]
    if extra or missing:
        raise InvalidLabels(f"{function.id} takes exactly the labels {', '.join(spec.keys)}"
                            f"{'; unknown: ' + ', '.join(extra) if extra else ''}"
                            f"{'; missing: ' + ', '.join(missing) if missing else ''}")
    return {key: labels[key] for key in spec.keys}, {key: key for key in spec.keys}


def _choice_criteria(function: FunctionSpec, labels: Labels | None
                     ) -> tuple[dict[str, str | None], dict[str, str]]:
    spec = function.labels
    if spec.source == "fixed":
        if labels is None:
            return dict(spec.fixed), {key: key for key in spec.fixed}
        if not spec.subset:
            raise InvalidLabels(f"{function.id} has fixed labels ({', '.join(spec.fixed)}); do not pass labels=")
        return _fixed_subset_criteria(function, labels)
    if labels is None:
        raise MissingLabels(function.id)
    if spec.source == "subset":
        return _subset_criteria(function, labels)
    if spec.source == "describe":
        return _describe_criteria(function, labels)
    if not isinstance(labels, Mapping):
        raise InvalidLabels(f"{function.id}: pass labels= as an object of key -> description")
    caller = dict(labels)
    if not caller:
        raise MissingLabels(function.id)
    if spec.max is not None and len(caller) > spec.max:
        raise TooManyLabels(f"{function.id} got {len(caller)} labels; it was trained on at most {spec.max}")
    for key in caller:
        if key in spec.fixed_first:
            raise InvalidLabels(f"{function.id}: label {key!r} is reserved (always listed first: "
                                f"{', '.join(spec.fixed_first)}); rename yours")
        if NUMBER_LIKE.match(key):
            raise InvalidLabels(f"{function.id}: label key {key!r} looks like a number; use a word")
    criteria: dict[str, str | None] = dict(spec.fixed_first)
    wire_to_caller = {key: key for key in spec.fixed_first}
    for index, (key, description) in enumerate(caller.items(), start=1):
        wire = spec.caller_keys.format(n=index) if spec.caller_keys else key
        if wire in criteria:
            raise InvalidLabels(f"{function.id}: label {key!r} collides with option {wire!r}")
        criteria[wire] = description
        wire_to_caller[wire] = key
    return criteria, wire_to_caller


def bind(manifest: Manifest, function: FunctionSpec, context: Mapping[str, JSON] | None,
         labels: Labels | None, *, calibration: Calibration | None) -> Binding:
    """calibration: the block the function decides with on the engine's base format (jeff.manifest.calibration_for,
    which Jeff resolves once per binding), or None when it is uncalibrated there."""
    bound_context = _context(function, context)
    if function.type == "choice":
        criteria, wire_to_caller = _choice_criteria(function, labels)
        return Binding(function, manifest, bound_context, criteria, wire_to_caller, calibration)
    if labels is not None:
        raise InvalidLabels(f"{function.id} is a {function.type} function; it takes no labels=")
    keys = option_keys_for(function)
    return Binding(function, manifest, bound_context, None, {key: key for key in keys}, calibration)


def option_keys_for(function: FunctionSpec) -> list[str]:
    if function.type == "noul":
        return ["false", "true"]
    if function.type == "score":
        return [str(index) for index in range(len(function.labels.levels))]
    raise ValueError("choice functions get their option keys from a binding")


def option_keys(binding: Binding) -> list[str]:
    if binding.criteria is not None:
        return list(binding.criteria)
    return option_keys_for(binding.function)


def request_state(function_id: str, spec: InputField, context: dict[str, JSON],
                  input: JSON) -> dict[str, JSON] | str:
    """The state the model reads, shaped as the adapter was trained: form fields, the bound context and then the input
    under its key (the input last); form text, the input string itself (a text input has no context)."""
    if spec.form == "fields":
        return {**context, spec.key: input}
    if not isinstance(input, str):
        raise InvalidRequest(f"{function_id} takes plain text: its input {spec.code_name} must be a string, got "
                             f"{type(input).__name__}")
    return input


def build_row(binding: Binding, input: JSON) -> dict[str, Any]:
    function = binding.function
    state = request_state(function.id, function.input, binding.context, input)
    question: dict[str, Any] = {"type": function.type, "instructions": function.instructions}
    if function.type == "choice":
        question["criteria"] = dict(binding.criteria or {})
    elif function.type == "score":
        question["criteria"] = list(function.labels.levels)
    elif function.labels.fixed:
        question["criteria"] = dict(function.labels.fixed)
    return {"state": state, "question": question}
