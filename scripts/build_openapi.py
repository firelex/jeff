"""Writes contract/openapi.yaml (OpenAPI 3.1): one path for every route the server actually has, request and
response bodies `$ref`-ing the contract's JSON Schemas (inlined into components.schemas), and error responses
derived from contract/errors.yaml -- not a blanket list, but each route's own reachable statuses (see "Adding a
route" below). Run after changing contract/ (a schema, the routes below, or errors.yaml);
packages/python/tests/test_openapi.py fails and names this script when the committed file is stale. Reads
contract/ directly (not through jeff.contract, which reads the shipped copy sync_shipped.py makes from this
script's own output): the generator must not depend on the file it writes.

/openapi.json itself is left out of the document and out of the route-parity check in test_openapi.py: it serves
this file, so documenting it inside itself is circular (the same choice FastAPI's own generator makes for its
built-in /openapi.json, /docs and /redoc).

Every response (success or error) carries an x-request-id header (components.headers.XRequestId):
jeff_server.app._Application stamps one on literally every response it sends, including the ones Starlette's own
ServerErrorMiddleware sends for an exception no handler caught.

Two request/response shapes are documented as several named component schemas instead of one with a bare `oneOf`
(decide_request's calls/dispatch/graph, health_response's minimal/full): see DECIDE_CALLS_REQUEST and
HEALTH_STATUS_ONLY below. The wire format itself is unchanged -- no discriminator property exists on either, and
none is added here; each operation's oneOf says so in its own description."""

import copy
import json
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS_DIR = ROOT / "contract" / "schemas"
ERRORS_PATH = ROOT / "contract" / "errors.yaml"
OUTPUT_PATH = ROOT / "contract" / "openapi.yaml"

# Every contract schema served over HTTP, directly or because another one of these $refs a piece of it (decision
# and graph_result, for decide_response and graph_result itself; graph, for decide_request and graphs_response)
# -- plus this script's own small schemas for the endpoints that had none. Two are deliberately absent:
#
# - "socket": the engine socket protocol's own schema, never sent over HTTP.
# - "manifest": functions_response $refs exactly five small, self-contained fragments of it (labels, calibration,
#   context, input, licence) out of a schema that is mostly about things no HTTP response exposes (weights,
#   tests, routes_to, aliases, base, licence.adapter...). Embedding the whole schema as a component just to serve
#   those fragments would make most of it misleading dead weight in a document meant to describe the API, so
#   _manifest_fragment() below inlines only the five pieces functions_response actually uses, at the place that
#   uses them (see _localize_refs). If a second endpoint ever needs a sixth fragment, or most of the schema, it
#   may be worth promoting "manifest" back to a real component instead of inlining a sixth fragment too.
HTTP_SCHEMAS = [
    "decision", "graph", "graph_result", "decide_request", "decide_response", "functions_response",
    "graphs_response", "systemone_request", "systemone_response", "models_response", "served_request",
    "adapters_response", "health_response", "login_request", "login_response",
]

SCHEMA_URI_PREFIX = "https://jeff.dev/schemas/"
MANIFEST_SCHEMA_URI = SCHEMA_URI_PREFIX + "manifest.schema.json"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _schema_ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


def _manifest_fragment(pointer: str) -> Any:
    """The piece of contract/schemas/manifest.schema.json at JSON Pointer `pointer` (e.g. "/$defs/labels"),
    deep-copied, with each of its own $refs into the manifest's $defs (context and input $ref $defs/field_key and
    $defs/code_name) inlined the same way: the manifest is not an embedded component, so a bare `#/...` in it would
    point nowhere in this document."""
    manifest: Any = _load_json(SCHEMAS_DIR / "manifest.schema.json")
    node = manifest
    for part in pointer.lstrip("/").split("/"):
        node = node[part]
    return _inline_manifest_refs(copy.deepcopy(node))


def _inline_manifest_refs(node: Any) -> Any:
    if isinstance(node, dict):
        if set(node) == {"$ref"} and isinstance(node["$ref"], str) and node["$ref"].startswith("#/"):
            return _manifest_fragment(node["$ref"][1:])
        return {key: _inline_manifest_refs(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_inline_manifest_refs(item) for item in node]
    return node


def _rewrite_ref(ref: str, own_name: str) -> str:
    """A contract schema's own $id-based $ref, rewritten to a plain JSON Pointer into this document's
    components.schemas -- the form OpenAPI 3.1 tooling (and openapi-spec-validator) resolves without fetching
    https://jeff.dev/schemas/... over the network. `https://jeff.dev/schemas/X.schema.json[#/frag]` (another
    schema, by contract.py's own cross-schema $refs) becomes `#/components/schemas/X[/frag]`; a bare `#/frag`
    (a schema's $ref into its own $defs) becomes `#/components/schemas/<own_name>/frag`, since embedding strips
    each schema's $id (see _localize_refs) and a bare `#/...` would otherwise resolve against the whole OpenAPI
    document instead of the schema it came from."""
    if ref.startswith(MANIFEST_SCHEMA_URI):
        raise ValueError(f"A $ref into manifest.schema.json must be a standalone {{'$ref': ...}} object so "
                         f"_localize_refs can inline it instead (manifest is not embedded as its own component "
                         f"-- see HTTP_SCHEMAS's comment): {ref!r}")
    if ref.startswith(SCHEMA_URI_PREFIX):
        rest = ref[len(SCHEMA_URI_PREFIX):]
        file_name, _, fragment = rest.partition("#")
        target = file_name.removesuffix(".schema.json")
        return f"#/components/schemas/{target}" + (f"/{fragment.lstrip('/')}" if fragment else "")
    if ref.startswith("#/"):
        return f"#/components/schemas/{own_name}/{ref[2:]}"
    raise ValueError(f"Unexpected $ref form in a contract schema: {ref!r}")


def _localize_refs(node: Any, own_name: str) -> Any:
    """`node`, with every $ref rewritten by _rewrite_ref and every $id/$schema dropped (both are meaningless, and
    the $id actively wrong, once the schema is embedded under components.schemas rather than standing alone: a
    nested $id would become the base URI JSON Schema resolves this schema's own bare `#/...` refs against,
    which breaks the rewrite above) -- except a $ref into manifest.schema.json, which is inlined in place
    (_manifest_fragment) rather than rewritten, since "manifest" is not one of the embedded components."""
    if isinstance(node, dict):
        if set(node) == {"$ref"} and isinstance(node["$ref"], str) and node["$ref"].startswith(MANIFEST_SCHEMA_URI):
            fragment = node["$ref"][len(MANIFEST_SCHEMA_URI):].removeprefix("#")
            return _localize_refs(_manifest_fragment(fragment), own_name)
        return {key: _rewrite_ref(value, own_name) if key == "$ref" else _localize_refs(value, own_name)
                for key, value in node.items() if key not in ("$id", "$schema")}
    if isinstance(node, list):
        return [_localize_refs(item, own_name) for item in node]
    return node


def _component_schemas() -> dict[str, Any]:
    return {name: _localize_refs(_load_json(SCHEMAS_DIR / f"{name}.schema.json"), name) for name in HTTP_SCHEMAS}


ERROR_BODY: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["error"],
    "properties": {
        "error": {
            "type": "object", "additionalProperties": False, "required": ["type", "message", "details"],
            "properties": {
                "type": {"type": "string", "description": "The JeffError subclass name, e.g. NotLicensed."},
                "message": {"type": "string"},
                "details": {"type": "object", "description": "The error's own extra attributes, if it has any "
                            "(for example NotInstalled's function_id); empty for most errors."},
            },
        }
    },
}

