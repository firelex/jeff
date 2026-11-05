"""MCP (Model Context Protocol) for any Jeff: one tool per function the Jeff can call now, plus jeff_dispatch (route
an input through the router functions to the function that should answer it) and jeff_list_functions (the GET
/v1/functions listing, jeff.listing.function_listing).

build_mcp_server takes `jeff_for_request`, called once for each tools/list and tools/call, so a server can hand each
request its own view (the Jeff server's view carries the caller's licences) and a change to the served adapters shows
at the next tools/list. Tools are listed from the functions of the installed adapters that the Jeff's catalogue
describes; a tool name is tool_name(function id). Each tool's description comes from the manifest (the function's
instructions, its interface, its input and context fields and what its labels mean), and its input schema is
{"input", "context" (the function's own context fields, or its adapter's; required when one of them is required),
"labels" (only for a function whose caller supplies or chooses the labels), "on_low_confidence": "return" (the
default, as in POST /v1/decide) or "error"}.
That listed schema describes the arguments for the model choosing them; a call's arguments are checked only for their
top-level shape (_call_validator: input present, no other keys than those four, context and labels of the forms
/v1/decide takes, on_low_confidence one of the two), and binding then checks the context and labels as on /v1/decide,
so a tool call is refused with the same error type as the same call to POST /v1/decide (MissingContext,
UnknownContext, MissingLabels, InvalidLabels, TooManyLabels).

A tool call answers the whole Decision (abstained included) as JSON text and as structured content. A JeffError is a
tool error whose text starts with the error's type ("NotLicensed: ...") and whose structured content is
{"error": <the error as the HTTP API sends it>}; any other exception is not caught. A JeffError while listing tools is
an MCP error with the same text.

The Jeff server serves these tools at /mcp (jeff_server.mcp_mount); `jeff mcp` serves them over stdio (serve_stdio).
This module is the only one in jeff that imports the MCP SDK (the extra jeff[mcp])."""

import json
import threading
import weakref
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from typing import Any

import anyio
import anyio.to_thread
import jsonschema
import jsonschema.exceptions
import mcp_types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import MCPError

from jeff import contract
from jeff.catalogue import Catalogue
from jeff.client import Jeff
from jeff.errors import ContractError, InvalidRequest, JeffError, UnknownFunction
from jeff.listing import function_listing
from jeff.manifest import FunctionSpec, Manifest
from jeff.routing import DEFAULT_ROUTER, check_router_reaches
from jeff.wire import error_to_wire

DISPATCH_TOOL = "jeff_dispatch"
LIST_TOOL = "jeff_list_functions"
POLICIES = ["return", "error"]

ABSTAINED = ("The answer is a decision: its value, its confidence, and abstained, which is true when Jeff is not "
             "confident enough in the answer to act on it (check the input another way, or ask a person).")


def tool_name(function_id: str) -> str:
    """The MCP tool name of a function: its ID without the leading "jeff.", with every "." and "-" replaced by "_"
    (jeff.support.triage.route -> support_triage_route)."""
    if not function_id.startswith("jeff."):
        raise ContractError(f"Function ID {function_id!r} does not start with 'jeff.'; every Jeff function ID does")
    return function_id.removeprefix("jeff.").replace(".", "_").replace("-", "_")


def check_tool_names(catalogue: Catalogue) -> None:
    """Every function in the catalogue has its own tool name; ContractError names the two function IDs that share
    one, or the function whose tool name is jeff_dispatch or jeff_list_functions. Serving checks this at startup
    (jeff_server.mcp_mount, serve_stdio), so a catalogue with clashing names never starts serving."""
    _catalogue_tools(catalogue)


def _tool_functions(catalogue: Catalogue) -> dict[str, FunctionSpec]:
    """Every function in the catalogue by tool name, installed or not, so a call to a tool whose adapter stopped being
    served is NotInstalled rather than unknown."""
    by_name: dict[str, FunctionSpec] = {}
    for manifest in catalogue.manifests():
        for function in manifest.functions.values():
            name = tool_name(function.id)
            if name in (DISPATCH_TOOL, LIST_TOOL):
                raise ContractError(f"Function {function.id} has the MCP tool name {name}, which is the name of "
                                    "one of Jeff's own tools; rename the adapter or the function")
            if name in by_name:
                raise ContractError(f"Functions {by_name[name].id} and {function.id} have the same MCP tool name "
                                    f"{name}; rename one of the adapters or functions")
            by_name[name] = function
    return by_name


# --- tool descriptions and schemas ---------------------------------------------------------------------------------

