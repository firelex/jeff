"""The hub's recorded examples (apps/hub/content/examples/<family>.yaml, and the hidden adapters' examples in
apps/hub/content/owner-only/examples/) and the pre-publish check.

Each example names a function, its context, its input and (for a function whose labels the caller gives) its labels.
The docs show the call in Python and curl together with the answer recorded from the live API, so a reader sees a
real answer. Modes, one per run:

  --record       calls the live server for every example (except `offline: true` ones, whose adapter the server does
                 not serve) and writes its `recorded` block: value, confidence, the options with the highest
                 probabilities (at most three, highest first), the base version of the adapter that answered, and
                 the date. Nothing else in the files changes.
  --record-new   the same as --record, for only the examples that have no `recorded` block yet: the others keep their
                 recorded answers and are not called (a new example can be recorded while an older one's adapter is
                 no longer served).
  --check        calls the live server again and fails listing every example whose value differs from the recorded
                 one, or whose confidence differs by more than 0.02, or that has no recorded answer. Then runs the
                 home page's remote Python example (apps/hub/content/home-example.py), every Python SDK docs snippet
                 that calls a server (apps/hub/content/snippets/sdk/*.py with no `# offline` first line and not
                 *.local.py), and, through
                 apps/hub/scripts/run-node-samples.ts, every Node sample the site shows that calls a server: the Node
                 snippets, home-example.mjs and the Node code of every recorded example (checked against its recorded
                 answer); and, through apps/hub/scripts/sdk-samples.ts, the same for each of Rust, C# and Java that
                 apps/hub/content/sdk-languages.yaml shows (home-example.<ext>, every recorded example's code and
                 every docs snippet in that language that is not local, built against that SDK's source). Every
                 failure is listed.
  --check-local  starts a Jeff engine on this machine serving the guard and the adapter of every worked example the
                 site shows "On your machine" (apps/hub/scripts/local-examples.ts; `jeff runtime serve`, as the pages
                 show it) and runs the local samples against it: home-example-local.py and every such example's
                 Python code and the Python docs snippets *.local.py (Jeff.local, which downloads the public
                 weights); through run-node-samples.ts,
                 home-example-local.mjs, the Node snippets marked `// local` and every such example's Node code; and
                 the same for each shown Rust, C# or Java language (sdk-samples.ts local). A worked example's printed
                 value must be its recorded value (its confidence is not compared: the recorded one is the live
                 server's). It needs every one of those adapters' weights public at the catalogue's revision (see
                 --check-repos), and the flags the pages give for a Mac or a CPU.
  --validate     reads the files only (no server): fails listing every example that the SDK would refuse to bind
                 against its adapter's manifest (an unknown function, a missing or unknown context field, labels the
                 function does not take or lacks, an input of the wrong form), and every example whose `offline` mark
                 does not match whether the live server serves its adapter (results/v14-manifests).
  --check-repos  fails listing every open adapter in catalogue/ whose weights repository does not exist publicly on
                 Hugging Face at its revision (asked anonymously; reads only), and every mismatch between that and
                 apps/hub/content/published-weights.yaml, the list behind the pages' "published at launch" marks.

The server is JEFF_HUB_TEST_URL. The API key is JEFF_API_KEY when set, else the macOS Keychain item
(service jeff-hub, account jeff-v14-api); it is only ever held in memory, never printed or written. An example is
called through the SDK (jeff.Jeff.remote(...).function(...)), or, with `via: http` (a proprietary function, which
the SDK does not bind), as a plain POST /v1/decide.
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import yaml as yaml11
from ruamel.yaml import YAML, YAMLError
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.scalarstring import SingleQuotedScalarString

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = ROOT / "apps/hub/content/examples"
# The hidden adapters' examples (every adapter the API serves that is not open): never on the public site, only on the
# owner's page; validated, recorded and checked with the others.
OWNER_EXAMPLES_DIR = ROOT / "apps/hub/content/owner-only/examples"
CATALOGUE = ROOT / "catalogue"
SERVED_DIR = ROOT / "results/v14-manifests"
PUBLISHED_WEIGHTS = ROOT / "apps/hub/content/published-weights.yaml"
SDK_LANGUAGES_FILE = ROOT / "apps/hub/content/sdk-languages.yaml"
# The languages apps/hub/scripts/sdk-samples.ts builds and runs (Python and Node have their own runners above).
SAMPLE_LANGUAGES = ("rust", "csharp", "java")
HUB = ROOT / "apps/hub"
SNIPPETS_DIR = HUB / "content/snippets/sdk"
ENGINE_START_TIMEOUT_S = 900.0
CONFIDENCE_TOLERANCE = 0.02
TIMEOUT_S = 30.0
KEYCHAIN_SERVICE = "jeff-hub"
KEYCHAIN_ACCOUNT = "jeff-v14-api"

FIELDS = {"id", "name", "function", "context", "input", "labels", "via", "offline", "routes_to_example", "recorded"}
REQUIRED = ("id", "function", "input")
RECORDED_FIELDS = {"value", "confidence", "top_options", "server", "at"}
# How many of the answer's options, by probability, a recorded block keeps (the pages show them under the answer).
TOP_OPTIONS = 3
CODE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
SERVER_VERSION = re.compile(r"^v\d+\.\d+$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ExampleError(Exception):
    """An example file, or a manifest the script reads, that is not as this script expects."""


class RefusedCall(Exception):
    """The live server refused an example's call (for example NotInstalled: it does not serve that adapter). --check
    lists it with the other failures and goes on; --record stops on it."""


def _yaml() -> YAML:
    # Round-trip mode keeps the files' comments, order and quoting, so --record changes only the recorded blocks.
    y = YAML(typ="rt")
    y.width = 4096
    y.indent(mapping=2, sequence=2, offset=0)
    # Keep quoted scalars quoted ('yes', 'no', ...), so a file stays unambiguous for YAML 1.1 readers after --record.
    y.preserve_quotes = True
    # Write a null as `null`, as the files spell it (ruamel's default writes nothing after the colon).
    y.representer.add_representer(type(None), lambda r, _: r.represent_scalar("tag:yaml.org,2002:null", "null"))
    return y


def unambiguous(value: Any) -> Any:
    """A recorded string that a YAML 1.1 reader would take for something else (no, yes, on, off, ...), quoted."""
    if isinstance(value, str) and yaml11.resolver.Resolver().resolve(
            yaml11.ScalarNode, value, (True, False)) != "tag:yaml.org,2002:str":
        return SingleQuotedScalarString(value)
    return value


def check_example(example: Any, where: str) -> None:
    if not isinstance(example, dict):
        raise ExampleError(f"{where}: an example must be a map, not {type(example).__name__}")
    unknown = sorted(set(example) - FIELDS)
    if unknown:
        raise ExampleError(f"{where}: unknown fields {', '.join(unknown)} (allowed: {', '.join(sorted(FIELDS))})")
    missing = [f for f in REQUIRED if f not in example]
    if missing:
        raise ExampleError(f"{where}: missing {', '.join(missing)}")
    if example["input"] is None:
        raise ExampleError(f"{where}: input must not be null")
    for field in ("id", "function"):
        if not isinstance(example[field], str) or not example[field]:
            raise ExampleError(f"{where}: {field} must be a non-empty string")
    # name: a short phrase saying what the case is, unique among the adapter's examples (the hub's playground menu).
    if "name" in example and (not isinstance(example["name"], str) or not example["name"].strip()):
        raise ExampleError(f"{where}: name must be a non-empty string")
    if example["function"].count(".") < 3:
        raise ExampleError(f"{where}: function {example['function']} is not a full function id "
                           f"(adapter id, a dot, the function's key, e.g. jeff.safety.guard.attempt)")
    context = example.get("context")
    if context is not None:
        if not isinstance(context, dict):
            raise ExampleError(f"{where}: context must be a map of field code name to value")
        bad = [str(k) for k in context if not isinstance(k, str) or not CODE_NAME.match(k)]
        if bad:
            raise ExampleError(f"{where}: context keys must be code names (lower case, digits, underscores, as "
                               f"GET /v1/functions lists them): {', '.join(bad)}")
    if "routes_to_example" in example and (not isinstance(example["routes_to_example"], str)
                                           or not example["routes_to_example"]):
        raise ExampleError(f"{where}: routes_to_example must be the id of the specialist example the router picks")
    if "via" in example and example["via"] != "http":
        raise ExampleError(f"{where}: via must be http (or absent, for a call through the SDK)")
    if "offline" in example and example["offline"] is not True:
        raise ExampleError(f"{where}: offline must be true (or absent)")
    if example.get("offline") is True:
        if "via" in example:
            raise ExampleError(f"{where}: an offline example runs locally with Jeff.local, so it cannot be via http")
        if "recorded" in example:
            raise ExampleError(f"{where}: an offline example is never called on the server, so it has no recorded")
    if "recorded" in example:
        recorded = example["recorded"]
        if not isinstance(recorded, dict) or set(recorded) != RECORDED_FIELDS:
            raise ExampleError(f"{where}: recorded must have exactly {', '.join(sorted(RECORDED_FIELDS))}")
        check_recorded(recorded, where)


def check_recorded(recorded: dict[str, Any], where: str) -> None:
    """The same rules as apps/hub/app/lib/examples.server.ts applies to a recorded block."""
    value, confidence, top, server, at = (recorded[k] for k in ("value", "confidence", "top_options", "server", "at"))
    if not isinstance(value, (str, bool, int)):
        raise ExampleError(f"{where}: recorded.value must be a string, true/false or a whole number, not {value!r}")
    if confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                                   or not 0 <= confidence <= 1):
        raise ExampleError(f"{where}: recorded.confidence must be a number from 0 to 1, or null, not {confidence!r}")
    if not isinstance(top, list) or not 1 <= len(top) <= TOP_OPTIONS:
        raise ExampleError(f"{where}: recorded.top_options must be a list of 1 to {TOP_OPTIONS} options")
    for option in top:
        if (not isinstance(option, dict) or set(option) != {"option", "probability"} or not isinstance(option["option"], str)
                or isinstance(option["probability"], bool) or not isinstance(option["probability"], (int, float))
                or not 0 <= option["probability"] <= 1):
            raise ExampleError(f"{where}: recorded.top_options must hold maps of option (a string) and probability "
                               f"(0 to 1), not {option!r}")
    probabilities = [option["probability"] for option in top]
    if probabilities != sorted(probabilities, reverse=True):
        raise ExampleError(f"{where}: recorded.top_options must be in order, highest probability first")
    if not isinstance(server, str) or not SERVER_VERSION.match(server):
        raise ExampleError(f"{where}: recorded.server must be a base version such as v1.4, not {server!r}")
    # Unquoted in YAML, a date reads as a date object here (and as a string in the hub's loader): both are fine.
    if not (type(at) is date or (isinstance(at, str) and ISO_DATE.match(at))):
        raise ExampleError(f"{where}: recorded.at must be a date written YYYY-MM-DD, not {at!r}")


def read_examples(*folders: Path) -> list[tuple[Path, list[dict[str, Any]]]]:
    """Every examples file in `folders` with its examples, checked; an id used twice (in any files of any of the
    folders) is an error, and so is a folder with no example file."""
    out: list[tuple[Path, list[dict[str, Any]]]] = []
    seen: dict[str, Path] = {}
    names: dict[tuple[str, str], str] = {}
    files: list[Path] = []
    for folder in folders:
        found = sorted(folder.glob("*.yaml"))
        if not found:
            raise ExampleError(f"No example files (*.yaml) in {folder}")
        files.extend(found)
    for path in files:
        try:
            items = _yaml().load(path.read_text())
        except YAMLError as error:
            raise ExampleError(f"{path}: not valid YAML: {error}") from error
        if not isinstance(items, list):
            raise ExampleError(f"{path}: must be a list of examples")
        for i, example in enumerate(items):
            check_example(example, f"{path.name}[{i}]")
            if example["id"] in seen:
                raise ExampleError(f"Example id {example['id']} is in both {seen[example['id']].name} and {path.name}")
            seen[example["id"]] = path
            if "name" in example:
                adapter = example["function"].rsplit(".", 1)[0]
                if (adapter, example["name"]) in names:
                    raise ExampleError(f"Example {example['id']} in {path.name} has the name {example['name']!r}, as "
                                       f"{names[(adapter, example['name'])]} of the same adapter {adapter} has")
                names[(adapter, example["name"])] = example["id"]
        out.append((path, items))
    return out


def plain(value: Any) -> Any:
    """A YAML value as plain JSON types (ruamel's round-trip maps and lists are subclasses with extra state)."""
    return json.loads(json.dumps(value))


def request_body(example: dict[str, Any]) -> dict[str, Any]:
    """The POST /v1/decide body for one example: one call. curl_for shows exactly this body, and the via-http path
    sends it."""
    call: dict[str, Any] = {"function": example["function"], "input": plain(example["input"])}
    if example.get("context"):
        call["context"] = plain(example["context"])
    if example.get("labels") is not None:
        call["labels"] = plain(example["labels"])
    return {"calls": [call]}


def curl_for(example: dict[str, Any], origin: str) -> str:
    return "\n".join([
        f"curl -s {origin}/v1/decide \\",
        '  -H "Authorization: Bearer $JEFF_API_KEY" \\',
        '  -H "Content-Type: application/json" \\',
        f"  -d {shlex.quote(json.dumps(request_body(example), ensure_ascii=False))}",
    ])


def compare(recorded: dict[str, Any], answer: dict[str, Any]) -> list[str]:
    """What differs between a recorded answer and a new one: the value (or its type) at all, the confidence by more
    than CONFIDENCE_TOLERANCE (or one of them being null when the other is not). The difference is rounded to six
    places first, so a drift of exactly the tolerance passes despite floating-point noise."""
    differences = []
    # The type too: True and 1 are equal in Python, but a function that answered true now answering 1 has changed.
    if type(recorded["value"]) is not type(answer["value"]) or recorded["value"] != answer["value"]:
        differences.append(f"value: recorded {recorded['value']!r}, now {answer['value']!r}")
    was, now = recorded["confidence"], answer["confidence"]
    if (was is None) != (now is None) or (was is not None and round(abs(was - now), 6) > CONFIDENCE_TOLERANCE):
        differences.append(f"confidence: recorded {was}, now {now} (tolerance {CONFIDENCE_TOLERANCE})")
    return differences


def keychain_key() -> str | None:
    """The API key from the macOS Keychain item, or None when there is no such item."""
    result = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT,
                             "-w"], capture_output=True, text=True, check=False)
    if result.returncode == 44:  # security's "The specified item could not be found in the keychain."
        return None
    if result.returncode != 0:
        raise SystemExit(f"Reading the Keychain item (service {KEYCHAIN_SERVICE}, account {KEYCHAIN_ACCOUNT}) "
                         f"failed with exit code {result.returncode}: {result.stderr.strip()}")
    return result.stdout.strip()