# Item 3's same treatment for the legacy detail oneOf: the array-of-objects alternative is cheap to name (the
# plain string alternative is a primitive -- nothing to name there).
LEGACY_MODEL_ERROR_DETAIL: dict[str, Any] = {
    "title": "LegacyModelErrorDetail",
    "description": "jeff-server's Pydantic-style problem location for the request body's model field.",
    "type": "array", "minItems": 1,
    "items": {
        "type": "object", "additionalProperties": False, "required": ["loc", "msg", "type"],
        "properties": {"loc": {"type": "array", "items": {"type": "string"}}, "msg": {"type": "string"},
                       "type": {"const": "value_error"}},
    },
}

# The legacy error body, added on POST /v1/systemone and GET /v1/models only, for jeff-server's existing
# client: a problem with the request body's model as a Pydantic-style list located at /model, anything else as the
# message string (jeff_server.systemone.legacy_detail).
LEGACY_ERROR_BODY: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["error", "detail"],
    "properties": {
        "error": copy.deepcopy(ERROR_BODY["properties"]["error"]),
        "detail": {
            "description": "A problem with the model as LegacyModelErrorDetail, anything else as a plain string "
                           "(jeff_server.systemone.legacy_detail). No discriminator property is added -- the "
                           "wire has none -- so tell them apart by JSON type (array vs string).",
            "oneOf": [{"type": "string"}, _schema_ref("LegacyModelErrorDetail")],
        },
    },
}

# --- item 3: decide_request's calls/dispatch/graph oneOf, as three named, referenceable component schemas -------
#
# decide_request itself (embedded unchanged, below, in HTTP_SCHEMAS) is unaffected -- it is still what
# jeff.contract validates POST /v1/decide bodies against, and these three borrow its own $defs (call, dispatch,
# graph, policy) by reference rather than copying them, so a change to those $defs cannot drift the two apart.
# No discriminator property is added to the wire (there has never been one): the operation that uses this oneOf
# says so in its own description.

def _decide_defs_ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/decide_request/$defs/{name}"}


DECIDE_CALLS_REQUEST: dict[str, Any] = {
    "title": "DecideCallsRequest",
    "description": "Up to 256 function calls, answered in order.",
    "type": "object", "additionalProperties": False, "required": ["calls"],
    "properties": {
        "calls": {"type": "array", "minItems": 1, "maxItems": 256, "items": _decide_defs_ref("call")},
        "on_low_confidence": _decide_defs_ref("policy"),
    },
}

DECIDE_DISPATCH_REQUEST: dict[str, Any] = {
    "title": "DecideDispatchRequest",
    "description": "One input routed through router functions to the function that should answer it.",
    "type": "object", "additionalProperties": False, "required": ["dispatch"],
    "properties": {"dispatch": _decide_defs_ref("dispatch"), "on_low_confidence": _decide_defs_ref("policy")},
}

DECIDE_GRAPH_REQUEST: dict[str, Any] = {
    "title": "DecideGraphRequest",
    "description": "A registered graph (by id) or an inline graph without code nodes and with at most 256 nodes.",
    "type": "object", "additionalProperties": False, "required": ["graph", "input", "params"],
    "properties": {
        "graph": _decide_defs_ref("graph"), "input": True, "params": {"type": "object"},
        "on_low_confidence_by_node": {"type": "object", "maxProperties": 256,
                                      "additionalProperties": _decide_defs_ref("policy")},
    },
}

# --- item 3: health_response's minimal/full oneOf, as two named, referenceable component schemas -----------------
#
# Same treatment, same reason: health_response itself stays embedded (below) as the thing jeff_server.openapi's
# document actually carries internally-consistent $defs for; these two borrow its $defs/status and $defs/engine.

def _health_defs_ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/health_response/$defs/{name}"}


HEALTH_STATUS_ONLY: dict[str, Any] = {
    "title": "HealthStatusOnly",
    "description": "What a public server (JEFF_PUBLIC=1) answers a caller sending no credentials at all.",
    "type": "object", "additionalProperties": False, "required": ["status"],
    "properties": {"status": _health_defs_ref("status")},
}

HEALTH_REPORT: dict[str, Any] = {
    "title": "HealthReport",
    "description": "What a signed-in caller gets, and what any caller gets on a private server.",
    "type": "object", "additionalProperties": False,
    "required": ["status", "engines", "model", "checkpoint", "max_options", "authentication", "modalities"],
    "properties": {
        "status": _health_defs_ref("status"),
        "engines": {"type": "array", "items": _health_defs_ref("engine")},
        "model": {"type": "string", "description": "The server's JEFF_MODEL_NAME."},
        "checkpoint": {"type": ["string", "null"]},
        "max_options": {"type": ["integer", "null"]},
        "authentication": {"type": "boolean"},
        "modalities": {"type": ["array", "null"], "items": {"enum": ["text", "image"]}},
        "cause": {"type": "string", "description": "Present when status is 'broken': why no engine can answer."},
    },
}

# --- the console's own JSON endpoints (apps/server/src/jeff_server/console.py) -----------------------------------
#
# These four shapes are this server's own wire format, never validated with jeff.contract.is_valid (unlike
# decide_request, served_request, systemone_request and login_request, which the SDK also builds and checks
# against a contract/schemas/*.json file): there is no second implementation of any of them to keep in sync with a
# shared schema file, so they are named component schemas here, inline, exactly like HEALTH_STATUS_ONLY above.

CONSOLE_ME_RESPONSE: dict[str, Any] = {
    "title": "ConsoleMeResponse",
    "description": "The signed-in caller's identity (jeff_server.console.me).",
    "type": "object", "additionalProperties": False, "required": ["user", "kind", "admin"],
    "properties": {
        "user": {"type": "string", "description": "The caller's subject: a login (kind \"console\"), "
                 "\"api-key\" (kind \"api-key\"), a hub token's sub (kind \"hub\"), or \"anonymous\" when this "
                 "server has no credentials configured at all (kind \"open\")."},
        "kind": {"type": "string", "enum": ["api-key", "console", "hub", "open"]},
        "admin": {"type": "boolean", "description": "Whether this is a console session signed in under a login "
                 "JEFF_ADMINS lists; always false for \"api-key\", \"hub\" and \"open\" -- admin status is a "
                 "console-session question only."},
    },
}