def _meanings(labels: Mapping[str, str | None]) -> str:
    return "; ".join(f"{key}: {meaning}" if meaning else key for key, meaning in labels.items())


def _answers(function: FunctionSpec) -> str:
    """What the function answers and, for labels the caller supplies, what to pass, in plain words."""
    labels = function.labels
    if function.type == "score":
        levels = "; ".join(f"{index} = {level}" for index, level in enumerate(labels.levels))
        return f"It answers with a level number from 0 to {len(labels.levels) - 1}: {levels}."
    if function.type == "noul":
        meanings = f" ({_meanings(labels.fixed)})" if labels.fixed else ""
        return f"It answers true or false{meanings}."
    if labels.source == "fixed" and labels.subset:
        return (f"It answers with one of these labels: {_meanings(labels.fixed)}. To offer only some of them, pass "
                "'labels' as the list of their keys; omit 'labels' to offer them all.")
    if labels.source == "fixed":
        return f"It answers with one of these labels: {_meanings(labels.fixed)}."
    if labels.source == "subset":
        required = f" The labels {', '.join(labels.required)} must always be offered." if labels.required else ""
        return ("Pass 'labels' as the list of label keys to choose from, taken from these labels: "
                f"{_meanings(labels.fixed)}.{required} It answers with one of the keys you offered.")
    if labels.source == "describe":
        return (f"Pass 'labels' as an object giving a description for each of exactly these keys: "
                f"{', '.join(labels.keys)}. It answers with one of those keys.")
    reserved = f" It may also answer with: {_meanings(labels.fixed_first)}." if labels.fixed_first else ""
    return ("Pass 'labels' as an object of your own labels to choose from, each label key with a description of "
            f"what it means (at most {labels.max} labels). It answers with one of your label keys.{reserved}")


def _description(manifest: Manifest, function: FunctionSpec) -> str:
    interface = contract.interfaces()[function.interface]
    parts = [function.instructions,
             f"Jeff function {function.id} (adapter version {manifest.version}), interface {function.interface}: "
             f"{interface.description}",
             f"Input: {function.input.description}"]
    if function.context:
        fields = "; ".join(f"{field.code_name} ({'required' if field.required else 'optional'}): {field.description}"
                           for field in function.context)
        parts.append(f"Context fields: {fields}")
    parts += [_answers(function), ABSTAINED]
    return "\n".join(parts)


def _labels_schema(function: FunctionSpec) -> dict[str, Any] | None:
    """The labels argument's schema, or None for a function that takes no labels from the caller."""
    labels = function.labels
    description = _answers(function)
    if function.type != "choice" or (labels.source == "fixed" and not labels.subset):
        return None
    if labels.source in ("subset", "fixed"):
        return {"type": "array", "items": {"enum": list(labels.fixed)}, "minItems": 1, "uniqueItems": True,
                "description": description}
    if labels.source == "describe":
        return {"type": "object", "properties": {key: {"type": ["string", "null"]} for key in labels.keys},
                "required": list(labels.keys), "additionalProperties": False, "description": description}
    return {"type": "object", "additionalProperties": {"type": ["string", "null"]}, "minProperties": 1,
            "maxProperties": labels.max, "description": description}


def _context_schema(function: FunctionSpec) -> dict[str, Any]:
    """The function's own context fields (or its adapter's), by code name."""
    return {"type": "object", "additionalProperties": False,
            "properties": {field.code_name: {"description": field.description} for field in function.context},
            "required": [field.code_name for field in function.context if field.required],
            "description": "The context fields, by code name."}


POLICY_SCHEMA = {"enum": POLICIES, "default": "return",
                 "description": "What happens when Jeff abstains: 'return' the decision with abstained true (the "
                                "default), or 'error' to get a LowConfidence error instead."}


def _input_schema(function: FunctionSpec) -> dict[str, Any]:
    if function.input.form == "text":
        input_schema: dict[str, Any] = {"type": "string", "description": f"{function.input.description} Plain text."}
    else:
        input_schema = {"description": f"{function.input.description} Any JSON value; usually text."}
    properties: dict[str, Any] = {"input": input_schema}
    required = ["input"]
    if function.context:
        properties["context"] = _context_schema(function)
        if any(field.required for field in function.context):
            required.append("context")
    labels = _labels_schema(function)
    if labels is not None:
        properties["labels"] = labels
        if function.labels.source != "fixed":  # fixed labels offered as a subset: without labels, all of them
            required.append("labels")
    properties["on_low_confidence"] = POLICY_SCHEMA
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def _dispatch_schema() -> dict[str, Any]:
    interfaces = contract.interfaces()
    return {
        "type": "object", "additionalProperties": False, "required": ["input", "interface"],
        "properties": {
            "input": {"description": "The input to decide on. Any JSON value; usually text."},
            "interface": {"enum": list(interfaces), "description": "The kind of decision wanted: "
                          + "; ".join(f"{name}: {spec.description}" for name, spec in interfaces.items())},
            "context": {"type": "object", "description": "Context fields by code name. Each function the input is "
                        "routed to takes the fields it has (its own, or its adapter's); see each function's own tool "
                        "for its fields."},
            "labels": {"description": "The labels for the function the input reaches, when it takes labels from "
                       "the caller: an object of label key -> description, or a list of label keys.",
                       "oneOf": [{"type": "object", "additionalProperties": {"type": ["string", "null"]}},
                                 {"type": "array", "items": {"type": "string"}}]},
            "router": {"type": "string", "minLength": 1,
                       "description": f"The router function to start from; {DEFAULT_ROUTER} when left out."}}}