def api_key(keychain: Callable[[], str | None] = keychain_key) -> str:
    key = os.environ.get("JEFF_API_KEY")
    if key:
        return key
    key = keychain()
    if not key:
        raise SystemExit(f"No API key: set JEFF_API_KEY, or store it in the macOS Keychain (service "
                         f"{KEYCHAIN_SERVICE}, account {KEYCHAIN_ACCOUNT})")
    return key


def server_url() -> str:
    url = os.environ.get("JEFF_HUB_TEST_URL")
    if not url:
        raise SystemExit("Set JEFF_HUB_TEST_URL to the Jeff server the examples are recorded from")
    return url.rstrip("/")


def adapter_folder(name: str, folder: Path) -> Path | None:
    """The folder in `folder` of the adapter whose ID or alias is `name` (an example may name an adapter by its
    unversioned ID, jeff.routing.router-open for jeff.routing.router-open-v14), or None when no adapter there has that name."""
    if (folder / name / "manifest.yaml").is_file():
        return folder / name
    for path in sorted(folder.glob("*/manifest.yaml")):
        if name in YAML(typ="safe").load(path.read_text())["aliases"]:
            return path.parent
    return None


def manifest_path(function_id: str, catalogue: Path = CATALOGUE, served: Path = SERVED_DIR) -> Path:
    """The manifest file of the adapter a function belongs to (named by its ID or an alias): the open one in
    catalogue/, else the served one."""
    adapter_name = function_id.rsplit(".", 1)[0]
    for folder in (catalogue, served):
        found = adapter_folder(adapter_name, folder)
        if found is not None:
            return found / "manifest.yaml"
    raise ExampleError(f"{function_id}: no manifest for adapter {adapter_name} in {catalogue} or {served}")


