"""The contract shipped with jeff: JSON Schemas and the interface catalogue (copied from the repository's contract/
folder by scripts/sync_shipped.py)."""

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Literal

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from jeff.errors import ContractError

type SchemaName = Literal["manifest", "decision", "graph", "graph_result", "socket", "decide_request",
                          "decide_response", "functions_response", "graphs_response", "systemone_request",
                          "systemone_response", "models_response", "served_request", "adapters_response",
                          "health_response", "login_request", "login_response", "accounts_keys", "accounts_usage"]
SCHEMAS: tuple[SchemaName, ...] = ("manifest", "decision", "graph", "graph_result", "socket", "decide_request",
                                   "decide_response", "functions_response", "graphs_response", "systemone_request",
                                   "systemone_response", "models_response", "served_request", "adapters_response",
                                   "health_response", "login_request", "login_response", "accounts_keys",
                                   "accounts_usage")
type QuestionType = Literal["choice", "noul", "score"]


def shipped_dir() -> Path:
    return Path(__file__).with_name("_shipped")


@dataclass(frozen=True)
class InterfaceSpec:
    name: str
    question_type: QuestionType
    values: tuple[str, ...] | None
    description: str


@dataclass(frozen=True)
class ErrorSpec:
    name: str
    status: int | None
    meaning: str


@cache
def load_schema(name: SchemaName) -> dict[str, Any]:
    path = shipped_dir() / "contract" / "schemas" / f"{name}.schema.json"
    if not path.exists():
        raise ContractError(f"Schema {path} is missing; run scripts/sync_shipped.py")
    schema: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return schema


@cache
def _registry() -> Registry:
    """Every contract schema by its $id, so one schema can $ref another (graph_result refers to decision)."""
    schemas = [load_schema(name) for name in SCHEMAS]
    return Registry().with_resources((schema["$id"], Resource.from_contents(schema)) for schema in schemas)


@cache
def _validator(name: SchemaName, definition: str | None = None) -> Draft202012Validator:
    """A validator for schema `name`, or for one of its $defs (`definition`), built once and reused."""
    schema = load_schema(name)
    if definition is not None:
        if definition not in schema.get("$defs", {}):
            raise ContractError(f"The {name} schema has no definition {definition!r}")
        schema = {"$ref": f"{schema['$id']}#/$defs/{definition}"}
    return Draft202012Validator(schema, registry=_registry())


def validate(name: SchemaName, document: object, where: str, definition: str | None = None) -> None:
    """ContractError listing every problem when `document` does not match schema `name` (or, with `definition`,
    the one of its $defs the document says it is: checking one shape is much quicker than a oneOf of all)."""
    validator = _validator(name, definition)
    problems = sorted(validator.iter_errors(document), key=lambda error: list(error.absolute_path))
    if problems:
        lines = []
        for problem in problems:
            lines.append(f"  at /{'/'.join(map(str, problem.absolute_path))}: {problem.message}")
            # A oneOf/anyOf failure only says "not valid under any of the given schemas"; its context
            # carries why each alternative failed, which is what actually names the wrong field.
            lines.extend(f"    - {sub.message}" for sub in problem.context or ())
        shape = f"the {name} schema" if definition is None else f"{definition} in the {name} schema"
        raise ContractError(f"{where} does not match {shape}:\n" + "\n".join(lines))


def is_valid(name: SchemaName, document: object) -> bool:
    return bool(_validator(name).is_valid(document))


def first_problem(name: SchemaName, document: object, definition: str | None = None) -> str:
    """One short line naming the first thing wrong with a document that does not match schema `name`, for error
    replies that must stay short. Wherever the first problem is a oneOf (the socket schema's top level, or a nested
    one such as /v1/decide's on_low_confidence), the document is judged against the alternative it comes closest to
    matching, repeatedly, so the line names a field ("'rows' is a required property") rather than "not valid under
    any of the given schemas". The closest alternative is the one whose problems all lie deepest in the document
    (the document matched it furthest: a call list with one bad label is closer to the call shape than to the
    dispatch shape it lacks a key for), then the one with the fewest problems. A value with too many items or
    properties is reported by its count, not quoted. Long values quoted in the line are cut at 200 characters.
    `definition` judges the document against one of the schema's $defs instead of the whole schema: the shape the
    document says it has (a socket message whose op names a channel operation), so the line names what is wrong
    with that shape rather than with whichever other shape the document happens to come closest to."""
    validator = _validator(name, definition)
    problems = list(validator.iter_errors(document))
    if not problems:
        raise ContractError(f"first_problem was asked about a document that matches the {name} schema")
    problem = min(problems, key=lambda error: list(map(str, error.absolute_path)))
    while problem.validator == "oneOf" and problem.context:
        by_alternative: dict[object, list[Any]] = {}
        for sub in problem.context:
            by_alternative.setdefault(sub.relative_schema_path[0], []).append(sub)
        closest = min(by_alternative.values(),
                      key=lambda errors: (-min(len(error.absolute_path) for error in errors), len(errors)))
        problem = min(closest, key=lambda error: list(map(str, error.absolute_path)))
    if problem.validator in ("maxItems", "maxProperties") and isinstance(problem.instance, (list, dict)):
        # jsonschema's own message quotes the whole value before saying what is wrong with it
        kind = "items" if problem.validator == "maxItems" else "properties"
        message = f"{len(problem.instance)} {kind}, at most {problem.validator_value} are allowed"
    else:
        message = problem.message if len(problem.message) <= 200 else problem.message[:200] + "..."
    where = "/".join(map(str, problem.absolute_path))
    return f"{message} (at /{where})" if where else message


@cache
def interfaces() -> dict[str, InterfaceSpec]:
    path = shipped_dir() / "contract" / "interfaces.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    found: dict[str, InterfaceSpec] = {}
    for name, spec in raw.items():
        if spec["question_type"] not in ("choice", "noul", "score"):
            raise ContractError(f"{path}: interface {name} has an unknown question_type {spec['question_type']!r}")
        values = tuple(spec["values"]) if "values" in spec else None
        found[name] = InterfaceSpec(name, spec["question_type"], values, spec["description"])
    return found


@cache
def error_catalogue() -> dict[str, ErrorSpec]:
    path = shipped_dir() / "contract" / "errors.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    found: dict[str, ErrorSpec] = {}
    for name, spec in raw.items():
        status = spec["status"]
        if status is not None and not (isinstance(status, int) and 400 <= status <= 599):
            raise ContractError(f"{path}: {name} has status {status!r}; use an HTTP error status or null")
        found[name] = ErrorSpec(name, status, spec["meaning"])
    return found