DISPATCH_DESCRIPTION = (
    "Let Jeff choose the function: the input goes through Jeff's router functions, each choosing where it goes next, "
    "until it reaches the function that should answer it, which then decides. Give the kind of decision wanted as "
    f"'interface'. {ABSTAINED} The decision's route lists each router's choice.")
LIST_DESCRIPTION = ("List every function Jeff can call now, with its interface, labels, context and input fields, "
                    "calibration, adapter version and licence.")


@dataclass(frozen=True)
class _CatalogueTools:
    """What a catalogue's tools are, worked out once per catalogue: each function by tool name and its Tool (name,
    description, input schema)."""
    functions: dict[str, FunctionSpec]
    tools: dict[str, types.Tool]


# Catalogues are never changed once built, so their tools are kept for as long as the catalogue lives.
_BY_CATALOGUE: "weakref.WeakKeyDictionary[Catalogue, _CatalogueTools]" = weakref.WeakKeyDictionary()
_BY_CATALOGUE_LOCK = threading.Lock()


def _catalogue_tools(catalogue: Catalogue) -> _CatalogueTools:
    with _BY_CATALOGUE_LOCK:
        known = _BY_CATALOGUE.get(catalogue)
    if known is not None:
        return known
    functions = _tool_functions(catalogue)
    tools: dict[str, types.Tool] = {}
    for name, function in functions.items():
        manifest = catalogue.manifest_of(function)
        tools[name] = types.Tool(name=name, title=function.id, description=_description(manifest, function),
                                 input_schema=_input_schema(function))
    built = _CatalogueTools(functions, tools)
    with _BY_CATALOGUE_LOCK:
        _BY_CATALOGUE[catalogue] = built
    return built


LIST_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}


@cache
def _call_validator() -> jsonschema.Draft202012Validator:
    """The check of a function tool's arguments, the same for every function: context and labels take
    the forms of a /v1/decide call (contract/schemas/decide_request, $defs context and labels)."""
    definitions = contract.load_schema("decide_request")["$defs"]
    schema = {"type": "object", "required": ["input"], "additionalProperties": False,
              "properties": {"input": True, "context": definitions["context"], "labels": definitions["labels"],
                             "on_low_confidence": {"enum": POLICIES}}}
    return jsonschema.Draft202012Validator(schema)


@cache
def _own_tools() -> tuple[types.Tool, types.Tool]:
    return (types.Tool(name=DISPATCH_TOOL, description=DISPATCH_DESCRIPTION, input_schema=_dispatch_schema()),
            types.Tool(name=LIST_TOOL, description=LIST_DESCRIPTION, input_schema=LIST_SCHEMA))


@cache
def _own_validators() -> dict[str, jsonschema.Draft202012Validator]:
    return {DISPATCH_TOOL: jsonschema.Draft202012Validator(_dispatch_schema()),
            LIST_TOOL: jsonschema.Draft202012Validator(LIST_SCHEMA)}


def _tools(jeff: Jeff) -> list[types.Tool]:
    installed = jeff.engine.installed(refresh=True)
    known = _catalogue_tools(jeff.catalogue)
    tools = [tool for name, tool in known.tools.items() if known.functions[name].adapter_id in installed]
    return [*tools, *_own_tools()]


# --- calls ---------------------------------------------------------------------------------------------------------

def _check_arguments(tool: str, validator: jsonschema.Draft202012Validator, arguments: dict[str, Any]) -> None:
    error = jsonschema.exceptions.best_match(validator.iter_errors(arguments))
    if error is not None:
        where = "/".join(str(part) for part in error.absolute_path)
        raise InvalidRequest(f"The arguments of tool {tool} do not have the shape of a tool call: {error.message} "
                             f"(at /{where})")