def adapter_manifest(function_id: str) -> dict[str, Any]:
    manifest: dict[str, Any] = YAML(typ="safe").load(manifest_path(function_id).read_text())
    return manifest


def validate(files: list[tuple[Path, list[dict[str, Any]]]], catalogue: Path = CATALOGUE,
             served: Path = SERVED_DIR) -> list[str]:
    """Every problem a dry run finds, one line each: the example bound to its function exactly as the SDK binds it
    (jeff.binding.bind) and its request row built (build_row), with no server and no model; and its `offline` mark
    checked against whether the live server serves its adapter (a folder in `served`)."""
    from jeff.binding import bind, build_row
    from jeff.errors import JeffError
    from jeff.manifest import load_manifest

    failures = []
    by_id = {example["id"]: example for _, items in files for example in items}
    for path, items in files:
        failures.extend(f"{path.name}: {problem}" for problem in ambiguous_scalars(path))
        for example in items:
            where = f"{path.name}: {example['id']}"
            adapter_id, key = example["function"].rsplit(".", 1)
            try:
                manifest = load_manifest(manifest_path(example["function"], catalogue, served))
            except ExampleError as error:
                failures.append(f"{where}: {error}")
                continue
            if key not in manifest.functions:
                failures.append(f"{where}: {adapter_id} has no function {key!r}; its functions are "
                                f"{', '.join(manifest.functions)}")
                continue
            is_open = adapter_folder(adapter_id, catalogue) is not None
            if not is_open and example.get("via") != "http":
                failures.append(f"{where}: {adapter_id} is proprietary (not in {catalogue.name}/), so the SDK does not "
                                f"bind it; add via: http")
            is_served = adapter_folder(adapter_id, served) is not None
            if example.get("offline") is True and is_served:
                failures.append(f"{where}: marked offline, but the live server serves {adapter_id}")
            if example.get("offline") is not True and not is_served:
                failures.append(f"{where}: the live server does not serve {adapter_id}; mark the example offline: true")
            body = request_body(example)["calls"][0]
            try:
                binding = bind(manifest, manifest.functions[key], body.get("context"), body.get("labels"),
                               calibration=None)
                build_row(binding, body["input"])
            except JeffError as error:
                failures.append(f"{where}: {type(error).__name__}: {error}")
            if "routes_to_example" in example:
                failures.extend(f"{where}: {problem}"
                                for problem in routed_request_problems(example, by_id, catalogue, served))
    return failures


