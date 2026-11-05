"""Record contract/conformance/: what the Python SDK (packages/python, the reference behaviour) does, written as
language-neutral JSON that every other SDK replays offline (the SDKs' design,
Section 6; the file formats are described in each file's "description").

- catalogues.json: the catalogues the cases use. "shipped" is catalogue/ as the Python SDK reads it (every manifest's
  YAML as JSON, so another SDK can check its own YAML reading). Each variant is the shipped catalogue with a few
  manifests replaced or added: copies of open catalogue/ manifests with one documented change each, and two invented
  routers. None holds private data.
- rows.json: for every golden case and for hand-picked edge calls, the engine row the Python SDK builds (as JSON and
  as exact MessagePack bytes) or the error it raises.
- decisions.json: calls through the Python client over a scripted engine (Task 2).
- socket.json: byte-exact engine socket conversations (Task 3).
- remote.json: HTTP exchanges with a real jeff-server over a fake engine (Task 4).

Inputs are invented here, or taken from a golden case only when its adapter has a public test set. A golden case of an
adapter without one is replaced by an invented call of the same function.

Run `uv run python scripts/record_conformance.py` to write the files, only for a deliberate change of behaviour; then
run `uv run python scripts/sync_shipped.py`. scripts/tests/test_record_conformance.py fails when the files differ from
what the Python SDK does now."""

import base64
import copy
import datetime
import json
import math
import shutil
import socket
import struct
import tempfile
import threading
import time
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

import httpx
import msgpack  # type: ignore[import-untyped]
import yaml
from starlette.testclient import TestClient

from jeff.binding import bind, build_row, option_keys
from jeff.catalogue import Catalogue
from jeff.client import Call, Jeff
from jeff.decision import Decision
from jeff.engine import EngineResult, format_id_of
from jeff.errors import JeffError
from jeff.escalation import Policy, Question, escalate, function_step, human, llm
from jeff.graph import Skipped, graph_from_dict
from jeff.manifest import FunctionSpec, calibration_for, manifest_from_dict
from jeff.protocol import MAX_FRAME, encode, hello
from jeff.remote import RemoteJeff, RestTransport
from jeff.routing import DEFAULT_ROUTER
from jeff.socket_engine import SocketEngine
from jeff.testing import FakeEngine
from jeff.wire import error_to_wire
from jeff_server.app import create_app
from jeff_server.settings import ServerSettings

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "contract" / "conformance"
CATALOGUE = ROOT / "catalogue"
GOLDEN = ROOT / "contract" / "golden" / "step1.json"
BF16 = {"format": "bf16"}
FORMAT_ID = "jeff-base@v1.3+int4-g64-gptq.c0ffee00"
INT4 = {"format": "int4-g64", "format_id": FORMAT_ID}
#: The 'versioned' variant's invented adapter (a copy of spam) and the alias its ID has without the version part.
VERSIONED_ID, UNVERSIONED_ID = "jeff.safety.filter-v14", "jeff.safety.filter"
INVENTED_INPUT = "An invented message for the conformance fixtures: my order arrived late and the box was damaged."


def _date_as_json(value: Any) -> str:
    """A manifest's changelog carries unquoted YAML dates (e.g. `date: 2026-10-03`), which PyYAML parses as
    datetime.date; its JSON form is the same ISO string YAML read it from. Any other type json.dumps cannot encode
    is left to raise TypeError, not swallowed here."""
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def plain(value: Any) -> Any:
    """The value as JSON reads it back (tuples become lists; a YAML date becomes its ISO string); NaN and infinities
    are refused."""
    return json.loads(json.dumps(value, allow_nan=False, ensure_ascii=False, default=_date_as_json))


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def packed(value: Any) -> str:
    return b64(msgpack.packb(value, use_bin_type=True))


def wire_error(error: JeffError) -> dict[str, Any]:
    body = error_to_wire(error)
    details = {key: value for key, value in body["details"].items() if key != "request_id"}
    return plain({"type": body["type"], "message": body["message"], "details": details})


# ---- catalogues