def _answer(jeff: Jeff, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """The structured result of one tool call; raises the JeffError that refuses it."""
    if name in (LIST_TOOL, DISPATCH_TOOL):
        _check_arguments(name, _own_validators()[name], arguments)
    if name == LIST_TOOL:
        return function_listing(jeff)
    if name == DISPATCH_TOOL:
        router = arguments.get("router", DEFAULT_ROUTER)
        check_router_reaches(jeff.catalogue, router, arguments["interface"],
                             direct="call the function's own tool")
        return jeff.dispatch(arguments["input"], interface=arguments["interface"],
                             context=arguments.get("context"), labels=arguments.get("labels"),
                             router=router).to_json()
    known = _catalogue_tools(jeff.catalogue)
    if name not in known.functions:
        raise UnknownFunction(f"There is no tool {name!r}: it is not jeff_dispatch, jeff_list_functions or the tool "
                              "of any function in the catalogue; list the tools again")
    function = known.functions[name]
    _check_arguments(name, _call_validator(), arguments)
    decision = jeff._interface_call(function.interface, arguments["input"], function.id, arguments.get("context"),
                                    arguments.get("labels"), arguments.get("on_low_confidence", "return"))
    return decision.to_json()


def _text(value: Any) -> list[types.ContentBlock]:
    return [types.TextContent(type="text", text=json.dumps(value))]


def tool_error(error: JeffError) -> types.CallToolResult:
    """A JeffError as a tool error: the text starts with the error's type, the structured content is the error as
    the HTTP API sends it."""
    return types.CallToolResult(content=[types.TextContent(type="text", text=f"{type(error).__name__}: {error}")],
                                structured_content={"error": error_to_wire(error)}, is_error=True)


def build_mcp_server(jeff_for_request: Callable[[], Jeff],
                     answer_with: Callable[[Callable[[Jeff], dict[str, Any]]], dict[str, Any]] | None = None,
                     reword: Callable[[JeffError], JeffError] | None = None) -> Server[Any]:
    """The low-level MCP server answering tools/list and tools/call with `jeff_for_request()`, called once for each
    of those requests, on the worker thread that then does that request's work on the Jeff (which may wait on an
    engine or a server), never on the event loop. answer_with(work), when given, runs a tool call's work on a Jeff
    of its own choosing in place of work(jeff_for_request()) (jeff-server's: the caller's view of the planner, and
    once more on a newer catalogue after an unknown tool). reword(error), when given, is the error a tools/list or
    tools/call failure is answered with (jeff-server's: one that names no private adapter to a caller who is not the
    owner); without it, the error as raised."""

    def answer(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if answer_with is None:
            return _answer(jeff_for_request(), name, arguments)
        return answer_with(lambda planned: _answer(planned, name, arguments))

    def sent(error: JeffError) -> JeffError:
        return error if reword is None else reword(error)

    async def list_tools(ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
                         ) -> types.ListToolsResult:
        try:
            tools = await anyio.to_thread.run_sync(lambda: _tools(jeff_for_request()))
        except JeffError as raised:
            error = sent(raised)
            raise MCPError(types.INTERNAL_ERROR, f"{type(error).__name__}: {error}", {"error": error_to_wire(error)})
        # The listing depends on the served adapters, which change: never cached, never shared between callers.
        return types.ListToolsResult(tools=tools, ttl_ms=0, cache_scope="private")

    async def call_tool(ctx: ServerRequestContext[Any], params: types.CallToolRequestParams) -> types.CallToolResult:
        arguments = dict(params.arguments) if params.arguments is not None else {}
        try:
            answered = await anyio.to_thread.run_sync(answer, params.name, arguments)
        except JeffError as error:
            return tool_error(sent(error))
        return types.CallToolResult(content=_text(answered), structured_content=answered)

    # get_tool_input_schema: the tools declare no Mcp-Param headers, so a 2026-07-28 tools/call over HTTP has none
    # to check; without it the SDK would run a whole tools/list (reading the served adapters) before every call.
    return Server("jeff", version=_version(), on_list_tools=list_tools, on_call_tool=call_tool,
                  get_tool_input_schema=lambda name: None)


def _version() -> str:
    from importlib.metadata import version
    return version("jeff-sdk")


def serve_stdio(jeff: Jeff) -> None:
    """Serve the tools of `jeff` over this process's stdin and stdout until stdin closes. Clashing tool names
    (check_tool_names) raise ContractError before anything is served."""
    from mcp.server.stdio import stdio_server

    check_tool_names(jeff.catalogue)
    server = build_mcp_server(lambda: jeff)

    async def main() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(main)