def ambiguous_scalars(path: Path) -> list[str]:
    """Values that a YAML 1.1 reader (PyYAML, many other tools) reads differently from this script's YAML 1.2 reader:
    unquoted yes, no, on, off, y, n and the like. Quote them. Recorded blocks are left out (their dates read as dates in
    both, but as different date types)."""
    def without_recorded(items: Any) -> Any:
        return [{k: v for k, v in item.items() if k != "recorded"} for item in items]

    as_11 = without_recorded(yaml11.safe_load(path.read_text()))
    as_12 = without_recorded(plain(YAML(typ="safe").load(path.read_text())))
    return [f"{item.get('id')}: a value reads differently in YAML 1.1 (quote yes, no, on, off and the like)"
            for item, other in zip(as_11, as_12, strict=True) if item != other]


def routed_request_problems(router: dict[str, Any], by_id: dict[str, dict[str, Any]], catalogue: Path,
                            served: Path) -> list[str]:
    """A router example's request must be exactly the request its specialist example makes: the specialist's
    instructions, its fixed label keys in the manifest's order, and as state the specialist's state (its context fields
    under their keys, then its input under the input key) as JSON text, serialised as the router's training data was
    (json.dumps with its default separators, non-ASCII kept)."""
    from jeff.binding import bind, request_state
    from jeff.errors import JeffError
    from jeff.manifest import load_manifest

    target_id = router["routes_to_example"]
    if target_id not in by_id:
        return [f"routes_to_example {target_id} is not an example"]
    target = by_id[target_id]
    request = plain(router["input"])
    if not isinstance(request, dict) or set(request) != {"instructions", "options", "state"}:
        return ["a router's input must be an object with exactly instructions, options and state"]
    try:
        manifest = load_manifest(manifest_path(target["function"], catalogue, served))
    except ExampleError as error:
        return [str(error)]
    key = target["function"].rsplit(".", 1)[1]
    if key not in manifest.functions:
        return [f"routes_to_example {target_id} names no function of {manifest.id}"]
    function = manifest.functions[key]
    problems = []
    if request["instructions"] != function.instructions:
        problems.append(f"instructions differ from {target['function']}'s instructions in its manifest")
    if function.type != "choice" or function.labels.source != "fixed":
        problems.append(f"{target['function']} has no fixed labels to list as options")
    elif request["options"] != list(function.labels.fixed):
        problems.append(f"options {request['options']} are not {target['function']}'s labels in order "
                        f"{list(function.labels.fixed)}")
    body = request_body(target)["calls"][0]
    try:
        binding = bind(manifest, function, body.get("context"), body.get("labels"), calibration=None)
    except JeffError as error:
        return [*problems, f"routes_to_example {target_id} does not bind: {error}"]
    expected = request_state(function.id, function.input, binding.context, body["input"])
    if not isinstance(request["state"], str):
        problems.append("state must be JSON text (a string)")
    elif json.loads(request["state"]) != expected:
        problems.append(f"state is not {target_id}'s context and input")
    elif request["state"] != json.dumps(expected, ensure_ascii=False):
        problems.append("state holds the right values but is not serialised as json.dumps(..., ensure_ascii=False) "
                        "writes it (key order or separators differ)")
    return problems


