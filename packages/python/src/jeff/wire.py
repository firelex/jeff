"""Errors on the wire (engine socket and HTTP): {"type", "message", "details"}. The receiving side rebuilds the same
JeffError subclass with the same message and attributes, so every SDK raises identical errors."""

from collections.abc import Mapping
from typing import Any

import jeff.errors as errors
from jeff.decision import Decision
from jeff.errors import EngineError, JeffError


def error_to_wire(error: JeffError) -> dict[str, Any]:
    """The error's own attributes are its details; Python's internal ones (such as __notes__ from add_note) are
    not."""
    details: dict[str, Any] = {}
    for key, value in vars(error).items():
        if key.startswith("__"):
            continue
        details[key] = value.to_json() if isinstance(value, Decision) else value
    return {"type": type(error).__name__, "message": str(error), "details": details}


def error_from_wire(body: Mapping[str, Any]) -> JeffError:
    name, message = str(body["type"]), str(body["message"])
    cls = getattr(errors, name, None)
    if not (isinstance(cls, type) and issubclass(cls, JeffError)):
        return EngineError(f"unknown error type {name!r} from the engine: {message}")
    error = cls.__new__(cls)
    Exception.__init__(error, message)
    for key, value in dict(body.get("details") or {}).items():
        setattr(error, key, Decision.from_json(value) if key == "decision" else value)
    return error