CONSOLE_EVENT_REQUEST: dict[str, Any] = {
    "title": "ConsoleEventRequest",
    "type": "object", "additionalProperties": False, "required": ["view"],
    "properties": {"view": {"type": "string", "minLength": 1, "maxLength": 100, "pattern": "^[a-z0-9-]+$"}},
}

CONSOLE_EVENT_RESPONSE: dict[str, Any] = {
    "title": "ConsoleEventResponse",
    "type": "object", "additionalProperties": False, "required": ["logged"],
    "properties": {"logged": {"type": "boolean",
                              "description": "false when JEFF_ACCESS_LOG is not set: nothing was written."}},
}

CONSOLE_EXAMPLE: dict[str, Any] = {
    "title": "ConsoleExample",
    "type": "object", "additionalProperties": False, "required": ["name", "call"],
    "properties": {"name": {"type": "string"}, "call": {"type": "object", "description": "A decide_request call."},
                   "expected": {"type": ["string", "boolean", "number"],
                                "description": "The test set's answer, for an example taken from an adapter's test set "
                                               "(its folder's playground-examples.json); absent for a golden case."}},
}

CONSOLE_EXAMPLES_RESPONSE: dict[str, Any] = {
    "title": "ConsoleExamplesResponse",
    "description": "contract/golden/step1.json's cases, then the examples in each served adapter folder's "
                   "playground-examples.json, for functions this server serves now (jeff_server.console).",
    "type": "object", "additionalProperties": False, "required": ["examples"],
    "properties": {"examples": {"type": "array", "items": _schema_ref("ConsoleExample")}},
}

CONSOLE_ACCESS_LOG_RESPONSE: dict[str, Any] = {
    "title": "ConsoleAccessLogResponse",
    "description": "jeff_server.access_log.AccessLog.read()'s report.",
    "type": "object", "additionalProperties": False,
    "required": ["entries", "total", "people", "anonymous_visits", "anonymous_ips", "locations_on"],
    "properties": {
        "entries": {"type": "array", "items": {"type": "object"}, "description": "Newest first, at most `limit`."},
        "total": {"type": "integer", "description": "Every entry ever recorded, not just the ones returned."},
        "people": {"type": "array", "items": {"type": "object"}, "description": "One summary per signed-in login."},
        "anonymous_visits": {"type": "integer"},
        "anonymous_ips": {"type": "array", "items": {"type": "string"}},
        "locations_on": {"type": "boolean", "description": "Whether JEFF_GEOIP_DB is configured."},
    },
}

NAMED_VARIANT_SCHEMAS = {
    "DecideCallsRequest": DECIDE_CALLS_REQUEST, "DecideDispatchRequest": DECIDE_DISPATCH_REQUEST,
    "DecideGraphRequest": DECIDE_GRAPH_REQUEST, "HealthStatusOnly": HEALTH_STATUS_ONLY,
    "HealthReport": HEALTH_REPORT, "LegacyModelErrorDetail": LEGACY_MODEL_ERROR_DETAIL,
    "ConsoleMeResponse": CONSOLE_ME_RESPONSE, "ConsoleEventRequest": CONSOLE_EVENT_REQUEST,
    "ConsoleEventResponse": CONSOLE_EVENT_RESPONSE, "ConsoleExample": CONSOLE_EXAMPLE,
    "ConsoleExamplesResponse": CONSOLE_EXAMPLES_RESPONSE, "ConsoleAccessLogResponse": CONSOLE_ACCESS_LOG_RESPONSE,
}

BEARER_DESCRIPTION = (
    "Authorization: Bearer <token>. Three kinds of token are accepted, each only when the server has it "
    "configured: the server's own API key (JEFF_API_KEY); a console session token from POST /ui/login "
    "(JEFF_UI_USERS); or a hub-signed JSON Web Token (JEFF_HUB_PUBLIC_KEY), algorithm EdDSA (Ed25519), carrying "
    "sub, licences (the adapter IDs it may call), iat and exp, lasting at most 3600 seconds -- the server never "
    "calls the hub to check one. When the server has none of these configured, the API is open: every caller is "
    "treated as the owner without sending a token at all. A public server (JEFF_PUBLIC=1) always has at least one "
    "of these configured, so a token is then always required."
)

OWNER_ONLY = (
    "Owner-only: needs the server's own credentials (the API key or a console session token from POST "
    "/ui/login), never a hub-signed token, which belongs to a customer and is refused with 403 NotLicensed. With "
    "no credentials configured on the server, every caller is the owner."
)

# --- item 2: x-request-id on every response --------------------------------------------------------------------

X_REQUEST_ID_HEADER_COMPONENT: dict[str, Any] = {
    "description": "A 32-character lowercase-hex request id, set on every response this server sends (even one "
                   "Starlette's own ServerErrorMiddleware sends for an exception no handler caught -- see "
                   "jeff_server.app._Application).",
    "schema": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
}


def _request_id_header() -> dict[str, str]:
    """A fresh dict each call (see _optional_bearer's docstring for why): a $ref to the shared header component
    above, not a copy of it."""
    return {"$ref": "#/components/headers/XRequestId"}


def _error_statuses() -> list[int]:
    """Every distinct HTTP status in contract/errors.yaml, sorted; used only as a sanity check in build() that no
    per-route list below (item 1) has drifted onto a status contract/errors.yaml does not actually define. The
    catalogue's null statuses never reach HTTP (jeff_server.errors answers a JeffError whose catalogued status is
    null as 500 EngineError, logging it as a server bug) and are correctly absent from this list."""
    raw = yaml.safe_load(ERRORS_PATH.read_text(encoding="utf-8"))
    return sorted({spec["status"] for spec in raw.values() if spec["status"] is not None})


def _error_response(status: int, body_schema: str) -> dict[str, Any]:
    headers: dict[str, Any] = {"x-request-id": _request_id_header()}
    if status in (429, 529):
        headers["Retry-After"] = {
            "description": "Whole seconds to wait before retrying.",
            "schema": {"type": "integer", "minimum": 0},
        }
    if status == 401:
        headers["WWW-Authenticate"] = {
            "description": "Bearer, unless the 401 is for console sign-in itself, which no bearer token would fix.",
            "schema": {"type": "string"},
        }
    return {
        "description": f"HTTP {status}; see contract/errors.yaml for which error type(s) answer with this status.",
        "headers": headers,
        "content": {"application/json": {"schema": _schema_ref(body_schema)}},
    }


def _error_responses(statuses: list[int], body_schema: str) -> dict[str, Any]:
    return {str(status): _error_response(status, body_schema) for status in statuses}


def _json_body(schema_name: str, *, required: bool = True) -> dict[str, Any]:
    return {"required": required, "content": {"application/json": {"schema": _schema_ref(schema_name)}}}