def base_version(function_id: str, served_version: str) -> str:
    """The base version of the adapter that answered: its manifest's, once the version the server reported for
    the adapter is the manifest's own."""
    manifest = adapter_manifest(function_id)
    if manifest["version"] != served_version:
        raise ExampleError(f"{function_id}: the server answered with adapter version {served_version}, but its "
                           f"manifest here is version {manifest['version']}")
    version: str = manifest["base"]["version"]
    return version


class Caller:
    """Calls examples on the live server, through the SDK or as a plain POST /v1/decide."""

    def __init__(self, url: str, key: str) -> None:
        self.url = url
        self.key = key
        self._sdk: Any = None

    def _client(self) -> Any:
        if self._sdk is None:
            import jeff
            self._sdk = jeff.Jeff.remote(self.url, token=self.key, timeout_s=TIMEOUT_S)
        return self._sdk

    def decide(self, example: dict[str, Any]) -> dict[str, Any]:
        """The answer's value, confidence and the adapter version the server reported."""
        if example.get("via") == "http":
            response = httpx.post(f"{self.url}/v1/decide", json=request_body(example), timeout=TIMEOUT_S,
                                  headers={"Authorization": f"Bearer {self.key}"})
            if response.status_code != 200:
                raise RefusedCall(f"{example['id']}: POST /v1/decide answered {response.status_code}: "
                                  f"{response.text[:500]}")
            decision = response.json()["decisions"][0]
            return {"value": decision["value"], "confidence": decision["confidence"], "version": decision["version"],
                    "probabilities": decision["probabilities"]}
        from jeff.errors import JeffError
        body = request_body(example)["calls"][0]
        try:
            bound = self._client().function(example["function"], context=body.get("context"),
                                            labels=body.get("labels"))
            result = bound(body["input"])
        except JeffError as error:
            raise RefusedCall(f"{example['id']}: the SDK call failed: {type(error).__name__}: {error}") from error
        return {"value": result.value, "confidence": result.confidence, "version": result.version,
                "probabilities": dict(result.probabilities)}

    def close(self) -> None:
        if self._sdk is not None:
            self._sdk.close()


def record(files: list[tuple[Path, list[dict[str, Any]]]], caller: Caller, only_new: bool = False) -> None:
    """Calls every example (with `only_new`, every example with no recorded block yet) first and only then writes the
    files, so a failure part-way (a refused call, an adapter at another version) leaves every file as it was."""
    today = date.today().isoformat()
    blocks: list[tuple[dict[str, Any], CommentedMap]] = []
    for _, items in files:
        for example in items:
            if example.get("offline") is True or (only_new and "recorded" in example):
                continue
            answer = caller.decide(example)
            confidence = answer["confidence"]
            blocks.append((example, CommentedMap([
                ("value", unambiguous(answer["value"])),
                ("confidence", None if confidence is None else round(confidence, 3)),
                ("top_options", top_options(example["id"], answer["probabilities"])),
                ("server", base_version(example["function"], answer["version"])),
                ("at", today),
            ])))
            print(f"{example['id']}: {answer['value']!r} ({confidence})")
    for example, block in blocks:
        example["recorded"] = block
    for path, items in files:
        with path.open("w") as out:
            _yaml().dump(items, out)


