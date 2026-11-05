"""The functions a Jeff can call now, in the shape GET /v1/functions answers: the server's /v1/functions, the MCP tool
jeff_list_functions (jeff.mcp) and anything else that lists functions build it here, so the listings never differ."""

from typing import TYPE_CHECKING, Any

from jeff.manifest import FunctionSpec

if TYPE_CHECKING:
    from jeff.client import Jeff


def function_listing(jeff: "Jeff") -> dict[str, Any]:
    """Every function of every installed adapter that has a manifest in the Jeff's catalogue, read with
    installed(refresh=True) so a change to the served adapters shows at once. An adapter the engine serves but the
    catalogue does not describe is not listed: without its manifest it cannot be called. Each function's calibration
    fields are its manifest's; base_format says which of them applies on this Jeff's engine (calibration on a
    bfloat16 base, the calibration_by_format entry for format_id on an INT4 one). Each function lists its own context
    and input (those its manifest entry declares, else the adapter's), each field with its code_name, the name callers
    use for it, and its aliases: each adapter alias with the function's key, the other references that name it."""
    installed = jeff.engine.installed(refresh=True)
    listed = []
    for manifest in jeff.catalogue.manifests():
        if manifest.id not in installed:
            continue
        for key, function in manifest.functions.items():
            raw = manifest.raw["functions"][key]
            entry = {"id": function.id, "aliases": [f"{alias}.{key}" for alias in manifest.aliases],
                     "interface": function.interface, "type": function.type,
                     "labels": raw["labels"], "context": _context(function), "input": _input(function),
                     "calibration": raw["calibration"],
                     "adapter_version": manifest.version, "licence": manifest.raw["licence"]}
            if "calibration_by_format" in raw:
                entry["calibration_by_format"] = raw["calibration_by_format"]
            listed.append(entry)
    return {"base_format": jeff.base_format, "functions": listed}


def _context(function: FunctionSpec) -> list[dict[str, Any]]:
    """The function's context fields as the manifest describes them, each with its code name (declared or derived)."""
    return [{"key": field.key, "description": field.description, "required": field.required,
             "code_name": field.code_name} for field in function.context]


def _input(function: FunctionSpec) -> dict[str, Any]:
    spec = function.input
    return {"key": spec.key, "description": spec.description, "form": spec.form, "code_name": spec.code_name}