def _ok(description: str, schema_name: str | None = None, *, media_type: str = "application/json",
        schema: dict[str, Any] | None = None) -> dict[str, Any]:
    body_schema = schema if schema is not None else (_schema_ref(schema_name) if schema_name else None)
    response: dict[str, Any] = {"description": description, "headers": {"x-request-id": _request_id_header()}}
    if body_schema is not None:
        response["content"] = {media_type: {"schema": body_schema}}
    return response


def _operation(summary: str, description: str, *, request_body: dict[str, Any] | None = None,
               parameters: list[dict[str, Any]] | None = None, ok: dict[str, Any], error_statuses: list[int],
               legacy_errors: bool = False, security: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    operation: dict[str, Any] = {"summary": summary, "description": description}
    if parameters is not None:
        operation["parameters"] = parameters
    if request_body is not None:
        operation["requestBody"] = request_body
    error_schema = "LegacyErrorBody" if legacy_errors else "ErrorBody"
    operation["responses"] = {"200": ok, **_error_responses(error_statuses, error_schema)}
    if security is not None:
        operation["security"] = security
    return operation


def _optional_bearer() -> list[dict[str, Any]]:
    """Either a token, or none at all (the open-server case BEARER_DESCRIPTION covers). A fresh list each call: a
    list reused by reference across operations would come back from yaml.safe_dump with YAML anchors/aliases,
    which is correct YAML but needless noise in a committed file meant to be read as plain OpenAPI."""
    return [{"bearerAuth": []}, {}]


# --- item 1: per-route reachable HTTP statuses, not a blanket list -----------------------------------------------
#
# Adding a route:
#   1. Add the route itself in apps/server/src/jeff_server/<module>.py (plus a contract schema for any new request
#      or response body under contract/schemas/, registered in jeff.contract.SCHEMAS and re-shipped with
#      `uv run python scripts/sync_shipped.py`).
#   2. Add a `paths["/..."] = {"<method>": _operation(...)}` entry in _build() below.
#   3. Work out error_statuses by reading the route's handler and everything it calls, not by guessing:
#        - every route built with jeff_server.auth.V1_DEPENDENCIES (the overwhelming majority) carries
#          `limited` -> `authenticated`, so starts from V1_STATUSES (401 Unauthorised, 429 RateLimited);
#        - a route with jeff_server.operations.owner as an extra dependency (PUT /v1/adapters/served, POST
#          /v1/adapters/reload, GET /metrics) adds OWNER_ONLY_STATUS (403 NotLicensed);
#        - anything else the handler (or a function it calls, transitively -- the engine, the catalogue, the
#          binder, the router, an escalation) can raise that is NOT already covered by the two groups above:
#          look up each JeffError subclass's status in contract/errors.yaml and add it, deduplicated.
#        - a route with NO V1_DEPENDENCIES (currently only GET /health and POST /ui/login) needs the same
#          by-hand reading with no mechanical starting point at all -- both of them still reach some of 401/422/
#          429 directly from their own handler code; see HEALTH_STATUSES and LOGIN_STATUSES below for why.
#   4. Run `uv run python scripts/build_openapi.py` then `uv run python scripts/sync_shipped.py`.
#   5. Run packages/python/tests/test_openapi.py. It mechanically checks only what is mechanically checkable (that
#      every route with the rate limiter lists 401 and 429, that every owner-only route lists 403, and that
#      /health -- which has no rate limiter at all -- lists neither 429 nor 529); it cannot check that your
#      hand-built list for a brand new error source is complete, because that genuinely requires reading the
#      code, which is what step 3 above is for.

AUTH_STATUS = 401        # jeff_server.auth.authenticated: no credentials sent, or none valid, when some are
                         # configured on this server.
RATE_LIMIT_STATUS = 429  # jeff_server.auth.limited's token bucket / in-flight limiter (jeff_server.auth.
                         # RateLimiter), when JEFF_RATE_LIMIT is configured.
OWNER_ONLY_STATUS = 403  # jeff_server.operations.owner: a hub token is a customer's, never the owner's.
V1_STATUSES = (AUTH_STATUS, RATE_LIMIT_STATUS)  # every route carrying jeff_server.auth.V1_DEPENDENCIES.


def _statuses(*groups: tuple[int, ...]) -> list[int]:
    merged: set[int] = set()
    for group in groups:
        merged.update(group)
    return sorted(merged)


DECIDE_STATUSES = _statuses(V1_STATUSES, (
    403,  # NotLicensed: a call's function, a dispatch's leaf, an escalation step or a graph node's function
          # belongs to a proprietary adapter the caller's licences do not list (jeff_server.auth.licence_check,
          # run as jeff.Jeff's authorise hook just before each one is decided).
    402,  # UsageLimitReached: an account key's account has used its input-token allowance for the billing period
          # (jeff_server.accounts.Meter.check, run by jeff_server.decide.caller_view before anything is decided).
    404,  # UnknownFunction: a function, adapter or router reference names something the catalogue does not have
          # (jeff.catalogue, jeff.routing).
    409,  # LowConfidence: on_low_confidence="error" and the decision abstained (jeff.escalation).
    422,  # InvalidRequest/InvalidReference/MissingContext/UnknownContext/MissingLabels/InvalidLabels/
          # TooManyLabels/InterfaceMismatch/InvalidPolicy/GraphError: the request body, a malformed reference,
          # labels, context, or a graph (jeff_server.decide, jeff.binding, jeff.escalation, jeff.graph).
    424,  # NotInstalled: the adapter is in the catalogue but not loaded by any engine right now -- including a
          # race with another API worker changing what is served between this request's check and its engine
          # call (global-constraints.md's review focus 1).
    500,  # EngineError: the engine broke its contract (e.g. answered the wrong number of rows, or a router
          # returned a decision without a confidence).
    502,  # EscalationFailed: an on_low_confidence=escalate(...) step failed.
    504,  # EscalationBudgetExceeded: an escalation ran past its budget_ms.
    529,  # Busy: the engine's queue was full; Retry-After says how long to wait.
))

FUNCTIONS_STATUSES = _statuses(V1_STATUSES, (
    500,  # EngineError: jeff.engine.installed(refresh=True) could not reach an engine.
))

GRAPHS_STATUSES = _statuses(V1_STATUSES)  # returns graphs registered at startup; nothing else can go wrong here.

LOGIN_STATUSES = [401, 422, 429]
# Not built with jeff_server.auth.V1_DEPENDENCIES at all (no bearer token is ever involved in signing in), so none
# of these come from V1_STATUSES: 401 Unauthorised (sign-in not configured on this server, or the credentials are
# wrong); 422 InvalidRequest (the body fails SignIn's own Pydantic validation); 429 RateLimited
# (jeff_server.users.SignInGuard's own lockout after repeated failed attempts -- a different limiter from
# jeff_server.auth.RateLimiter, with its own Retry-After).

ADAPTERS_LIST_STATUSES = _statuses(V1_STATUSES, (
    500,  # EngineError: an engine could not be reached, or the engines serve different adapters (there is then
          # no single served list for the deployment to report; GET /health reports each engine instead), or
          # different bases (jeff_server.router.EngineRouter.base: no one base to list adapters for).
))

SERVE_RELOAD_STATUSES = _statuses(V1_STATUSES, (OWNER_ONLY_STATUS,), (
    404,  # UnknownFunction: an adapter name the engine does not have available to serve at all.
    422,  # InvalidRequest: the request body; a served set that would leave some router without one of its
          # targets (checked, and refused, before any engine changes); a public server, which refuses any change.
    424,  # NotInstalled: after the change is applied everywhere, a router is left without a target the engines
          # now serve -- reported this way because the engines have already changed by the time it is caught.
    500,  # EngineError: one engine failed while the others succeeded, or the engines disagree afterwards.
          # ManifestError: an adapter folder under JEFF_ADAPTER_DIRS is invalid or clashes with the catalogue
          # (jeff_server.catalogue_refresh.build_catalogue, checked before any engine changes).
))

HEALTH_STATUSES = [401, 500]
# No jeff_server.auth.V1_DEPENDENCIES at all, so no 429 here -- /health has no rate limiter of any kind, unlike
# every other route on this list. A caller that does send an Authorization header still has it checked
# (jeff_server.auth.Authenticator.caller, called directly from the handler): wrong credentials are 401. No engine
# call here can raise past the handler (jeff_server.operations.health_report's _each catches every JeffError per
# engine itself and reports it as that engine's own "down"/"broken" status, never re-raising it), but the handler
# first runs the same engines_of() type-check as GET /metrics and GET /v1/adapters: EngineError, 500.

METRICS_STATUSES = _statuses(V1_STATUSES, (OWNER_ONLY_STATUS,), (
    500,  # EngineError: the same engines_of() type-check and per-engine reachability as GET /v1/adapters.
))

SYSTEMONE_STATUSES = _statuses(V1_STATUSES, (
    403,  # NotLicensed: the named model is a proprietary adapter the caller's licences do not list
          # (jeff_server.auth.check_adapter_licence, called directly, as for /v1/decide).
    402,  # UsageLimitReached: an account key's account has used its input-token allowance for the billing period
          # (jeff_server.accounts.Meter.check, run by jeff_server.decide.caller_view before anything is decided).
    422,  # InvalidRequest/TooManyLabels: the request body, an unknown model name, orders, an image, or more
          # options/levels than this model or question type allows.
    424,  # NotInstalled: the adapter was removed from what the engines serve between jeff_server.systemone's own
          # pre-check and the engine call itself -- the same cross-worker race as /v1/decide (review focus 1).
    500,  # EngineError: the engine answered the wrong number of rows, or without input token counts.
    529,  # Busy: the engine's queue was full.
))

MODELS_STATUSES = _statuses(V1_STATUSES, (
    500,  # EngineError (jeff.engine.installed(refresh=True) could not reach an engine, or the engines serve
          # different bases) or ManifestError (bases.yaml does not list the base the engines serve, or gives it no
          # "released" date) -- both 500.
))

CONSOLE_ME_STATUSES = _statuses(V1_STATUSES)  # reads the caller and JEFF_ADMINS only; nothing else can go wrong.

CONSOLE_EVENTS_STATUSES = _statuses(V1_STATUSES, (
    422,  # InvalidRequest: the request body fails ConsoleEvent's own Pydantic validation (an unknown field, or
          # "view" longer than 100 characters).
))

CONSOLE_ACCESS_LOG_STATUSES = _statuses(V1_STATUSES, (
    403,  # NotLicensed: the caller's login is not in JEFF_ADMINS (jeff_server.console._is_admin).
    422,  # InvalidRequest: JEFF_ACCESS_LOG is not set on this server, so there is nothing to read.
))

CONSOLE_EXAMPLES_STATUSES = _statuses(V1_STATUSES, (
    500,  # EngineError: jeff.listing.function_listing's own installed(refresh=True) could not reach an engine,
          # exactly as for GET /v1/functions (FUNCTIONS_STATUSES).
))


# --- the adapter factory (/v1/factory, jeff_server.factory.routes) ------------------------------------------------
#
# Every factory route is owner-only (jeff_server.operations.owner) and rate-limited like /v1, so each starts from
# FACTORY_STATUSES: 401, 429, and 403 -- NotLicensed (a hub token, a customer's) or FactoryOff (JEFF_FACTORY=off, or
# no teacher settings for a description check, or no JEFF_FACTORY_DIR for the replays). On top of that:
#   409 Conflict: the request does not fit the run's state (a spec approved twice, a step queued while the run
#       already has a job waiting or running, a description check used twice, ...).
#   422 InvalidRequest: the body fails its own validation, the factory's checks refuse it (a ValueError, message
#       unchanged), or the run, test set, reference or replay named in the path does not exist.
#   500 EngineError: the teacher failed during a description check (the one teacher call the API makes itself).
# Request bodies are the JSON Schemas of the routes' own Pydantic models (jeff_server.factory.routes), so they
# cannot drift from what the routes accept; responses are loose objects for now (the factory's own shapes, as
# jeff-server sent them).

FACTORY_STATUSES = (AUTH_STATUS, RATE_LIMIT_STATUS, OWNER_ONLY_STATUS)
FACTORY_OWNER = (" Owner-only, like every /v1/factory route: the API key or a console session, never a hub token "
                 "(403 NotLicensed). Every build step is queued for the factory worker (jeff-server factory worker); "
                 "the API never runs one.")

# (method, path, summary, extra statuses, request body: None or (the routes' model name, required), response kind)
FACTORY_ROUTES: list[tuple[str, str, str, tuple[int, ...], tuple[str, bool] | None, str]] = [
    ("get", "/v1/factory/mode", "The factory's mode: off or owner, and what it offers.", (), None, "json"),
    ("get", "/v1/factory/worker", "Whether the factory worker is running (a heartbeat at most 15 seconds old) and "
     "the job it runs now.", (), None, "json"),
    ("post", "/v1/factory/description-checks", "The teacher checks a task description and proposes an improved one "
     "(one synchronous teacher call; also offered with the factory off when the teacher settings are given).",
     (422, 500), ("CheckDescription", True), "json"),
    ("post", "/v1/factory/runs", "Start a run from a confirmed description check; its interpretation is queued.",
     (409, 422), ("StartRun", True), "json"),
    ("get", "/v1/factory/references", "The question sets that can be imported verbatim.", (422,), None, "json"),
    ("get", "/v1/factory/references/{name}/files/{file_path}", "One file of a reference, as plain text.", (422,),
     None, "text"),
    ("post", "/v1/factory/previews", "Start a preview (the cheap steps only) from a confirmed description check.",
     (409, 422), ("StartRun", True), "json"),
    ("post", "/v1/factory/previews/import", "Start a preview from an imported question set.", (409, 422),
     ("ImportRun", True), "json"),
    ("get", "/v1/factory/previews/{run_id}", "One preview.", (422,), None, "json"),
    ("put", "/v1/factory/previews/{run_id}/spec", "Save an edited spec of a preview.", (409, 422), ("SpecBody", True),
     "json"),
    ("post", "/v1/factory/previews/{run_id}/approve", "Approve and freeze a preview's spec.", (409, 422), None,
     "json"),
    ("get", "/v1/factory/previews/{run_id}/events", "A preview's events as server-sent events.", (422,), None,
     "events"),
    ("post", "/v1/factory/runs/import", "Start a run from an imported question set; its planning is queued.",
     (409, 422), ("ImportRun", True), "json"),
    ("get", "/v1/factory/testsets", "The independent test sets.", (), None, "json"),
    ("get", "/v1/factory/testsets/{name}", "One test set, with its items and reviews.", (422,), None, "json"),
    ("post", "/v1/factory/testsets/{name}/review", "Confirm, correct or drop one item of a test set.", (422,),
     ("ReviewBody", True), "json"),
    ("post", "/v1/factory/testsets/{name}/freeze", "Freeze a test set after the owner's checks, or with a waiver.",
     (422,), ("FreezeBody", False), "json"),
    ("post", "/v1/factory/runs/{run_id}/testset/{name}", "Use a frozen test set as the run's independent test.",
     (409, 422), None, "json"),
    ("post", "/v1/factory/runs/{run_id}/again", "A new run with this run's approved spec, a new name and sizes.",
     (409, 422), ("FromRunBody", True), "json"),
    ("get", "/v1/factory/runs", "Every run, newest first, and the run the worker is busy with.", (), None, "json"),
    ("get", "/v1/factory/runs/{run_id}", "One run: its state, spec, the steps left, its job and results.", (422,),
     None, "json"),
    ("put", "/v1/factory/runs/{run_id}/spec", "Save an edited spec before approval.", (409, 422), ("SpecBody", True),
     "json"),
    ("post", "/v1/factory/runs/{run_id}/approve", "Approve and freeze the run's spec.", (409, 422), None, "json"),
    ("post", "/v1/factory/runs/{run_id}/steps/{step}", "Queue one build step of the run.", (409, 422), None, "json"),
    ("post", "/v1/factory/runs/{run_id}/run-rest", "Queue every build step the run has left, in order.", (409, 422),
     None, "json"),
    ("put", "/v1/factory/runs/{run_id}/gate", "Set the minimum accuracy to publish.", (422,), ("GateBody", True),
     "json"),
    ("post", "/v1/factory/runs/{run_id}/test-set", "Upload an independent labelled test set.", (409, 422),
     ("TestSetBody", True), "json"),
    ("post", "/v1/factory/runs/{run_id}/replay", "Save a clean, finished run as a replay.", (422,), None, "json"),
    ("get", "/v1/factory/runs/{run_id}/events", "A run's events as server-sent events: every line of its "
     "events.jsonl from line `after` on, then new lines as they are written.", (422,), None, "events"),
    ("get", "/v1/factory/replays", "The recorded replays.", (), None, "json"),
    ("get", "/v1/factory/replays/{replay_id}", "One replay, read-only.", (422,), None, "json"),
]

FACTORY_MEDIA: dict[str, tuple[str, dict[str, Any]]] = {
    "json": ("application/json", {"type": "object"}),
    "text": ("text/plain; charset=utf-8", {"type": "string"}),
    "events": ("text/event-stream", {"type": "string", "description": "Server-sent events: \"id: <line number>\" and "
                                                                        "\"data: <one event as JSON>\" per event, and "
                                                                        "a keep-alive comment every second."}),
}


def _path_parameters(path: str) -> list[dict[str, Any]]:
    names = [part[1:-1] for part in path.split("/") if part.startswith("{")]
    return [{"name": name, "in": "path", "required": True, "schema": {"type": "string"}} for name in names]


def _factory_body(model: str) -> dict[str, Any]:
    """The JSON Schema of the factory route's request model `model` (jeff_server.factory.routes). Imported here,
    not read from contract/, because the routes' models are the definition; none has nested $defs, so the schema
    stands alone."""
    from jeff_server.factory import routes

    schema: dict[str, Any] = getattr(routes, model).model_json_schema()
    if "$defs" in schema:
        raise ValueError(f"{model}'s schema has $defs, which this script does not place in components yet")
    return schema


def _factory_paths() -> dict[str, dict[str, Any]]:
    paths: dict[str, dict[str, Any]] = {}
    for method, path, summary, extra, body, media in FACTORY_ROUTES:
        media_type, schema = FACTORY_MEDIA[media]
        parameters = _path_parameters(path)
        if media == "events":
            parameters.append({"name": "after", "in": "query", "required": False,
                               "description": "Skip the first `after` events.",
                               "schema": {"type": "integer", "minimum": 0, "default": 0}})
        request_body = None if body is None else {
            "required": body[1], "content": {"application/json": {"schema": _factory_body(body[0])}}}
        paths.setdefault(path, {})[method] = _operation(
            summary, summary + FACTORY_OWNER, request_body=request_body, parameters=parameters or None,
            ok=_ok("The answer.", media_type=media_type, schema=copy.deepcopy(schema)),
            error_statuses=_statuses(FACTORY_STATUSES, extra), security=_optional_bearer())
    return paths


def _build() -> dict[str, dict[str, Any]]:
    """Every path item, keyed by its path, built from the server's own routes (apps/server/src/jeff_server:
    decide.py, auth.py, operations.py, systemone.py, openapi.py)."""
    paths: dict[str, dict[str, Any]] = {}

    paths["/v1/decide"] = {"post": _operation(
        "Run the planner: calls, a dispatch, or a graph.",
        "Exactly one of three request shapes; see DecideCallsRequest, DecideDispatchRequest and "
        "DecideGraphRequest. Every call's function (for 'calls'; for 'dispatch', the leaf a router reaches; for "
        "'graph', each node's function) is checked against the caller's licences before it is decided (403 "
        "NotLicensed), and against on_low_confidence's own escalation steps first. on_low_confidence='error' "
        "answers 409 LowConfidence carrying the abstained decision; escalation runs through function references "
        "only -- an LLM, a person or another server step runs in the SDK, not here, and is refused as 422 "
        "InvalidRequest.",
        request_body={
            "required": True,
            "content": {"application/json": {"schema": {
                "description": "Told apart by which key is present -- calls, dispatch or graph. No "
                               "discriminator property is added: the wire has none.",
                "oneOf": [_schema_ref("DecideCallsRequest"), _schema_ref("DecideDispatchRequest"),
                          _schema_ref("DecideGraphRequest")],
            }}},
        },
        ok=_ok("One reply per request shape.", "decide_response"),
        error_statuses=DECIDE_STATUSES, security=_optional_bearer(),
    )}

    paths["/v1/functions"] = {"get": _operation(
        "List every callable function.",
        "Every function of every adapter the server's engine currently serves and that has a manifest in the "
        "server's catalogue (an adapter the engine serves without a manifest is not listed: without it, nothing "
        "can call it).",
        ok=_ok("The functions this server can decide.", "functions_response"), error_statuses=FUNCTIONS_STATUSES,
        security=_optional_bearer(),
    )}

    paths["/v1/graphs"] = {"get": _operation(
        "List the decision graphs registered on this server.",
        "Graphs loaded from JEFF_GRAPHS at startup (none with code nodes, which the server cannot run); call one "
        "through POST /v1/decide with {\"graph\": {\"id\": ...}}.",
        ok=_ok("The registered graphs.", "graphs_response"), error_statuses=GRAPHS_STATUSES,
        security=_optional_bearer(),
    )}

    paths["/ui/login"] = {"post": _operation(
        "Sign in for a console session token.",
        "Checks an email and password against the server's JEFF_UI_USERS file and returns a session token, which "
        "the API then accepts in place of the API key (as an owner). 401 Unauthorised when sign-in is not "
        "configured (JEFF_UI_USERS unset) or the credentials are wrong; repeated wrong attempts answer 429 "
        "RateLimited with Retry-After instead (its own lockout, separate from the per-caller limiter every /v1 "
        "route has). Never needs a bearer token itself.",
        request_body=_json_body("login_request"),
        ok=_ok("Signed in.", "login_response"), error_statuses=LOGIN_STATUSES,
    )}

    paths["/v1/adapters"] = {"get": _operation(
        "List every catalogue adapter trained for the served base, and whether it is served.",
        "Only the adapters trained for the base the engines serve are listed (an adapter for another base can never "
        "be served here, and PUT /v1/adapters/served refuses it with 404 UnknownFunction), each with its one-line "
        "description and its functions (ID, interface, and whether it has a threshold for the served base); GET "
        "/v1/functions has each function's full detail. 500 EngineError while any engine is down, or while the "
        "engines serve different adapters (or different bases): this endpoint reports one served list for the "
        "whole deployment, which does not exist then -- GET /health reports each engine instead. See "
        "adapters_response's own description for what adapters_version means.",
        ok=_ok("The catalogue's adapters for the served base, each marked served or not.", "adapters_response"),
        error_statuses=ADAPTERS_LIST_STATUSES, security=_optional_bearer(),
    )}

    paths["/v1/adapters/served"] = {"put": _operation(
        "Replace what every engine serves.",
        "Every engine process is changed to serve exactly this set (not added to its current list), concurrently; "
        "all must accept it, or none of the engines are left changed when the check that runs before any change "
        "(a served set that would leave a router without one of its targets) fails. Validated against "
        "served_request; an adapter string must not be empty (\"\" is 422 InvalidRequest, naming where). " +
        OWNER_ONLY + " Refused as 422 InvalidRequest on a public server (JEFF_PUBLIC=1): a public server's "
        "served adapters stay as started.",
        request_body=_json_body("served_request"),
        ok=_ok("The adapters after the change.", "adapters_response"), error_statuses=SERVE_RELOAD_STATUSES,
        security=_optional_bearer(),
    )}

    paths["/v1/adapters/reload"] = {"post": _operation(
        "Make every engine re-read its served file from disk.",
        "Each engine reads the served file only it can read and scans its adapter folders again; a router left "
        "without one of its targets afterwards is reported as 424 NotInstalled, saying the engines have already "
        "changed. The server's catalogue (JEFF_CATALOGUE plus the adapter folders under JEFF_ADAPTER_DIRS) is "
        "built first, so an invalid adapter folder is 500 ManifestError with no engine changed; after the reload "
        "the server answers from the rebuilt catalogue at once. " + OWNER_ONLY + " Refused as 422 InvalidRequest "
        "on a public server.",
        ok=_ok("The adapters after the reload.", "adapters_response"), error_statuses=SERVE_RELOAD_STATUSES,
        security=_optional_bearer(),
    )}

    paths["/health"] = {"get": _operation(
        "Report whether this deployment can answer requests.",
        "Needs no credentials, and has no rate limit at all (unlike every other route here). On a public server, "
        "a caller sending none gets only HealthStatusOnly (cached for a short time, so a flood of unsigned "
        "checks is never engine traffic); a signed-in caller, and any caller on a private server, gets the full "
        "HealthReport. status: 'ready' (every engine ready, and they agree on what they serve), 'degraded' (some "
        "engine down or broken, but the ready ones agree and keep answering), or 'broken' (no engine ready, or "
        "the ready ones disagree on what they serve: then only requests for adapters every engine serves are "
        "answered).",
        ok=_ok("The deployment's health.", schema={
            "description": "Told apart by which shape matches. No discriminator property is added: the wire has "
                           "none.",
            "oneOf": [_schema_ref("HealthStatusOnly"), _schema_ref("HealthReport")],
        }),
        error_statuses=HEALTH_STATUSES,
    )}

    paths["/metrics"] = {"get": _operation(
        "Prometheus metrics for this deployment.",
        "Per engine: whether the API reached it, and its scheduler's counters (queue, batches, rows, busy, "
        "deadlines, model time per batch). Per route: HTTP responses by status. " + OWNER_ONLY,
        ok=_ok("Prometheus text format, exactly as sent.", media_type="text/plain; version=0.0.4; charset=utf-8",
              schema={"type": "string"}),
        error_statuses=METRICS_STATUSES, security=_optional_bearer(),
    )}

    paths["/v1/systemone"] = {"post": _operation(
        "jeff-server's API, kept for its existing clients.",
        "One state and one or more questions about it, each answered on its own; see systemone_request's own "
        "description. Checked in order: the request body, the model name (422 InvalidRequest for one that is "
        "neither the base nor a catalogue adapter), the caller's licence for a proprietary adapter (403 "
        "NotLicensed, before any engine sees a row), then whether the engines serve it. Every error response "
        "here, and on GET /v1/models, also carries jeff-server's legacy \"detail\" field (see LegacyErrorBody) so "
        "that existing client raises the same exceptions it always has.",
        request_body=_json_body("systemone_request"),
        ok=_ok("One answer per question.", "systemone_response"), error_statuses=SYSTEMONE_STATUSES,
        legacy_errors=True, security=_optional_bearer(),
    )}

    paths["/v1/models"] = {"get": _operation(
        "jeff-server's model list, kept for its existing clients.",
        "The base under each name it answers to (jeff, jeff-latest and the server's model name), then every "
        "adapter the engines currently serve, under its alias or ID. release_date is that of the base the engines "
        "serve (bases.yaml's \"released\"), however many bases the catalogue's adapters were trained on; engines "
        "serving different bases are 500 EngineError. Errors here also carry the legacy \"detail\" field; see "
        "POST /v1/systemone.",
        ok=_ok("The models POST /v1/systemone will answer to.", "models_response"), error_statuses=MODELS_STATUSES,
        legacy_errors=True, security=_optional_bearer(),
    )}

    paths["/ui/me"] = {"get": _operation(
        "Who is signed in.",
        "The caller's identity, and whether it is a console session signed in under a login JEFF_ADMINS lists "
        "(see ConsoleMeResponse's own admin field); the console reads this once it has a session token, to "
        "decide what to show (for example, whether to offer the access log).",
        ok=_ok("The signed-in caller.", "ConsoleMeResponse"), error_statuses=CONSOLE_ME_STATUSES,
        security=_optional_bearer(),
    )}

    paths["/ui/events"] = {"post": _operation(
        "Record a console page view.",
        "The console calls this once per view change, for the access log (jeff_server.access_log); the entry's "
        "user is \"<kind>:<subject>\", so a hub token's sub or the API key's fixed subject never merges into a "
        "console login's own row in the access log's per-user summary. logged is false when JEFF_ACCESS_LOG is "
        "not set on this server.",
        request_body=_json_body("ConsoleEventRequest"),
        ok=_ok("Whether the view was written to the access log.", "ConsoleEventResponse"),
        error_statuses=CONSOLE_EVENTS_STATUSES, security=_optional_bearer(),
    )}

    # The bounds below (1, 5000, a default of 1000) must match jeff_server.console.ACCESS_LOG_MIN/MAX/DEFAULT by
    # hand: every other value in this file is read from contract/ or written out longhand rather than imported
    # from the server's own code, and these three are no exception.
    paths["/ui/access-log"] = {"get": _operation(
        "Read the access log.",
        "Only a console session signed in under a login JEFF_ADMINS lists may read it (403 NotLicensed "
        "otherwise -- never a hub token or the API key, and never on a server with no credentials configured at "
        "all, which has no console sessions and so no admins, whatever JEFF_ADMINS says). limit outside [1, "
        "5000] is 422 InvalidRequest, never silently clamped into range. 422 InvalidRequest too when "
        "JEFF_ACCESS_LOG is not set on this server at all.",
        parameters=[{
            "name": "limit", "in": "query", "required": False,
            "description": "At most this many entries, newest first; outside [1, 5000] is 422 InvalidRequest.",
            "schema": {"type": "integer", "minimum": 1, "maximum": 5000, "default": 1000},
        }],
        ok=_ok("The access log's report.", "ConsoleAccessLogResponse"), error_statuses=CONSOLE_ACCESS_LOG_STATUSES,
        security=_optional_bearer(),
    )}

    paths["/ui/examples"] = {"get": _operation(
        "List playground examples.",
        "One example per committed golden case (contract/golden/step1.json) whose function belongs to an adapter "
        "the server serves right now, then the examples each served adapter's folder holds in "
        "playground-examples.json (rows of its test set, with the test set's answer), for the console's playground "
        "to offer as starting points.",
        ok=_ok("The examples.", "ConsoleExamplesResponse"), error_statuses=CONSOLE_EXAMPLES_STATUSES,
        security=_optional_bearer(),
    )}

    paths.update(_factory_paths())
    return paths


def _check_statuses_are_catalogued(paths: dict[str, dict[str, Any]]) -> None:
    """A per-route list above that names a status contract/errors.yaml does not define would silently document a
    status nothing can ever answer with; this is the one place the hand-maintained lists above are cross-checked
    against the catalogue, so a typo there fails build_openapi.py itself rather than shipping quietly."""
    catalogued = set(_error_statuses())
    used = {int(status) for item in paths.values() for operation in item.values()
            for status in operation["responses"] if status != "200"}
    unknown = used - catalogued
    if unknown:
        raise ValueError(f"{sorted(unknown)} are not statuses in contract/errors.yaml; fix the per-route list(s) "
                         "in scripts/build_openapi.py")


def build() -> dict[str, Any]:
    """The whole OpenAPI 3.1 document, in a fixed key order (yaml.safe_dump below writes it with sort_keys=False),
    so re-running this on an unchanged contract writes byte-identical YAML -- what
    packages/python/tests/test_openapi.py relies on to detect drift."""
    paths = _build()
    _check_statuses_are_catalogued(paths)
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Jeff HTTP API",
            "version": "1.0.0",
            "description": "The HTTP API in front of Jeff's engine processes (apps/server, package jeff-server): "
                           "POST /v1/decide, GET /v1/functions and GET /v1/graphs run the in-process SDK's "
                           "planner over the server's catalogue and engines; GET /v1/adapters, PUT "
                           "/v1/adapters/served, POST /v1/adapters/reload, GET /health and GET /metrics operate "
                           "the deployment; POST /v1/systemone and GET /v1/models are jeff-server's own API, kept "
                           "for its existing clients; POST /ui/login signs in for a console session, and GET "
                           "/ui/me, POST /ui/events, GET /ui/access-log and GET /ui/examples serve the console "
                           "itself (apps/server/src/jeff_server/console.py), behind the same credentials and rate "
                           "limit as /v1; /v1/factory/* is the adapter factory (owner-only), whose build steps "
                           "the API queues for a separate factory worker process. The console's page and its static "
                           "assets -- GET /ui, GET /ui/, GET /ui/assets/{name}, and GET / (which redirects to /ui/ "
                           "on a public server) -- are not listed under paths: they need no credentials at all "
                           "(the page signs itself in) and serve HTML and files, not JSON operations this document "
                           "describes. Every error "
                           "response has the body {\"error\": {\"type\", \"message\", \"details\"}} (ErrorBody), "
                           "or additionally \"detail\" (LegacyErrorBody) on the two legacy routes. Every "
                           "response, success or error, carries an x-request-id header (components.headers."
                           "XRequestId). The low-latency channel, a WebSocket at /v1/channel, is not an HTTP "
                           "operation and is not listed under paths: each binary WebSocket message carries one "
                           "frame of the engine socket's format (a 4-byte big-endian length, then a msgpack map); "
                           "the client authenticates once with an auth frame, binds functions to handles and then "
                           "sends only each call's input. Its operations, limits and errors are described in "
                           "contract/socket.md (\"The channel\") and contract/schemas/socket.schema.json. "
                           "The MCP (Model Context Protocol) endpoint, /mcp, is not listed under paths either: "
                           "it is MCP's streamable HTTP transport (JSON-RPC messages in POST bodies, answered as "
                           "JSON), offering one tool per "
                           "function the server can call now plus jeff_dispatch and jeff_list_functions, behind the "
                           "same bearer credentials as /v1 (401 Unauthorised without them) and the same rate limit "
                           "(each HTTP request to /mcp is one request; 429 RateLimited over it); a refused tool call "
                           "is a tool error whose text starts with the error type, e.g. \"NotLicensed: ...\".",
        },
        "paths": paths,
        "components": {
            "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer",
                                               "description": BEARER_DESCRIPTION}},
            "headers": {"XRequestId": X_REQUEST_ID_HEADER_COMPONENT},
            "schemas": {**_component_schemas(), **NAMED_VARIANT_SCHEMAS, "ErrorBody": ERROR_BODY,
                       "LegacyErrorBody": LEGACY_ERROR_BODY},
        },
    }


def main() -> None:
    document = build()
    text = yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100)
    OUTPUT_PATH.write_text(text, encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