def top_options(example_id: str, probabilities: dict[str, float]) -> list[CommentedMap]:
    """The options with the highest probabilities, highest first (at most TOP_OPTIONS), each rounded to 3 places."""
    if not probabilities:
        raise ExampleError(f"{example_id}: the server's answer has no option probabilities")
    ranked = sorted(probabilities.items(), key=lambda item: -item[1])[:TOP_OPTIONS]
    return [CommentedMap([("option", unambiguous(option)), ("probability", round(p, 3))]) for option, p in ranked]


def recorded_answer(recorded: dict[str, Any]) -> dict[str, Any]:
    """A recorded block's value and confidence as plain types: a quoted value reads back as a str subclass, which
    compare would count as a change of type."""
    return {"value": plain(recorded["value"]), "confidence": recorded["confidence"]}


def check(files: list[tuple[Path, list[dict[str, Any]]]], caller: Caller) -> list[str]:
    failures = []
    for path, items in files:
        for example in items:
            if example.get("offline") is True:
                continue
            where = f"{path.name}: {example['id']}"
            if "recorded" not in example:
                failures.append(f"{where}: not recorded (run --record)")
                continue
            try:
                answer = caller.decide(example)
            except RefusedCall as refused:
                failures.append(f"{where}: refused: {refused}")
                continue
            failures.extend(f"{where}: {d}" for d in compare(recorded_answer(example["recorded"]), answer))
    return failures


def reachable(url: str) -> None:
    """Stops naming the server when nothing answers at `url` (for this setup, usually the SSH tunnel being down)."""
    try:
        httpx.get(f"{url}/health", timeout=10.0)
    except httpx.TransportError as error:
        raise SystemExit(f"Cannot reach the Jeff server at {url}: {type(error).__name__}: {error}. Nothing was checked; "
                         f"if the server is reached through an SSH tunnel, the tunnel is down.") from error


def run_python_sample(path: Path, substitute: dict[str, str], env: dict[str, str]) -> list[str]:
    """Runs one Python sample (a copy, with `substitute` applied) with `uv run`; one line naming it when it fails."""
    import tempfile
    code = path.read_text()
    for old, new in substitute.items():
        code = code.replace(old, new)
    with tempfile.TemporaryDirectory() as work:
        copy = Path(work) / path.name
        copy.write_text(code)
        result = subprocess.run(["uv", "run", "--project", str(ROOT), "python", str(copy)], capture_output=True,
                                text=True, env=env, check=False, timeout=900)
    if result.returncode != 0:
        tail = " | ".join(result.stderr.strip().splitlines()[-6:])
        return [f"{path.relative_to(ROOT)}: exited with {result.returncode}: {tail}"]
    print(f"ok   {path.relative_to(ROOT)}: {result.stdout.strip()}")
    return []


def python_snippets(local: bool) -> list[Path]:
    """The Python SDK docs snippets that need a server (local False: no `# offline` first line, not *.local.py) or an
    engine on this machine (local True: *.local.py)."""
    snippets = sorted(SNIPPETS_DIR.glob("*.py"))
    if local:
        return [path for path in snippets if path.name.endswith(".local.py")]
    return [path for path in snippets if not path.name.endswith(".local.py")
            and not path.read_text().startswith("# offline\n")]


def run_python_snippets(snippets: list[Path], substitute: dict[str, str], env: dict[str, str]) -> list[str]:
    """Runs each snippet with `uv run` in a copy of the snippets folder (a snippet may read a file beside it, such as
    graphs-message-check.yaml), with `substitute` applied; one line naming each that fails."""
    import shutil
    import tempfile
    if not snippets:
        raise ExampleError(f"No Python snippets to run in {SNIPPETS_DIR}")
    failures = []
    with tempfile.TemporaryDirectory() as work:
        shutil.copytree(SNIPPETS_DIR, work, dirs_exist_ok=True)
        for path in snippets:
            code = path.read_text()
            for old, new in substitute.items():
                code = code.replace(old, new)
            copy = Path(work) / path.name
            copy.write_text(code)
            result = subprocess.run(["uv", "run", "--project", str(ROOT), "python", copy.name], cwd=work,
                                    capture_output=True, text=True, env=env, check=False, timeout=900)
            if result.returncode != 0:
                tail = " | ".join(result.stderr.strip().splitlines()[-6:])
                failures.append(f"{path.relative_to(ROOT)}: exited with {result.returncode}: {tail}")
            else:
                print(f"ok   {path.relative_to(ROOT)}")
    return failures


def run_node_samples(args: list[str], env: dict[str, str]) -> list[str]:
    """apps/hub/scripts/run-node-samples.ts in one mode; its failure lines, or one line when it could not run."""
    result = subprocess.run(["pnpm", "exec", "tsx", "scripts/run-node-samples.ts", *args], cwd=HUB, capture_output=True,
                            text=True, env=env, check=False, timeout=1800)
    print(result.stdout, end="")
    if result.returncode == 0:
        return []
    lines = result.stderr.strip().splitlines()
    return lines[1:] if lines and "Node samples failed" in lines[0] else [f"run-node-samples.ts {' '.join(args)}: "
                                                                          + " | ".join(lines[-8:])]