def shipped_raw() -> dict[str, dict[str, Any]]:
    """Every catalogue/ manifest as PyYAML reads it, by adapter ID. A manifest that is not open stops the recorder:
    the fixtures use open manifests only."""
    found: dict[str, dict[str, Any]] = {}
    for path in sorted(CATALOGUE.glob("*/manifest.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if raw["licence"]["kind"] != "open":
            raise SystemExit(f"{path} is not open; the conformance fixtures use open manifests only")
        found[raw["id"]] = raw
    return found


def _router(adapter_id: str, key: str, instructions: str, targets: dict[str, str],
            calibration: dict[str, Any] | None) -> dict[str, Any]:
    """An invented router manifest: one route function with subset labels over its targets. hub.summary is required
    by jeff_server.catalogue_refresh.check_descriptions, which a real server (used by Task 4's remote fixtures)
    refuses to build a catalogue without."""
    return {"id": adapter_id, "version": "0.1.0", "aliases": [],
            "licence": {"kind": "open", "adapter": "Apache-2.0"},
            "weights": {"open": {"repo": "conformance/router", "revision": "v0"}},
            "base": {"id": "jeff-base", "version": "v1.3", "max_options": 252},
            "hub": {"summary": f"An invented router for the conformance fixtures: {instructions}"},
            "context": [], "input": {"key": "text", "description": "Any text.", "form": "fields"},
            "functions": {key: {"interface": "route", "type": "choice", "instructions": instructions,
                                "labels": {"source": "subset", "fixed": dict(targets)},
                                "abstain_labels": [], "calibration": calibration}},
            "routes_to": list(targets),
            "tests": {"published": None, "calibration": None, "function_by": "only"}}


@cache
def variants() -> dict[str, list[dict[str, Any]]]:
    """Each variant's manifests: a copy of an open catalogue/ manifest with the one change its name says, or the two
    invented routers."""
    shipped = shipped_raw()

    def changed(adapter_id: str) -> dict[str, Any]:
        return copy.deepcopy(shipped[adapter_id])

    spaced = changed("jeff.safety.spam")  # context keys with spaces, and a key that looks like a number
    spaced["context"] = [
        {"key": "counterparty name", "description": "Who the message is from.", "required": True},
        {"key": "Deal ID", "description": "The deal the message is about.", "required": False},
        {"key": "10", "code_name": "ten", "description": "A field whose key looks like a number.", "required": False}]
    text = changed("jeff.safety.spam")  # a plain-text input, so no context
    text["context"] = []
    text["input"] = {"key": "puzzle", "description": "The whole message, as one text.", "form": "text"}
    subset_fixed = changed("jeff.safety.guard")  # fixed labels the caller may offer a subset of
    subset_fixed["functions"]["kind"]["labels"]["subset"] = True
    int4 = changed("jeff.safety.spam")  # is_spam calibrated on one INT4 base; message_type on bfloat16 only
    block = dict(int4["functions"]["is_spam"]["calibration"])
    int4["functions"]["is_spam"]["calibration_by_format"] = {
        FORMAT_ID: {**block, "threshold": 0.9, "excluded": "None: an invented entry for the conformance fixtures."}}
    uncalibrated = changed("jeff.safety.spam")
    uncalibrated["functions"]["message_type"]["calibration"] = None
    abstaining = changed("jeff.safety.guard")
    abstaining["functions"]["kind"]["abstain_labels"] = ["benign"]
    own_shape = changed("jeff.support.triage")  # one function with its own request shape
    own_shape["functions"]["needs_human"]["context"] = [
        {"key": "ticket system", "description": "The ticketing tool the message came through.", "required": True}]
    own_shape["functions"]["needs_human"]["input"] = {"key": "ticket", "description": "The ticket as received.",
                                                      "form": "fields"}
    newer_spam = changed("jeff.safety.spam")
    newer_spam["version"] = "9.9.9"
    versioned = changed("jeff.safety.spam")  # an adapter whose ID carries a version part, and its unversioned ID
    versioned["id"], versioned["aliases"] = VERSIONED_ID, ["filter-v14", UNVERSIONED_ID]
    threshold = {"data": "invented", "rows": 10, "target_precision": 0.9, "threshold": 0.8,
                 "precision_at_threshold": 0.9, "coverage_at_threshold": 0.9}
    routed = [
        _router("jeff.core.router", "domain", "Which area does this text belong to?",
                {"jeff.support.router": "Customer support", "jeff.safety.spam.message_type": "Spam and safety",
                 "jeff.support.triage.urgency": "How urgent a message is"}, threshold),
        _router("jeff.support.router", "function", "Which support decision fits this text?",
                {"jeff.support.triage.urgency": "How urgent a message is",
                 "jeff.support.triage.route": "Which team should handle a message"}, None)]
    return {"spaced": [spaced], "text": [text], "subset_fixed": [subset_fixed], "int4": [int4],
            "uncalibrated": [uncalibrated], "abstaining": [abstaining], "own_shape": [own_shape],
            "newer_spam": [newer_spam], "versioned": [versioned], "routed": routed}


@cache
def catalogue(name: str) -> Catalogue:
    shipped = Catalogue.from_dir(CATALOGUE)
    if name == "shipped":
        return shipped
    return shipped.replacing([manifest_from_dict(copy.deepcopy(raw), Path(f"conformance/{name}/{raw['id']}"))
                              for raw in variants()[name]])


def adapter_ids(name: str) -> list[str]:
    return sorted(manifest.id for manifest in catalogue(name).manifests())


def catalogues_file() -> dict[str, Any]:
    return {"description": (
                "The catalogues the conformance cases use. 'shipped' is catalogue/ as the Python SDK reads it: bases.yaml "
                "and every manifest's YAML as JSON, by adapter ID (an SDK checks its own YAML reading against it). Each "
                "variant is the shipped catalogue with these manifests in place of (or beside) the shipped ones of the "
                "same ID, each loaded with the path 'conformance/<variant>/<adapter id>': copies of open manifests with "
                "one change each (named by the variant; 'versioned' is a copy of spam under an ID with a version part, "
                "jeff.safety.filter-v14, with its unversioned ID jeff.safety.filter as an alias), and two invented "
                "routers ('routed'). A date inside a manifest "
                "(for example a changelog entry's date) is recorded as an ISO date string (YYYY-MM-DD), the same string "
                "YAML read it from; compare it as a string in every SDK."),
            "shipped": {"bases": yaml.safe_load((CATALOGUE / "bases.yaml").read_text(encoding="utf-8")),
                        "manifests": shipped_raw()},
            "variants": variants()}


# ---- manifests every SDK refuses

#: Each refused manifest: the shipped spam manifest's YAML with one line of its is_spam calibration changed.
REFUSED_MANIFESTS: list[tuple[str, str, str]] = [
    ("a calibration threshold of .nan", "threshold: 0.515807", "threshold: .nan"),
    ("a precision_at_threshold of .nan", "precision_at_threshold: 0.991914", "precision_at_threshold: .nan"),
    ("a calibration threshold of .inf", "threshold: 0.515807", "threshold: .inf"),
    ("a coverage_at_threshold of -.inf", "coverage_at_threshold: 1.0", "coverage_at_threshold: -.inf"),
]


def _refused_yaml(line: str, replacement: str) -> str:
    """The spam manifest's text with the is_spam calibration's `line` (its first occurrence, which is is_spam's)
    replaced; the line must be there, or the recorder stops."""
    text = (CATALOGUE / "jeff.safety.spam" / "manifest.yaml").read_text(encoding="utf-8")
    indented = f"\n      {line}\n"
    if indented not in text:
        raise SystemExit(f"catalogue/jeff.safety.spam/manifest.yaml has no line {line!r}; update REFUSED_MANIFESTS")
    return text.replace(indented, f"\n      {replacement}\n", 1)


#: Each refused alias: the shipped spam manifest's YAML with its ID and aliases (the lines before `licence:`) replaced.
REFUSED_ALIASES: list[tuple[str, str]] = [
    ("an alias of the ID form on an adapter whose ID has no version part",
     "id: jeff.safety.spam\nversion: 1.4.0\naliases:\n- spam\n- jeff.safety.filter\n"),
    ("an alias of the ID form that is not the ID without its version part",
     "id: jeff.safety.spam-v14\nversion: 1.4.0\naliases:\n- spam\n- jeff.safety.spam-v15\n"),
    ("an alias of the ID form on an adapter whose ID has two version parts",
     "id: jeff.safety.spam-v14-v2\nversion: 1.4.0\naliases:\n- spam\n- jeff.safety.spam\n"),
    ("an alias of the ID form on an adapter whose name is only a version part",
     "id: jeff.safety.v14\nversion: 1.4.0\naliases:\n- spam\n- jeff.safety.spam\n"),
    ("an alias equal to the adapter's own ID",
     "id: jeff.safety.spam-v14\nversion: 1.4.0\naliases:\n- spam\n- jeff.safety.spam-v14\n"),
    ("the same alias listed twice",
     "id: jeff.safety.spam\nversion: 1.4.0\naliases:\n- spam\n- spam\n"),
]
SPAM_HEADER = "id: jeff.safety.spam\nversion: 1.4.0\naliases:\n- spam\n"


#: Each refused access: the shipped spam manifest's YAML with an `access:` line added at its end, a value the manifest
#: schema refuses (access is "owner" or {accounts: [account ids, at least one, each once]}).
REFUSED_ACCESS: list[tuple[str, str]] = [
    ("an access that is a string other than owner", "access: everyone\n"),
    ("an access with an empty list of accounts", "access: {accounts: []}\n"),
    ("an access naming an account id outside the account id pattern", "access: {accounts: [acct 1]}\n"),
    ("an access naming an account twice", "access: {accounts: [acct1, acct1]}\n"),
    ("an access with a field besides accounts", "access: {accounts: [acct1], owner: true}\n"),
    ("an access that is a list instead of an object", "access: [acct1]\n"),
]


def _refused_access_yaml(line: str) -> str:
    text = (CATALOGUE / "jeff.safety.spam" / "manifest.yaml").read_text(encoding="utf-8")
    if "\naccess:" in text:
        raise SystemExit("catalogue/jeff.safety.spam/manifest.yaml already has access; update REFUSED_ACCESS")
    return text + line


def _refused_alias_yaml(header: str) -> str:
    text = (CATALOGUE / "jeff.safety.spam" / "manifest.yaml").read_text(encoding="utf-8")
    if not text.startswith(SPAM_HEADER):
        raise SystemExit(f"catalogue/jeff.safety.spam/manifest.yaml does not start with {SPAM_HEADER!r}; update "
                         "REFUSED_ALIASES")
    return header + text[len(SPAM_HEADER):]


def manifest_case(name: str, line: str, replacement: str) -> dict[str, Any]:
    return _refused_case(name, _refused_yaml(line, replacement))


def _refused_case(name: str, text: str) -> dict[str, Any]:
    path = f"conformance/manifests/{name.replace(' ', '_')}/manifest.yaml"
    try:
        manifest_from_dict(yaml.safe_load(text), Path(path))
    except JeffError as error:
        return {"name": name, "path": path, "yaml": text, "expect": {"error": wire_error(error)}}
    raise SystemExit(f"the Python SDK read the manifest of the case {name!r}; it must refuse it")


def manifests_file() -> dict[str, Any]:
    return {"description": (
                "Manifests every SDK must refuse. Each case is a manifest's YAML text ('yaml'), read as a manifest "
                "file with the path 'path' (the YAML parsed, then checked as a manifest), and the error the Python SDK "
                "raises for it ('expect'). The cases are the shipped jeff.safety.spam manifest with one number of its "
                "is_spam calibration made non-finite (.nan, .inf, -.inf): JSON has no such number, and a threshold of "
                "nan would turn calibration off without a word, since `confidence < nan` is always false while the "
                "decision still reads as calibrated. An SDK refuses each with a ManifestError or ContractError naming "
                "the field and the value; one that reads YAML as the Python SDK does gives the recorded message. The "
                "last cases give the spam manifest an alias of the ID form it may not have (only the adapter's ID "
                "without its version part may be one: refused for an ID with no version part, two of them, or only "
                "one, and for an alias that is the ID itself or another ID), or the same alias twice, each refused "
                "with the recorded ManifestError. The access cases give it an access value the manifest schema "
                "refuses (access is \"owner\" or an object whose only field, accounts, lists one or more account "
                "ids, each once, of the accounts_keys schema's pattern); each is refused with a ContractError: an SDK "
                "whose schema checker words problems as the Python SDK's does gives the recorded message, and every "
                "SDK's message starts with its first line and names /access."),
            "cases": [manifest_case(*refused) for refused in REFUSED_MANIFESTS]
                     + [_refused_case(name, _refused_alias_yaml(header)) for name, header in REFUSED_ALIASES]
                     + [_refused_case(name, _refused_access_yaml(line)) for name, line in REFUSED_ACCESS]}


# ---- calls

def invented_labels(function: FunctionSpec) -> Any:
    spec = function.labels
    if function.type != "choice" or spec.source == "fixed":
        return None
    if spec.source == "caller":
        return {"refunds": "Money back for an order", "deliveries": "Where an order is",
                "accounts": "Signing in and passwords"}
    if spec.source == "subset":
        return [*spec.required, *[key for key in spec.fixed if key not in spec.required][:1]]
    return {key: f"Invented description of {key}" for key in spec.keys}


def invented_call(function: FunctionSpec) -> dict[str, Any]:
    """A call of `function` with invented context values and labels of its label form."""
    call: dict[str, Any] = {"function": function.id, "input": INVENTED_INPUT}
    context = {field.code_name: f"Invented {field.code_name.replace('_', ' ')} for the conformance fixtures."
               for field in function.context}
    if context:
        call["context"] = context
    labels = invented_labels(function)
    if labels is not None:
        call["labels"] = labels
    return call


def golden_calls() -> list[tuple[str, dict[str, Any]]]:
    """Every golden case's call when its adapter has a public test set, else an invented call of its function."""
    shipped = catalogue("shipped")
    calls = []
    for case in json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"]:
        function = shipped.function(case["call"]["function"])
        if shipped.manifest_of(function).tests["published"] is not None:
            calls.append((f"golden: {case['name']}", case["call"]))
        else:
            calls.append((f"golden, invented call (its adapter has no public test set): {function.id}",
                          invented_call(function)))
    return calls


# ---- rows

CTX = {"company": "A furniture shop.", "channel": "email"}
SMS = {"channel": "sms"}
TEAMS = {"refunds": "Money back for an order", "deliveries": "Where an order is"}
SOC = {"organisation": "A regional hospital.", "playbook": "Isolate infected machines first."}
DESK = {"desk": "A European equities desk.", "market_context": "Markets are calm."}
GUARD = {"application": "A customer support chatbot.", "source": "user"}
VENUES = {"venue_c": "The dark pool", "venue_a": "The main exchange", "venue_b": "The second exchange"}

# (name, catalogue, base format, call)
EDGE_ROWS: list[tuple[str, str, dict[str, str], dict[str, Any]]] = [
    ("context: a required field missing", "shipped", BF16,
     {"function": "spam.message_type", "input": "WIN NOW", "context": {}}),
    ("context: none given to a function that needs some", "shipped", BF16,
     {"function": "spam.message_type", "input": "WIN NOW"}),
    ("context: a field the function does not have", "shipped", BF16,
     {"function": "spam.message_type", "input": "WIN NOW", "context": {"channel": "sms", "colour": "red"}}),
    ("context: by code name; keys with spaces and a key that looks like a number keep the manifest's order",
     "spaced", BF16, {"function": "spam.message_type", "input": "Please sign",
                      "context": {"ten": "x", "deal_id": "D-7", "counterparty_name": "Acme Ltd"}}),
    ("context: an optional field left out", "spaced", BF16,
     {"function": "spam.is_spam", "input": "Please sign", "context": {"counterparty_name": "Acme Ltd"}}),
    ("context: a field given by its exact key is refused, naming its code name", "spaced", BF16,
     {"function": "spam.is_spam", "input": "Please sign", "context": {"counterparty name": "Acme Ltd"}}),
    ("context: a function's own request shape", "own_shape", BF16,
     {"function": "triage.needs_human", "input": "Call me back", "context": {"ticket_system": "Zendesk"}}),
    ("context: the adapter's fields are not the function's own", "own_shape", BF16,
     {"function": "triage.needs_human", "input": "Call me back", "context": CTX}),
    ("text input: the state is the input itself", "text", BF16,
     {"function": "spam.message_type", "input": "WIN A PRIZE NOW"}),
    ("text input: context is refused", "text", BF16,
     {"function": "spam.message_type", "input": "WIN A PRIZE NOW", "context": SMS}),
    ("text input: an input that is not a string is refused", "text", BF16,
     {"function": "spam.message_type", "input": 42}),
    ("input: nested JSON is sent as given", "shipped", BF16,
     {"function": "spam.message_type", "context": SMS,
      "input": {"subject": "Hi", "lines": ["a", "b"], "score": 2.5, "flag": True, "none": None}}),
    ("input: text outside ASCII", "shipped", BF16,
     {"function": "spam.message_type", "input": "Gewinnen Sie jetzt \U0001f4b6, café", "context": SMS}),
    ("caller labels: renamed on the wire after the fixed first labels", "shipped", BF16,
     {"function": "triage.route", "input": "My sofa is late", "context": CTX, "labels": TEAMS}),
    ("caller labels: kept as given when the function has no wire names", "shipped", BF16,
     {"function": "jeff.support.support-intents.intent", "input": "Where is my refund?",
      "context": {"service": "An online shop."}, "labels": TEAMS}),
    ("caller labels: none given", "shipped", BF16,
     {"function": "triage.route", "input": "My sofa is late", "context": CTX}),
    ("caller labels: an empty object", "shipped", BF16,
     {"function": "triage.route", "input": "My sofa is late", "context": CTX, "labels": {}}),
    ("caller labels: a list instead of an object", "shipped", BF16,
     {"function": "triage.route", "input": "My sofa is late", "context": CTX, "labels": ["refunds"]}),
    ("caller labels: a key that looks like a number", "shipped", BF16,
     {"function": "triage.route", "input": "My sofa is late", "context": CTX, "labels": {"12": "Twelve"}}),
    ("caller labels: a reserved key", "shipped", BF16,
     {"function": "triage.route", "input": "My sofa is late", "context": CTX, "labels": {"other": "Something"}}),
    ("caller labels: more than the function was trained on", "shipped", BF16,
     {"function": "triage.route", "input": "My sofa is late", "context": CTX,
      "labels": {f"team{n}": f"Team {n}" for n in range(41)}}),
    ("subset labels: the keys offered, in the caller's order", "shipped", BF16,
     {"function": "jeff.security.soc.action", "input": "Ransomware on one laptop", "context": SOC,
      "labels": ["block", "ignore", "investigate", "escalate"]}),
    ("subset labels: a required key missing", "shipped", BF16,
     {"function": "jeff.security.soc.action", "input": "x", "context": SOC, "labels": ["ignore", "investigate"]}),
    ("subset labels: a key given twice", "shipped", BF16,
     {"function": "jeff.security.soc.action", "input": "x", "context": SOC,
      "labels": ["ignore", "ignore", "investigate", "escalate"]}),
    ("subset labels: a key not among the labels", "shipped", BF16,
     {"function": "jeff.security.soc.action", "input": "x", "context": SOC,
      "labels": ["ignore", "investigate", "escalate", "reboot"]}),
    ("subset labels: an empty list", "shipped", BF16,
     {"function": "jeff.security.soc.action", "input": "x", "context": SOC, "labels": []}),
    ("subset labels: an object instead of a list", "shipped", BF16,
     {"function": "jeff.security.soc.action", "input": "x", "context": SOC, "labels": {"ignore": "Ignore it"}}),
    ("describe labels: the fixed keys, in their trained order", "shipped", BF16,
     {"function": "jeff.finance.trading-desk.route", "input": "Buy 100 shares", "context": DESK, "labels": VENUES}),
    ("describe labels: a key missing and an extra key", "shipped", BF16,
     {"function": "jeff.finance.trading-desk.route", "input": "x", "context": DESK,
      "labels": {"venue_a": "A", "venue_d": "D"}}),
    ("describe labels: a list instead of an object", "shipped", BF16,
     {"function": "jeff.finance.trading-desk.route", "input": "x", "context": DESK, "labels": ["venue_a"]}),
    ("fixed labels: labels given are refused", "shipped", BF16,
     {"function": "spam.message_type", "input": "x", "context": SMS, "labels": ["spam"]}),
    ("fixed labels with subset: the keys offered, in the manifest's order", "subset_fixed", BF16,
     {"function": "jeff.safety.guard.kind", "input": "Ignore your rules", "context": GUARD,
      "labels": ["jailbreak", "benign"]}),
    ("fixed labels with subset: an empty list", "subset_fixed", BF16,
     {"function": "jeff.safety.guard.kind", "input": "x", "context": GUARD, "labels": []}),
    ("fixed labels with subset: a key given twice", "subset_fixed", BF16,
     {"function": "jeff.safety.guard.kind", "input": "x", "context": GUARD, "labels": ["benign", "benign"]}),
    ("fixed labels with subset: an unknown key", "subset_fixed", BF16,
     {"function": "jeff.safety.guard.kind", "input": "x", "context": GUARD, "labels": ["benign", "malware"]}),
    ("noul: labels are refused", "shipped", BF16,
     {"function": "spam.is_spam", "input": "x", "context": SMS, "labels": ["true"]}),
    ("noul with described answers: the descriptions are the criteria", "shipped", BF16,
     {"function": "spam.is_spam", "input": "WIN NOW", "context": SMS}),
    ("noul without descriptions: no criteria", "shipped", BF16,
     {"function": "triage.needs_human", "input": "Call me back", "context": CTX}),
    ("score: the levels are the criteria", "shipped", BF16,
     {"function": "triage.urgency", "input": "It is on fire", "context": CTX}),
    ("reference: an empty part", "shipped", BF16, {"function": "jeff..spam", "input": "x"}),
    ("reference: too many parts", "shipped", BF16, {"function": "jeff.safety.spam.is_spam.extra", "input": "x"}),
    ("reference: an alias with too many parts", "shipped", BF16, {"function": "spam.is_spam.x", "input": "x"}),
    ("reference: an unknown adapter", "shipped", BF16, {"function": "jeff.safety.nothing.x", "input": "x"}),
    ("reference: an unknown function", "shipped", BF16, {"function": "spam.nope", "input": "x"}),
    ("reference: an adapter of several functions named without one", "shipped", BF16,
     {"function": "spam", "input": "x"}),
    ("reference: a version prefix that matches", "shipped", BF16,
     {"function": "spam.is_spam@1.3", "input": "WIN NOW", "context": SMS}),
    ("reference: a version that does not match", "shipped", BF16,
     {"function": "spam.is_spam@2", "input": "WIN NOW", "context": SMS}),
    ("reference: the ID without its version part names the versioned adapter", "versioned", BF16,
     {"function": f"{UNVERSIONED_ID}.is_spam", "input": "WIN NOW", "context": SMS}),
    ("reference: the ID without its version part, with a version", "versioned", BF16,
     {"function": f"{UNVERSIONED_ID}.is_spam@1.4", "input": "WIN NOW", "context": SMS}),
    ("reference: the versioned ID still names its adapter", "versioned", BF16,
     {"function": f"{VERSIONED_ID}.is_spam", "input": "WIN NOW", "context": SMS}),
    ("INT4: a function with an entry for the format binds", "int4", INT4,
     {"function": "spam.is_spam", "input": "WIN NOW", "context": SMS}),
    ("INT4: a calibrated function without an entry is refused before its context is checked", "int4", INT4,
     {"function": "spam.message_type", "input": "WIN NOW"}),
    ("INT4: an uncalibrated function stays uncalibrated", "uncalibrated", INT4,
     {"function": "spam.message_type", "input": "WIN NOW", "context": SMS}),
]


def row_case(name: str, catalogue_name: str, base_format: dict[str, str], call: dict[str, Any]) -> dict[str, Any]:
    """The row Jeff._binding and build_row make for `call` (calibration resolved before binding, as there)."""
    cat = catalogue(catalogue_name)
    try:
        function = cat.function(call["function"])
        manifest = cat.manifest_of(function)
        calibration = calibration_for(function, format_id_of(base_format))
        binding = bind(manifest, function, call.get("context"), call.get("labels"), calibration=calibration)
        row = build_row(binding, call["input"])
        expect: dict[str, Any] = {"adapter": manifest.id, "row": row, "row_msgpack": packed(row)}
    except JeffError as error:
        expect = {"error": wire_error(error)}
    return plain({"name": name, "catalogue": catalogue_name, "base_format": base_format, "call": call,
                  "expect": expect})


def rows_file() -> dict[str, Any]:
    cases = [row_case(name, "shipped", BF16, call) for name, call in golden_calls()]
    cases += [row_case(*edge) for edge in EDGE_ROWS]
    return {"description": (
                "The engine row the Python SDK builds for a call, or the error it raises. The call is bound as "
                "Jeff._binding binds it: the function is resolved in the catalogue, its calibration chosen for the "
                "base format (refused there for an INT4 base without its entry), then context and labels are bound "
                "and the row is built. 'row' is the row as JSON; 'row_msgpack' is base64 of the row packed with "
                "MessagePack, which pins the order of every key. Golden cases use the golden call when the adapter "
                "has a public test set, else an invented call of the same function."),
            "cases": cases}


# ---- decisions

ELAPSED = "<elapsed>"
URG, SENT = "jeff.support.triage.urgency", "jeff.support.triage.sentiment"
LOW_URGENCY = [0.3125, 0.171875, 0.171875, 0.171875, 0.171875]  # level 0 at 0.3125, below urgency's threshold
SURE_LEVEL_2 = [0.0, 0.0, 0.9, 0.1, 0.0]


NOT_FINITE = {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf}


def json_float(value: float) -> float | str:
    """A probability as decisions.json writes it: JSON has no NaN or infinities, so those are written as the strings
    "NaN", "Infinity" and "-Infinity" (contract/socket.md, "The probabilities of a predict")."""
    if math.isfinite(value):
        return value
    return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")


class ScriptedEngine:
    """An Engine (jeff.engine.Engine) whose n-th predict answers script[n] (one list of probabilities per row) and
    records what it was asked."""

    def __init__(self, installed: set[str], base_format: dict[str, str], script: list[list[list[float]]]) -> None:
        self._installed, self.base_format, self.script = set(installed), dict(base_format), list(script)
        self.predicts: list[dict[str, Any]] = []

    def installed(self, refresh: bool = False) -> set[str]:
        return set(self._installed)

    def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
        if not self.script:
            raise SystemExit(f"The script has no answer left for a predict of {adapter_id}")
        probabilities = self.script.pop(0)
        if not all(isinstance(p, float) for row in probabilities for p in row):
            raise SystemExit(f"Scripted probabilities must be floats, as an engine sends them: {probabilities}")
        timing = {"path": "fake", "model_ms": 0.0, **self.base_format}
        self.predicts.append({"adapter": adapter_id, "rows": plain(rows), "rows_msgpack": packed(rows),
                              "probabilities": [[json_float(p) for p in row] for row in probabilities],
                              "timing": timing})
        return EngineResult([list(row) for row in probabilities], dict(timing), None)


def _asker(name: str, answer: str, questions: list[dict[str, Any]]) -> Callable[[Question], str]:
    def ask(question: Question) -> str:
        questions.append({"step": name, "question": {"function": question.function,
                                                     "instructions": question.instructions,
                                                     "state": question.state, "options": question.options}})
        return answer
    return ask


def policy_from(spec: Any, questions: list[dict[str, Any]]) -> Any:
    """A fixture policy as a Python policy; any string is passed as it is (so an unknown one is refused by Jeff)."""
    if isinstance(spec, str):
        return spec
    steps: list[Any] = []
    for step in spec["escalate"]:
        if "function" in step:
            steps.append(function_step(step["function"]))
        elif "llm" in step:
            steps.append(llm(_asker("llm", step["llm"], questions)))
        elif "human" in step:
            steps.append(human(_asker("human", step["human"], questions)))
        else:
            raise SystemExit(f"Unknown escalation step {step}")
    return escalate(*steps, budget_ms=spec.get("budget_ms"))


def _call(spec: dict[str, Any]) -> Call:
    return Call(spec["function"], spec["input"], spec.get("context"), spec.get("labels"))


def _result_json(result: Any) -> dict[str, Any]:
    if isinstance(result, Decision):
        return {"decision": result.to_json()}
    if isinstance(result, Skipped):
        return result.to_json()
    return {"code": copy.deepcopy(result)}


def _coder(name: str, value: Any, calls: list[dict[str, Any]]) -> Callable[[dict[str, Any]], Any]:
    def run(arguments: dict[str, Any]) -> Any:
        calls.append({"code": name, "input": arguments["input"], "params": arguments["params"],
                      "nodes": {node: _result_json(result) for node, result in arguments["nodes"].items()}})
        return copy.deepcopy(value)
    return run


def run_op(jeff: Jeff, op: str, args: dict[str, Any], questions: list[dict[str, Any]]) -> Any:
    policy = policy_from(args.get("policy", "return"), questions)
    if op == "decide":
        return [decision.to_json() for decision in jeff.decide([_call(spec) for spec in args["calls"]])]
    if op == "function":
        bound = jeff.function(args["ref"], context=args.get("context"), labels=args.get("labels"),
                              on_low_confidence=policy)
        return [decision.to_json() for decision in bound.many(args["inputs"])]
    if op == "shorthand":
        shorthand = getattr(jeff, args["interface"])
        return shorthand(args["input"], function=args.get("function"), context=args.get("context"),
                         labels=args.get("labels"), on_low_confidence=policy).to_json()
    if op == "dispatch":
        return jeff.dispatch(args["input"], interface=args["interface"], context=args.get("context"),
                             labels=args.get("labels"), router=args.get("router", DEFAULT_ROUTER),
                             on_low_confidence=policy).to_json()
    if op == "graph":
        graph = graph_from_dict(copy.deepcopy(args["graph"]), "the conformance graph")
        calls: list[dict[str, Any]] = []
        code = {name: _coder(name, value, calls) for name, value in args.get("code", {}).items()}
        policies = {node: policy_from(spec, questions) for node, spec in args.get("policies", {}).items()}
        result = jeff.run_graph(graph, args["input"], args["params"], code, policies)
        return {"graph_result": result.to_json(), "code_calls": calls}
    raise SystemExit(f"Unknown op {op}")


def normalised(value: Any) -> Any:
    """`value` with the timing.ms of every decision an llm or human step made replaced by ELAPSED."""
    if isinstance(value, list):
        return [normalised(item) for item in value]
    if not isinstance(value, dict):
        return value
    out = {key: normalised(item) for key, item in value.items()}
    if out.get("decided_by") in ("llm", "human") and isinstance(out.get("timing"), dict) and "ms" in out["timing"]:
        out["timing"] = {**out["timing"], "ms": ELAPSED}
    return out


def run_decision_case(spec: dict[str, Any]) -> dict[str, Any]:
    engine = ScriptedEngine(set(spec["installed"]), spec["base_format"], copy.deepcopy(spec["script"]))
    questions: list[dict[str, Any]] = []
    try:
        jeff = Jeff(engine, catalogue(spec["catalogue"]))
        expect: dict[str, Any] = {"result": normalised(plain(run_op(jeff, spec["op"], spec["args"], questions)))}
    except JeffError as error:
        expect = {"error": normalised(wire_error(error))}
    if engine.script:
        raise SystemExit(f"{spec['name']}: {len(engine.script)} scripted answers were not used")
    return plain({key: spec[key] for key in ("name", "group", "catalogue", "base_format", "installed", "op", "args")}
                 | {"predicts": engine.predicts, "questions": questions, "expect": expect})


def case(name: str, group: str, op: str, args: dict[str, Any], script: list[list[list[float]]], *,
         catalogue_name: str = "shipped", base_format: dict[str, str] = BF16,
         installed: list[str] | None = None) -> dict[str, Any]:
    return {"name": name, "group": group, "catalogue": catalogue_name, "base_format": base_format,
            "installed": installed if installed is not None else adapter_ids(catalogue_name), "op": op,
            "args": args, "script": script}


def _spread(n: int, winner: int, p: float) -> list[float]:
    rest = (1.0 - p) / (n - 1) if n > 1 else 0.0
    return [p if index == winner else rest for index in range(n)]


def engine_answers(kind: str, n: int, threshold: float | None) -> list[tuple[str, list[float]]]:
    """Engine answers for one function of `n` options: both sides of its threshold, a tie, a sum just within the
    tolerance of 1 (contract/socket.md, "The probabilities of a predict") and four answers the client refuses."""
    t = 0.5 if threshold is None else threshold
    high = round((1.0 + t) / 2, 6)
    answers = [("confident first option", _spread(n, 0, high))]
    low = round(t - 0.01, 6)
    if low > max(1.0 / n, 0.5 if kind == "noul" else 0.0) + 0.001:
        answers.append(("first option just below the threshold", _spread(n, 0, low)))
    answers.append(("last option wins", _spread(n, n - 1, high)))
    answers.append(("tie between the first two options",
                    [0.5, 0.5] if n == 2 else [0.4, 0.4, *([0.2 / (n - 2)] * (n - 2))]))
    answers.append(("a sum just within the tolerance of 1", [p * (1 - 9e-5) for p in _spread(n, 1, high)]))
    answers.append(("a sum just outside the tolerance of 1", [p * (1 - 1.1e-4) for p in _spread(n, 1, high)]))
    answers.append(("one probability too many", _spread(n + 1, 0, high)))
    answers.append(("all zero", [0.0] * n))
    answers.append(("a negative probability", [-0.25, *_spread(n - 1, 0, 0.9)]))
    return answers


def generated_cases() -> list[dict[str, Any]]:
    shipped = catalogue("shipped")
    cases = []
    for manifest in shipped.manifests():
        for function in manifest.functions.values():
            call = invented_call(function)
            binding = bind(manifest, function, call.get("context"), call.get("labels"),
                           calibration=function.calibration)
            threshold = function.calibration.threshold if function.calibration else None
            for label, probabilities in engine_answers(function.type, len(option_keys(binding)), threshold):
                cases.append(case(f"{function.id}: {label}", "decide", "decide", {"calls": [call]},
                                  [[probabilities]]))
    return cases


def _call_of(function: str, input: Any, context: dict[str, Any] | None = None, labels: Any = None) -> dict[str, Any]:
    call: dict[str, Any] = {"function": function, "input": input}
    if context is not None:
        call["context"] = context
    if labels is not None:
        call["labels"] = labels
    return call


def _bound(ref: str, inputs: list[Any], policy: Any, context: dict[str, Any] | None = CTX,
           labels: Any = None) -> dict[str, Any]:
    args: dict[str, Any] = {"ref": ref, "inputs": inputs, "policy": policy}
    if context is not None:
        args["context"] = context
    if labels is not None:
        args["labels"] = labels
    return args


GRAPH: dict[str, Any] = {
    "graph": "jeff.graphs.conformance", "version": "1.0.0", "input": "message",
    "params": ["company", "channel", "teams"],
    "nodes": {
        "kind": {"call": "jeff.safety.spam.message_type", "context": {"channel": {"param": "channel"}}},
        "team": {"call": "jeff.support.triage.route",
                 "context": {"company": {"param": "company"}, "channel": {"param": "channel"}},
                 "labels": {"param": "teams"}, "when": {"node": "kind", "is": "legitimate"}},
        "urgency": {"call": URG, "context": {"company": {"param": "company"}, "channel": {"param": "channel"}},
                    "when": {"node": "team", "abstained": False}},
        "ticket": {"code": "make_ticket", "after": ["team", "urgency"]}},
    "outputs": ["kind", "team", "urgency", "ticket"]}
GRAPH_NO_CODE: dict[str, Any] = {**GRAPH, "nodes": {k: v for k, v in GRAPH["nodes"].items() if k != "ticket"},
                                 "outputs": ["kind", "team", "urgency"]}
PARAMS = {"company": "A furniture shop.", "channel": "email", "teams": TEAMS}
NO_SPAM = [adapter for adapter in adapter_ids("shipped") if adapter != "jeff.safety.spam"]
NO_GUARD = [adapter for adapter in adapter_ids("shipped") if adapter != "jeff.safety.guard"]


def broken_reply_cases(spam_sms: dict[str, Any]) -> list[dict[str, Any]]:
    """An engine reply that breaks its contract (contract/socket.md, "The probabilities of a predict") is refused,
    for a yes/no, a choice and a score function: NaN, the infinities, a probability above 1 (whose sum is within the
    tolerance, so only the range refuses it), and a sum that overflows float64. The other refusals (a count, a negative, all zero, a sum outside the tolerance) are among
    each function's engine_answers."""
    functions = [("spam.is_spam", spam_sms, 2), ("spam.message_type", spam_sms, 3), ("triage.urgency", CTX, 5)]
    broken = [("a NaN probability", lambda n: [math.nan, *_spread(n - 1, 0, 1.0)]),
              ("an infinite probability", lambda n: [math.inf, *([0.0] * (n - 1))]),
              ("a probability of minus infinity", lambda n: [*_spread(n - 1, 0, 1.0), -math.inf]),
              ("a probability above 1", lambda n: [1.00005, *([0.0] * (n - 1))]),  # its sum within the tolerance
              ("a sum that overflows", lambda n: [1e308] * n)]
    return [case(f"an engine reply with {label} is refused: {ref}", "decide", "decide",
                 {"calls": [_call_of(ref, "WIN", context)]}, [[answer(n)]])
            for ref, context, n in functions for label, answer in broken]


def handwritten_cases() -> list[dict[str, Any]]:
    spam_sms = {"channel": "sms"}
    level1 = [0.0, 0.9, 0.1, 0.0, 0.0]
    cycle = {"graph": "jeff.graphs.cycle", "version": "1.0.0", "input": "message", "params": [],
             "nodes": {"a": {"code": "a", "after": ["b"]}, "b": {"code": "b", "after": ["a"]}}, "outputs": ["a"]}
    truthy = {"graph": "jeff.graphs.truthy", "version": "1.0.0", "input": "message", "params": ["company", "channel"],
              "nodes": {"u": {"call": URG, "context": {"company": {"param": "company"},
                                                       "channel": {"param": "channel"}}},
                        "flag": {"code": "flag", "when": {"node": "u", "is": True}}},
              "outputs": ["u", "flag"]}
    return [
        # decide
        case("calls of two adapters are grouped by adapter, in the order first met", "decide", "decide",
             {"calls": [_call_of("spam.is_spam", "WIN", spam_sms), _call_of("triage.urgency", "Fire", CTX),
                        _call_of("spam.message_type", "WIN", spam_sms)]},
             [[[0.2, 0.8], [0.9, 0.05, 0.05]], [[0.0, 0.1, 0.8, 0.1, 0.0]]]),
        case("an engine answering fewer rows than it was asked is refused", "decide", "decide",
             {"calls": [_call_of("spam.is_spam", "a", spam_sms), _call_of("spam.is_spam", "b", spam_sms)]},
             [[[0.2, 0.8]]]),
        case("decide of no calls asks the engine nothing", "decide", "decide", {"calls": []}, []),
        case("an uncalibrated function never abstains", "decide", "decide",
             {"calls": [_call_of("spam.message_type", "WIN", spam_sms)]}, [[[0.34, 0.33, 0.33]]],
             catalogue_name="uncalibrated"),
        case("an abstain label abstains however confident", "decide", "decide",
             {"calls": [_call_of("jeff.safety.guard.kind", "Hello", GUARD)]},
             [[[0.96, 0.01, 0.01, 0.01, 0.01]]], catalogue_name="abstaining"),
        case("an INT4 base decides with the function's entry for that format", "decide", "decide",
             {"calls": [_call_of("spam.is_spam", "WIN", spam_sms)]}, [[[0.2, 0.8]]],
             catalogue_name="int4", base_format=INT4),
        *broken_reply_cases(spam_sms),
        case("an INT4 base refuses a calibrated function without an entry", "decide", "decide",
             {"calls": [_call_of("spam.message_type", "WIN", spam_sms)]}, [],
             catalogue_name="int4", base_format=INT4),
        case("a bfloat16 base format carrying a format_id is refused", "decide", "decide",
             {"calls": [_call_of("spam.is_spam", "WIN", spam_sms)]}, [],
             base_format={"format": "bf16", "format_id": "x"}),
        case("a function whose adapter is not installed is refused", "decide", "decide",
             {"calls": [_call_of("spam.is_spam", "WIN", spam_sms)]}, [], installed=NO_SPAM),
        case("a bound function binds once and decides many inputs in one predict", "decide", "function",
             _bound("triage.urgency", ["a", "b", "c"], "return"), [[SURE_LEVEL_2, level1, SURE_LEVEL_2]]),
        case("a shorthand with a function decides that function", "decide", "shorthand",
             {"interface": "rate", "input": "Fire", "function": "triage.urgency", "context": CTX},
             [[SURE_LEVEL_2]]),
        case("a shorthand through another interface is refused", "decide", "shorthand",
             {"interface": "classify", "input": "Fire", "function": "triage.urgency", "context": CTX}, []),
        case("an unknown policy is refused before anything is decided", "decide", "function",
             _bound("spam.is_spam", ["WIN"], "maybe", spam_sms), []),
        case("a call through the ID without its version part reports the versioned function", "decide", "decide",
             {"calls": [_call_of(f"{UNVERSIONED_ID}.is_spam", "WIN", spam_sms)]}, [[[0.2, 0.8]]],
             catalogue_name="versioned"),
        case("a function bound through the ID without its version part reports the versioned function", "decide",
             "function", _bound(f"{UNVERSIONED_ID}.message_type", ["WIN"], "return", spam_sms),
             [[[0.1, 0.1, 0.8]]], catalogue_name="versioned"),
        # escalation
        case("E1: an abstained decision escalates to a function step that answers", "escalation", "function",
             _bound(URG, ["The sofa is late"], {"escalate": [{"function": SENT}]}),
             [[LOW_URGENCY], [[0.0, 0.0, 0.05, 0.9, 0.05]]]),
        case("E2: when every step abstains the last attempt is returned", "escalation", "function",
             _bound(URG, ["The sofa is late"], {"escalate": [{"function": SENT}]}),
             [[LOW_URGENCY], [[0.6, 0.1, 0.1, 0.1, 0.1]]]),
        case("E3: an llm step answers after a function step abstains", "escalation", "function",
             _bound(URG, ["The sofa is late"], {"escalate": [{"function": SENT}, {"llm": "2"}]}),
             [[LOW_URGENCY], [[0.6, 0.1, 0.1, 0.1, 0.1]]]),
        case("E4: an llm answer that is not an option fails the escalation", "escalation", "function",
             _bound(URG, ["The sofa is late"], {"escalate": [{"llm": "9"}]}), [[LOW_URGENCY]]),
        case("E5: policy error raises LowConfidence carrying the decision", "escalation", "function",
             _bound(URG, ["The sofa is late"], "error"), [[LOW_URGENCY]]),
        case("E6: a human step on a noul function sees its fixed descriptions", "escalation", "function",
             _bound("spam.is_spam", ["Hello"], {"escalate": [{"human": "false"}]}, spam_sms), [[[0.47, 0.53]]]),
        case("E7: a human step on caller labels answers with the caller's key", "escalation", "function",
             _bound("triage.route", ["Where is my money"], {"escalate": [{"human": "refunds"}]}, CTX, TEAMS),
             [[[0.4, 0.35, 0.25]]]),
        case("E8: a function step of another interface is refused before deciding", "escalation", "function",
             _bound(URG, ["x"], {"escalate": [{"function": "spam.message_type"}]}), []),
        case("E9: an unknown function step is refused", "escalation", "function",
             _bound(URG, ["x"], {"escalate": [{"function": "jeff.support.triage.nope"}]}), []),
        case("E10: a function step whose adapter is not installed is refused", "escalation", "function",
             _bound("spam.is_spam", ["x"], {"escalate": [{"function": "jeff.safety.guard.attempt"}]}, spam_sms), [],
             installed=NO_GUARD),
        case("E11: a decision that does not abstain is returned as it is", "escalation", "function",
             _bound(URG, ["Fire"], {"escalate": [{"llm": "0"}]}), [[SURE_LEVEL_2]]),
        case("E12: many inputs: only the abstained one escalates", "escalation", "function",
             _bound(URG, ["Fire", "Hm"], {"escalate": [{"llm": "1"}]}), [[SURE_LEVEL_2, LOW_URGENCY]]),
        case("E13: a step failing with another error becomes EscalationFailed", "escalation", "function",
             _bound(URG, ["x"], {"escalate": [{"function": SENT}]}), [[LOW_URGENCY], [[0.25, 0.25, 0.25, 0.25]]]),
        # dispatch
        case("D1: dispatch walks two routers to the leaf", "dispatch", "dispatch",
             {"input": "It is on fire", "interface": "rate", "context": CTX},
             [[[0.9, 0.1]], [[1.0]], [SURE_LEVEL_2]], catalogue_name="routed"),
        case("D2: a router that abstains returns its own decision with the route", "dispatch", "dispatch",
             {"input": "It is on fire", "interface": "rate", "context": CTX}, [[[0.6, 0.4]]],
             catalogue_name="routed"),
        case("D3: a router that abstains under policy error raises LowConfidence", "dispatch", "dispatch",
             {"input": "It is on fire", "interface": "rate", "context": CTX, "policy": "error"}, [[[0.6, 0.4]]],
             catalogue_name="routed"),
        case("D4: context reaches only the fields each function has", "dispatch", "dispatch",
             {"input": "WIN NOW", "interface": "classify", "context": {"channel": "sms", "company": "A shop."}},
             [[[1.0]], [[0.1, 0.8, 0.1]]], catalogue_name="routed"),
        case("D5: labels go to a leaf that takes the caller's labels", "dispatch", "dispatch",
             {"input": "Where is my money", "interface": "route", "context": CTX, "labels": TEAMS},
             [[[1.0]], [[1.0]], [[0.1, 0.8, 0.1]]], catalogue_name="routed"),
        case("D6: an interface no router leads to", "dispatch", "dispatch",
             {"input": "x", "interface": "detect"}, [], catalogue_name="routed"),
        case("D7: no router installed", "dispatch", "dispatch",
             {"input": "x", "interface": "rate", "context": CTX}, []),
        case("D8: an abstained leaf escalates with the call dispatch made to it", "dispatch", "dispatch",
             {"input": "Hm", "interface": "rate", "context": CTX, "policy": {"escalate": [{"llm": "1"}]}},
             [[[0.9, 0.1]], [[1.0]], [LOW_URGENCY]], catalogue_name="routed"),
        case("D9: an unknown policy is refused before routing", "dispatch", "dispatch",
             {"input": "x", "interface": "rate", "policy": "maybe"}, [], catalogue_name="routed"),
        case("D10: a shorthand without a function is a dispatch", "dispatch", "shorthand",
             {"interface": "rate", "input": "It is on fire", "context": CTX},
             [[[0.9, 0.1]], [[1.0]], [SURE_LEVEL_2]], catalogue_name="routed"),
        # graph
        case("G1: a graph runs in stages and calls its code node with every result", "graph", "graph",
             {"graph": GRAPH, "input": "My wardrobe arrived broken", "params": PARAMS,
              "code": {"make_ticket": {"ticket": "T-1"}}},
             [[[0.9, 0.05, 0.05]], [[0.1, 0.8, 0.1]], [SURE_LEVEL_2]]),
        case("G2: a condition that fails skips the node and every node after it", "graph", "graph",
             {"graph": GRAPH, "input": "WIN NOW", "params": PARAMS, "code": {"make_ticket": {"ticket": "T-1"}}},
             [[[0.05, 0.9, 0.05]]]),
        case("G3: a graph needs every code function it uses", "graph", "graph",
             {"graph": GRAPH, "input": "x", "params": PARAMS}, []),
        case("G4: a code function no node uses is refused", "graph", "graph",
             {"graph": GRAPH, "input": "x", "params": PARAMS, "code": {"make_ticket": 1, "extra": 2}}, []),
        case("G5: a node policy escalates inside the graph", "graph", "graph",
             {"graph": GRAPH, "input": "My wardrobe arrived broken", "params": PARAMS,
              "code": {"make_ticket": {"ticket": "T-2"}}, "policies": {"urgency": {"escalate": [{"llm": "4"}]}}},
             [[[0.9, 0.05, 0.05]], [[0.1, 0.8, 0.1]], [LOW_URGENCY]]),
        case("G6: a cycle is refused", "graph", "graph", {"graph": cycle, "input": "x", "params": {}}, []),
        case("G7: is true holds for a level of 1, as Python compares", "graph", "graph",
             {"graph": truthy, "input": "x", "params": {"company": "A shop.", "channel": "email"},
              "code": {"flag": "raised"}}, [[level1]]),
        case("G8: every param must be given", "graph", "graph",
             {"graph": GRAPH, "input": "x", "params": {"company": "A shop.", "channel": "email"},
              "code": {"make_ticket": 1}}, []),
        case("G9: a node whose adapter is not installed is refused", "graph", "graph",
             {"graph": GRAPH, "input": "x", "params": PARAMS, "code": {"make_ticket": 1}}, [], installed=NO_SPAM),
    ]


def decisions_file() -> dict[str, Any]:
    return {"description": (
                "Calls through the Python client (decide, bound functions, interface shorthands, escalation, dispatch "
                "and graphs) over a scripted engine. 'predicts' is every predict the client made, in order: the "
                "adapter, the rows (as JSON and as base64 MessagePack) and the probabilities and timing the engine "
                "answered (a probability that is not finite written as the string 'NaN', 'Infinity' or '-Infinity', "
                "which JSON has no number for). 'questions' is what each llm or human step was shown. 'expect' is the result or the error. "
                "The timing.ms of a decision an llm or human step made is written '<elapsed>'."),
            "cases": [run_decision_case(spec) for spec in [*generated_cases(), *handwritten_cases()]]}


# ---- socket conversations

def _exactly(conn: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = conn.recv(n - len(data))
        if not chunk:
            raise RuntimeError(f"The client closed the connection after {len(data)} of {n} bytes")
        data += chunk
    return data


class ScriptedSocket:
    """An engine socket that follows a script, on its own thread: ("read",) reads one frame and keeps its exact bytes,
    ("write", bytes) sends bytes, ("close",) closes the connection. After the script it records anything more the
    client sends as "unexpected" (the recorder then stops), until the client closes."""

    def __init__(self, script: list[tuple[Any, ...]]) -> None:
        self.path = Path(tempfile.mkdtemp(prefix="jeffconf")) / "e.sock"
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(self.path))
        self.listener.listen(1)
        self.script = script
        self.steps: list[dict[str, Any]] = []
        self.failure: str | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def reads(self) -> int:
        return sum(1 for step in self.steps if "client" in step)

    def _run(self) -> None:
        conn, _ = self.listener.accept()
        try:
            for step in self.script:
                if step[0] == "read":
                    header = _exactly(conn, 4)
                    body = _exactly(conn, struct.unpack(">I", header)[0])
                    self.steps.append({"client": b64(header + body)})
                elif step[0] == "write":
                    conn.sendall(step[1])
                    self.steps.append({"engine": b64(step[1])})
                elif step[0] == "close":
                    self.steps.append({"close": True})
                    conn.shutdown(socket.SHUT_RDWR)
                    return
            while data := conn.recv(65536):
                self.steps.append({"unexpected": b64(data)})
        except (OSError, RuntimeError) as error:
            self.failure = f"{type(error).__name__}: {error}"
        finally:
            conn.close()
            self.listener.close()


def _ok(id_: int, result: dict[str, Any]) -> bytes:
    return encode({"id": id_, "ok": True, "result": result})


def _fail(id_: int, type_: str, message: str, details: dict[str, Any]) -> bytes:
    return encode({"id": id_, "ok": False, "error": {"type": type_, "message": message, "details": details}})


ADAPTERS = ["base", "jeff.safety.spam"]


def _installed(id_: int, version: int = 3) -> bytes:
    return _ok(id_, {"adapters": ADAPTERS, "adapters_version": version})


def _health(id_: int, base_format: dict[str, str] = BF16) -> bytes:
    return _ok(id_, {"status": "ready", "backend": "fake", "adapters": ADAPTERS, "adapters_version": 3,
                     "base": {"id": "jeff-base", "version": "v1.3", "checkpoint": "fake", "max_options": 252,
                              **base_format}})


def _predicted(id_: int, probabilities: list[list[float]], version: int = 3, tokens: bool = True) -> bytes:
    result: dict[str, Any] = {"probabilities": probabilities,
                              "timing": {"path": "fake", "model_ms": 1.5, "format": "bf16"}, "adapters_version": version}
    if tokens:
        result["input_tokens"] = [12] * len(probabilities)
    return _ok(id_, result)


def _connect(base_format: dict[str, str] = BF16) -> list[tuple[Any, ...]]:
    return [("read",), ("write", encode(hello(adapters_version=3))), ("read",), ("write", _installed(1)),
            ("read",), ("write", _health(2, base_format))]


ROW = {"state": {"channel": "sms", "message": "WIN NOW"}, "question": {"type": "noul", "instructions": "Spam?"}}
OTHER_ROW = {"state": {"channel": "email", "message": "Lunch at noon?"}, "question": {"type": "noul",
                                                                                     "instructions": "Spam?"}}
CONNECT = {"op": "connect"}


def _predict(rows: list[dict[str, Any]] = [ROW], **extra: Any) -> dict[str, Any]:
    return {"op": "predict", "adapter": "jeff.safety.spam", "rows": rows, **extra}


TRANSCRIPTS: list[dict[str, Any]] = [
    {"name": "connect, then a predict", "actions": [CONNECT, _predict()],
     "script": [*_connect(), ("read",), ("write", _predicted(3, [[0.25, 0.75]]))]},
    {"name": "a reply with a new adapters_version makes the client ask for the list",
     "actions": [CONNECT, _predict(), _predict()],
     "script": [*_connect(), ("read",), ("write", _predicted(3, [[0.25, 0.75]], version=4)), ("read",),
                ("write", _installed(4, version=4)), ("read",), ("write", _predicted(5, [[0.5, 0.5]], version=4))]},
    {"name": "a failure reply is raised as its error, without adapters_version", "actions": [CONNECT, _predict()],
     "script": [*_connect(), ("read",),
                ("write", _fail(3, "Busy", "The engine queue is full", {"retry_after_s": 2.5, "adapters_version": 3}))]},
    {"name": "replies may come in any order, matched by id",
     "actions": [CONNECT, _predict([ROW], concurrent=True), _predict([OTHER_ROW], concurrent=True)],
     "script": [*_connect(), ("read",), ("read",), ("write", _predicted(4, [[0.9, 0.1]])),
                ("write", _predicted(3, [[0.2, 0.8]]))]},
    {"name": "an engine speaking another protocol version is refused at hello", "actions": [CONNECT],
     "script": [("read",), ("write", encode({"op": "hello", "version": 1, "adapters_version": 3}))]},
    {"name": "a frame header over 64 MiB fails the waiting call", "actions": [CONNECT, _predict()],
     "script": [*_connect(), ("read",), ("write", struct.pack(">I", MAX_FRAME + 1))]},
    {"name": "a failure with id 0 fails every waiting call", "actions": [CONNECT, _predict()],
     "script": [*_connect(), ("read",),
                ("write", _fail(0, "InvalidRequest", "Frame could not be read", {"adapters_version": 3}))]},
    {"name": "the engine closing the connection fails the waiting call", "actions": [CONNECT, _predict()],
     "script": [*_connect(), ("read",), ("close",)]},
    {"name": "deadline_ms travels in the predict", "actions": [CONNECT, _predict(deadline_ms=250)],
     "script": [*_connect(), ("read",), ("write", _predicted(3, [[0.25, 0.75]]))]},
    {"name": "an unknown error type becomes EngineError", "actions": [CONNECT, _predict()],
     "script": [*_connect(), ("read",), ("write", _fail(3, "Weird", "Something odd", {"adapters_version": 3}))]},
    {"name": "an INT4 base is read from the health at connect", "actions": [CONNECT],
     "script": _connect(INT4)},
    {"name": "a message over 64 MiB is refused before it is sent",
     "actions": [CONNECT, {"op": "predict", "adapter": "jeff.safety.spam",
                           "rows_generated": {"state_length": MAX_FRAME, "question": ROW["question"]}}],
     "script": _connect()},
    {"name": "installed with refresh, then health", "actions": [CONNECT, {"op": "installed"}, {"op": "health"}],
     "script": [*_connect(), ("read",), ("write", _installed(3)), ("read",), ("write", _health(4))]},
    {"name": "a predict reply without input_tokens", "actions": [CONNECT, _predict()],
     "script": [*_connect(), ("read",), ("write", _predicted(3, [[0.25, 0.75]], tokens=False))]},
    {"name": "a reply without adapters_version is refused", "actions": [CONNECT, _predict()],
     "script": [*_connect(), ("read",),
                ("write", _ok(3, {"probabilities": [[0.25, 0.75]], "timing": {"path": "fake"}}))]},
]


def _rows_of(action: dict[str, Any]) -> list[dict[str, Any]]:
    if "rows" in action:
        return copy.deepcopy(action["rows"])
    generated = action["rows_generated"]
    return [{"state": "x" * generated["state_length"], "question": copy.deepcopy(generated["question"])}]


def _act(engine: SocketEngine, action: dict[str, Any]) -> dict[str, Any]:
    if action["op"] == "predict":
        result = engine.predict(action["adapter"], _rows_of(action), action.get("deadline_ms"))
        return {"probabilities": result.probabilities, "timing": result.timing, "input_tokens": result.input_tokens}
    if action["op"] == "installed":
        return {"installed": sorted(engine.installed(refresh=True))}
    if action["op"] == "health":
        return engine.health()
    raise SystemExit(f"Unknown socket action {action}")


def run_transcript(spec: dict[str, Any]) -> dict[str, Any]:
    server = ScriptedSocket(spec["script"])
    results: list[dict[str, Any] | None] = [None] * len(spec["actions"])
    engine: SocketEngine | None = None

    def failed(error: Exception) -> dict[str, Any]:
        return {"error": {"type": type(error).__name__, "message": str(error).replace(str(server.path), "{socket}")}}

    def run(index: int) -> None:
        assert engine is not None
        try:
            results[index] = {"result": _act(engine, spec["actions"][index])}
        except JeffError as error:
            results[index] = failed(error)

    try:
        engine = SocketEngine(server.path, timeout_s=5)
        results[0] = {"result": {"base_format": engine.base_format, "base": list(engine.base),
                                 "installed": sorted(engine.installed())}}
    except JeffError as error:
        results[0] = failed(error)
    index = 1
    while engine is not None and index < len(spec["actions"]):
        group = [index]
        while spec["actions"][group[-1]].get("concurrent") and group[-1] + 1 < len(spec["actions"]) \
                and spec["actions"][group[-1] + 1].get("concurrent"):
            group.append(group[-1] + 1)
        threads = []
        for member in group:
            before = server.reads()
            thread = threading.Thread(target=run, args=(member,))
            thread.start()
            threads.append(thread)
            if len(group) > 1:
                deadline = time.monotonic() + 5
                while server.reads() == before:
                    if time.monotonic() > deadline:
                        raise SystemExit(f"{spec['name']}: the engine never read action {member}'s frame")
                    time.sleep(0.001)
        for thread in threads:
            thread.join(10)
        index = group[-1] + 1
    if engine is not None:
        engine.close()
    server.thread.join(10)
    if server.thread.is_alive() or server.failure is not None:
        raise SystemExit(f"{spec['name']}: the scripted engine failed: {server.failure or 'it did not finish'}")
    if any("unexpected" in step for step in server.steps):
        raise SystemExit(f"{spec['name']}: the client sent more than the script reads: {server.steps}")
    return plain({"name": spec["name"], "actions": spec["actions"], "steps": server.steps, "results": results})


def socket_file() -> dict[str, Any]:
    return {"description": (
                "Engine socket conversations between the Python SocketEngine and a scripted engine. 'steps' is the "
                "conversation seen from the engine: 'client' is a frame the engine read (base64, header included), "
                "'engine' bytes the engine wrote, 'close' the engine closing the connection. 'actions' are what the "
                "client was asked to do, in order; consecutive actions marked 'concurrent' were started together, "
                "each after the engine had read the previous one's frame. 'rows_generated' stands for one row whose "
                "state is a string of that many 'x' characters. 'results' is each action's result, or its error "
                "with the socket path written {socket}."),
            "cases": [run_transcript(spec) for spec in TRANSCRIPTS]}


# ---- remote

BASE_URL = "http://jeff.test"
TOKEN = "secret-token"
REMOTE_KEYS = ("name", "catalogue", "server_catalogue", "server_installed", "token", "actions")


def server_answers(adapter: str, row: dict[str, Any]) -> list[float]:
    """The fake engine behind the recorded server: the first option wins, except that a score question is confident
    at level 2 unless its state mentions "unsure"."""
    question = row["question"]
    if question["type"] == "score":
        return [0.0, 0.5, 0.5, 0.0, 0.0] if "unsure" in json.dumps(row["state"]) else [0.0, 0.0, 0.9, 0.1, 0.0]
    count = len(question["criteria"]) if "criteria" in question else 2
    return [1.0] + [0.0] * (count - 1)


def _request_record(request: httpx.Request) -> dict[str, Any]:
    return {"method": request.method, "path": request.url.path,
            "authorization": request.headers.get("authorization"),
            "body": json.loads(request.content) if request.content else None}


def _response_record(response: httpx.Response) -> dict[str, Any]:
    return {"status": response.status_code,
            "headers": {key: response.headers[key] for key in ("content-type", "x-request-id") if key in response.headers},
            "body": response.text}


def _write_catalogue(folder: Path, name: str) -> Path:
    shutil.copytree(CATALOGUE, folder)
    for raw in variants().get(name, []):
        (folder / raw["id"]).mkdir(exist_ok=True)
        (folder / raw["id"] / "manifest.yaml").write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return folder


def _finish_remote(spec: dict[str, Any], http: httpx.Client, exchanges: list[dict[str, Any]]) -> dict[str, Any]:
    transport = RestTransport(BASE_URL, spec["token"], 10.0)
    transport.client.close()
    transport.client = http
    questions: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    try:
        client = RemoteJeff(transport, catalogue(spec["catalogue"]), transport.url)
        connect: dict[str, Any] = {"ok": True}
    except JeffError as error:
        client, connect = None, {"error": {**wire_error(error), "request_id": error.request_id}}
    if client is not None:
        for action in spec["actions"]:
            try:
                results.append({"result": normalised(plain(run_op(client, action["op"], action["args"], questions)))})
            except JeffError as error:
                results.append({"error": normalised({**wire_error(error), "request_id": error.request_id})})
    return plain({key: spec[key] for key in REMOTE_KEYS}
                 | {"connect": connect, "exchanges": exchanges, "questions": questions, "results": results})


#: Remote cases recorded against a server from before /v1/functions carried each function's aliases (the field is
#: optional, so clients still accept such a server, as the live one was when the field was added).
OLDER_SERVER_CASES = {"a server from before the functions' aliases is accepted"}


def _without_function_aliases(app: Any) -> Any:
    """`app` as a server from before the aliases field: its GET /v1/functions answers have no "aliases"."""
    async def older(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["path"] != "/v1/functions":
            await app(scope, receive, send)
            return
        start: dict[str, Any] = {}
        chunks: list[bytes] = []

        async def rewrite(message: Any) -> None:
            if message["type"] == "http.response.start":
                start.update(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return
            body = json.loads(b"".join(chunks))
            for function in body["functions"]:
                del function["aliases"]
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers = [(key, value) for key, value in start["headers"] if key != b"content-length"]
            await send({**start, "headers": [*headers, (b"content-length", str(len(data)).encode("ascii"))]})
            await send({"type": "http.response.body", "body": data})

        await app(scope, receive, rewrite)
    return older


def record_remote_case(spec: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        settings = ServerSettings(engine_sockets=(root / "engine.sock",), model_name="jeff-v1.3",
                                  catalogue=_write_catalogue(root / "catalogue", spec["server_catalogue"]))
        app = create_app(settings, engine=FakeEngine(server_answers, set(spec["server_installed"])))
        if spec["name"] in OLDER_SERVER_CASES:
            app = _without_function_aliases(app)
        exchanges: list[dict[str, Any]] = []
        sent: list[dict[str, Any]] = []

        def answered(response: httpx.Response) -> None:
            response.read()
            exchanges.append({"request": sent.pop(0), "response": _response_record(response)})

        with TestClient(app, base_url=BASE_URL) as http:
            http.event_hooks = {"request": [lambda request: sent.append(_request_record(request))],
                                "response": [answered]}
            return _finish_remote(spec, http, exchanges)


def replay_remote_case(recorded: dict[str, Any]) -> dict[str, Any]:
    answers = [exchange["response"] for exchange in recorded["exchanges"]]
    exchanges: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if not answers:
            raise SystemExit(f"{recorded['name']}: the client sent {request.method} {request.url.path} after every "
                             "recorded exchange")
        answer = answers.pop(0)
        exchanges.append({"request": _request_record(request), "response": answer})
        return httpx.Response(answer["status"], headers=answer["headers"], content=answer["body"].encode("utf-8"))

    http = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    return _finish_remote({key: recorded[key] for key in REMOTE_KEYS}, http, exchanges)


def _remote(name: str, actions: list[dict[str, Any]], *, client: str = "shipped", server: str = "shipped",
            token: str | None = TOKEN) -> dict[str, Any]:
    return {"name": name, "catalogue": client, "server_catalogue": server, "server_installed": adapter_ids(server),
            "token": token, "actions": actions}


REMOTE_CASES = [
    _remote("connect reads the served functions; decide sends the calls by full ID", [
        {"op": "decide", "args": {"calls": [
            {"function": "triage.route", "input": "Where is my money", "context": CTX, "labels": TEAMS},
            {"function": "spam.message_type", "input": "WIN NOW", "context": {"channel": "sms"}}]}}]),
    _remote("a bound function sends one request for many inputs", [
        {"op": "function", "args": {"ref": "triage.urgency", "context": CTX, "inputs": ["Fire", "Smoke"]}}]),
    _remote("policy error comes back as LowConfidence with the server's request id", [
        {"op": "function", "args": {"ref": URG, "context": CTX, "policy": "error", "inputs": ["unsure about it"]}}]),
    _remote("an llm step runs in the client on the decision the server returns", [
        {"op": "function", "args": {"ref": URG, "context": CTX, "policy": {"escalate": [{"llm": "2"}]},
                                    "inputs": ["unsure about it"]}}]),
    _remote("function steps are sent to the server, by full ID", [
        {"op": "function", "args": {"ref": URG, "context": CTX, "policy": {"escalate": [{"function": SENT}]},
                                    "inputs": ["unsure about it"]}}]),
    _remote("a graph without code nodes is one request", [
        {"op": "graph", "args": {"graph": GRAPH_NO_CODE, "input": "My wardrobe arrived broken", "params": PARAMS}}]),
    _remote("a graph with a code node runs stage by stage", [
        {"op": "graph", "args": {"graph": GRAPH, "input": "My wardrobe arrived broken", "params": PARAMS,
                                 "code": {"make_ticket": {"ticket": "T-1"}}}}]),
    _remote("dispatch is one request naming the router", [
        {"op": "dispatch", "args": {"input": "It is on fire", "interface": "rate", "context": CTX}}],
        client="routed", server="routed"),
    _remote("a server serving another version of an adapter is refused at connect", [], client="newer_spam"),
    _remote("a server from before the functions' aliases is accepted", [
        {"op": "decide", "args": {"calls": [
            {"function": "spam.is_spam", "input": "WIN NOW", "context": {"channel": "sms"}}]}}]),
    _remote("a call through the ID without its version part is sent by the versioned ID", [
        {"op": "decide", "args": {"calls": [
            {"function": f"{UNVERSIONED_ID}.is_spam", "input": "WIN NOW", "context": {"channel": "sms"}}]}}],
        client="versioned", server="versioned"),
    _remote("no token sends no Authorization header", [
        {"op": "decide", "args": {"calls": [{"function": "spam.is_spam", "input": "Hi", "context": {"channel": "sms"}}]}}],
        token=None),
]


def record_remote() -> dict[str, Any]:
    return {"description": (
                "Jeff.remote over HTTP against a real jeff-server over a fake engine. 'exchanges' is every request the "
                "Python client sent (method, path, Authorization header, JSON body) and the server's recorded answer "
                "(status, content-type and x-request-id headers, body). An SDK replays a case by serving each "
                "recorded answer in order, after checking that its own request is the recorded one. 'connect' is the "
                "outcome of connecting (GET /v1/functions); 'results' is each action's result, or its error with the "
                "request_id the client attached. The case \"a server from before the functions' aliases is accepted\" "
                "was recorded against a server whose GET /v1/functions answer has no 'aliases' field (optional), "
                "which a client must accept."),
            "base_url": BASE_URL, "cases": [record_remote_case(spec) for spec in REMOTE_CASES]}


def replay_remote(recorded: dict[str, Any]) -> dict[str, Any]:
    return {"description": recorded["description"], "base_url": BASE_URL,
            "cases": [replay_remote_case(case) for case in recorded["cases"]]}


# ---- writing

FILES: dict[str, Callable[[], dict[str, Any]]] = {
    "catalogues.json": catalogues_file,
    "manifests.json": manifests_file,
    "rows.json": rows_file,
    "decisions.json": decisions_file,
    "socket.json": socket_file,
    "remote.json": record_remote,
}


def write(name: str, data: dict[str, Any]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    text = json.dumps(plain(data), indent=1, ensure_ascii=False, allow_nan=False)
    (OUT / name).write_text(text + "\n", encoding="utf-8")


def main() -> None:
    for name, record in FILES.items():
        write(name, record())
        print(f"Wrote {(OUT / name).relative_to(ROOT)}")


if __name__ == "__main__":
    main()