def shown_sample_languages(path: Path = SDK_LANGUAGES_FILE) -> list[str]:
    """The Rust, C# and Java languages the site shows (apps/hub/content/sdk-languages.yaml), in tab order."""
    data = yaml11.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ExampleError(f"{path}: must be a map of language to settings")
    shown = []
    for language in SAMPLE_LANGUAGES:
        entry = data.get(language)
        if not isinstance(entry, dict) or not isinstance(entry.get("shown"), bool):
            raise ExampleError(f"{path}: {language}.shown must be true or false")
        if entry["shown"]:
            shown.append(language)
    return shown


def run_sdk_samples(args: list[str], env: dict[str, str]) -> list[str]:
    """apps/hub/scripts/sdk-samples.ts in one mode; its failure lines, or one line when it could not run."""
    result = subprocess.run(["pnpm", "exec", "tsx", "scripts/sdk-samples.ts", *args], cwd=HUB, capture_output=True,
                            text=True, env=env, check=False, timeout=3600)
    print(result.stdout, end="")
    if result.returncode == 0:
        return []
    lines = result.stderr.strip().splitlines()
    return lines[1:] if lines and "SDK samples failed" in lines[0] else [f"sdk-samples.ts {' '.join(args)}: "
                                                                        + " | ".join(lines[-8:])]


def check_samples(url: str, key: str) -> list[str]:
    """The home page's remote Python example, every Python docs snippet and Node sample that calls a server, and the
    samples of each shown Rust, C# or Java language."""
    env = {**os.environ, "JEFF_HUB_TEST_URL": url, "JEFF_API_KEY": key}
    languages = shown_sample_languages()
    return [*run_python_sample(HUB / "content/home-example.py", {"{{origin}}": url}, env),
            *run_python_snippets(python_snippets(local=False), {"{{origin}}": url}, env),
            *run_node_samples(["remote"], env),
            *(run_sdk_samples(["remote", *languages], env) if languages else [])]


def local_examples() -> dict[str, Any]:
    """apps/hub/scripts/local-examples.ts's list: the adapters the engine must serve, and each worked example's local
    Python code with its recorded answer."""
    result = subprocess.run(["pnpm", "exec", "tsx", "scripts/local-examples.ts"], cwd=HUB, capture_output=True,
                            text=True, check=False, timeout=600)
    if result.returncode != 0:
        raise ExampleError(f"scripts/local-examples.ts failed: {' | '.join(result.stderr.strip().splitlines()[-8:])}")
    return json.loads(result.stdout)


def value_problem(example_id: str, recorded: dict[str, Any], stdout: str) -> list[str]:
    """A local Python example printed `decision.value decision.confidence`: one line naming it when the value is not
    the recorded one (Python prints True/False for a boolean)."""
    line = stdout.strip().splitlines()[-1] if stdout.strip() else ""
    value = line.rsplit(" ", 1)[0] if " " in line else line
    expected = recorded["value"]
    want = ("True" if expected else "False") if isinstance(expected, bool) else str(expected)
    return [] if value == want else [f"example {example_id} (Python, local): value: recorded {want}, now {value}"]


def run_python_code(name: str, code: str, env: dict[str, str]) -> tuple[list[str], str]:
    """Runs Python `code` with `uv run`; (one line naming it when it fails, its stdout)."""
    import tempfile
    with tempfile.TemporaryDirectory() as work:
        copy = Path(work) / "sample.py"
        copy.write_text(code)
        result = subprocess.run(["uv", "run", "--project", str(ROOT), "python", str(copy)], capture_output=True,
                                text=True, env=env, check=False, timeout=900)
    if result.returncode != 0:
        tail = " | ".join(result.stderr.strip().splitlines()[-6:])
        return [f"{name}: exited with {result.returncode}: {tail}"], result.stdout
    return [], result.stdout


def check_local() -> list[str]:
    """Starts an engine serving the guard and every local example's adapter, as the pages start it, and runs the
    local samples."""
    import tempfile
    import time
    listed = local_examples()
    adapters = sorted({"jeff.safety.guard", *listed["adapters"]})
    with tempfile.TemporaryDirectory() as work:
        socket_path = str(Path(work) / "jeff.sock")
        log_path = Path(work) / "engine.log"
        with log_path.open("w") as log:
            engine = subprocess.Popen(["uv", "run", "--project", str(ROOT), "jeff", "runtime", "serve", "--socket",
                                       socket_path, *[a for adapter in adapters for a in ("--adapter", adapter)],
                                       "--batch-window-ms", "0.1",
                                       "--max-batch-rows", "64", "--queue-limit-rows", "4096", "--prepare-workers", "2"],
                                      stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + ENGINE_START_TIMEOUT_S
            while not Path(socket_path).exists():
                if engine.poll() is not None:
                    tail = " | ".join(log_path.read_text().strip().splitlines()[-8:])
                    return [f"jeff runtime serve exited with {engine.returncode} before listening: {tail}"]
                if time.monotonic() > deadline:
                    return [f"jeff runtime serve did not listen on its socket within {ENGINE_START_TIMEOUT_S:.0f} s"]
                time.sleep(0.5)
            env = dict(os.environ)
            languages = shown_sample_languages()
            python: list[str] = []
            for example in listed["examples"]:
                name = f"example {example['id']} (Python, local)"
                failed, stdout = run_python_code(name, example["python"], env)
                problems = failed or value_problem(example["id"], example["recorded"], stdout)
                if not problems:
                    print(f"ok   {name}")
                python.extend(problems)
            return [*run_python_sample(HUB / "content/home-example-local.py", {}, env),
                    *python,
                    *run_python_snippets(python_snippets(local=True), {}, env),
                    *run_node_samples(["local", socket_path], env),
                    *(run_sdk_samples(["local", socket_path, *languages], env) if languages else [])]
        finally:
            engine.terminate()
            engine.wait(timeout=60)


def open_repos(catalogue: Path) -> list[tuple[str, str]]:
    """(adapter id, weights written <repository>@<revision>) for every open adapter in `catalogue`."""
    out = []
    for path in sorted(catalogue.glob("*/manifest.yaml")):
        manifest = YAML(typ="safe").load(path.read_text())
        if manifest["licence"]["kind"] != "open":
            continue
        weights = manifest["weights"].get("open")
        if weights is None or not weights.get("repo") or not weights.get("revision"):
            raise ExampleError(f"{manifest['id']}: an open adapter with no weights.open.repo and revision ({path})")
        out.append((manifest["id"], f"{weights['repo']}@{weights['revision']}"))
    if not out:
        raise ExampleError(f"No open adapters in {catalogue}")
    return out


def read_published(path: Path = PUBLISHED_WEIGHTS) -> set[str]:
    """The weights the hub shows as public today (apps/hub/content/published-weights.yaml), as <repository>@<revision>."""
    items = YAML(typ="safe").load(path.read_text())
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        raise ExampleError(f"{path}: must be a list of <repository>@<revision> strings")
    return set(items)


def repo_problems(repos: list[tuple[str, str]], published: set[str], exists: Callable[[str], bool]) -> list[str]:
    """One line for each open adapter whose weights are not public at their revision, and for each mismatch between
    the hub's list of published weights and what is public: listed but not public (the page would hide the "published
    at launch" mark wrongly), or public but not listed (the page would show the mark wrongly)."""
    problems = []
    public = set()
    for adapter_id, weights in repos:
        if exists(weights):
            public.add(weights)
        else:
            problems.append(f"{adapter_id}: {weights} is not public on Hugging Face")
    problems.extend(f"{PUBLISHED_WEIGHTS.name}: lists {w}, which is not public" for w in sorted(published - public))
    problems.extend(f"{PUBLISHED_WEIGHTS.name}: does not list {w}, which is public" for w in sorted(public - published))
    return problems


def public_weights_exist(weights: str) -> bool:
    """Whether <repository>@<revision> is public on Hugging Face: the repository exists and has that tag or branch."""
    from huggingface_hub import HfApi
    repo, revision = weights.rsplit("@", 1)
    # token=False: ask anonymously, so a private repository (or one only this machine's token can see) counts as
    # missing -- the catalogue promises public weights.
    api = HfApi(token=False)
    if not api.repo_exists(repo, repo_type="model"):
        return False
    refs = api.list_repo_refs(repo, repo_type="model")
    return revision in {ref.name for ref in [*refs.tags, *refs.branches]}


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--record", action="store_true")
    mode.add_argument("--record-new", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--validate", action="store_true")
    mode.add_argument("--check-repos", action="store_true")
    mode.add_argument("--check-local", action="store_true")
    args = parser.parse_args(argv)

    if args.check_repos:
        problems = repo_problems(open_repos(CATALOGUE), read_published(), public_weights_exist)
        if problems:
            raise SystemExit(f"{len(problems)} problems with the open adapters' public weights:\n" + "\n".join(problems))
        print("Every open adapter's weights are public on Hugging Face at their revision, and the hub lists them all.")
        return

    if args.check_local:
        failures = check_local()
        if failures:
            raise SystemExit(f"{len(failures)} local samples failed:\n" + "\n".join(failures))
        print("Every local sample ran against the engine.")
        return

    files = read_examples(EXAMPLES_DIR, OWNER_EXAMPLES_DIR)
    if args.validate:
        problems = validate(files)
        if problems:
            raise SystemExit(f"{len(problems)} examples would be refused:\n" + "\n".join(problems))
        print(f"All {sum(len(items) for _, items in files)} examples bind to their functions.")
        return
    url, key = server_url(), api_key()
    reachable(url)
    caller = Caller(url, key)
    try:
        if args.record or args.record_new:
            try:
                record(files, caller, only_new=args.record_new)
            except RefusedCall as refused:
                raise SystemExit(f"Nothing was recorded: {refused}") from refused
            return
        failures = check(files, caller)
    finally:
        caller.close()
    failures.extend(check_samples(url, key))
    if failures:
        raise SystemExit(f"{len(failures)} examples or samples failed (an example no longer giving its recorded answer, "
                         f"or a sample that did not run):\n" + "\n".join(failures))
    print("Every example still gives its recorded answer, and every sample that calls a server ran.")


if __name__ == "__main__":
    main(sys.argv[1:])
