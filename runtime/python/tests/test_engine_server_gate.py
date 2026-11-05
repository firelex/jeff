"""An engine server on an INT4 base serves only adapters its gate passed (plan 2b-2 Task 7, Review Focus 2): at start,
and on every `serve` and `reload`, before anything changes, with an EngineError naming the adapter and the command
(jeff.gate.check_served). The base and its gate.json are synthetic (jeff.testing.fake_int4_base, fake_gate); the
engine is a FakeEngine."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from jeff.errors import EngineError
from jeff.gate import GatedBase, int4_identity
from jeff.testing import FakeEngine, fake_adapter_folder, fake_gate, fake_int4_base
from jeff_runtime.testing import Client, Running

FIXED = {"a.b.c": Path("/x"), "a.b.d": Path("/y")}
VERSIONS = {"a.b.c": "1.0.0", "a.b.d": "1.0.0"}


def gated(tmp_path: Path, adapters: dict[str, list[str]], versions: dict[str, str] = VERSIONS) -> GatedBase:
    folder = fake_int4_base(tmp_path / "int4")
    fake_gate(folder, adapters)
    identity = int4_identity(folder)
    assert identity is not None
    return GatedBase(identity, versions)


def engine(installed: set[str]) -> FakeEngine:
    return FakeEngine(lambda adapter, row: [0.4, 0.6], installed,
                      base_format={"format": "int4-g64", "format_id": "jeff-base@v1.3+int4-g64-gptq.0123abcd"})


def ask(client: Client, message: dict[str, Any]) -> dict[str, Any]:
    client.send(message)
    return client.receive()


def served_file(path: Path, adapters: list[str]) -> Path:
    path.write_text(json.dumps({"format": "jeff-engine-served", "version": 1, "adapters": adapters}))
    return path


def test_the_server_refuses_to_start_with_an_ungated_adapter_installed(tmp_path: Path) -> None:
    gate = gated(tmp_path, {"a.b.d@1.0.0": []})
    with pytest.raises(EngineError, match="a.b.c@1.0.0 is not in the gate .*--adapters a.b.c"):
        Running(engine({"a.b.c"}), fixed=FIXED, gate=gate)


def test_the_server_refuses_to_start_on_an_int4_base_without_a_gate(tmp_path: Path) -> None:
    folder = fake_int4_base(tmp_path / "int4")
    identity = int4_identity(folder)
    assert identity is not None
    with pytest.raises(EngineError, match="has no gate.json.*a.b.c@1.0.0.*jeff runtime gate --base"):
        Running(engine({"a.b.c"}), fixed=FIXED, gate=GatedBase(identity, VERSIONS))


def test_the_server_refuses_fixed_adapters_without_a_version(tmp_path: Path) -> None:
    gate = gated(tmp_path, {"a.b.c@1.0.0": []}, versions={"a.b.c": "1.0.0"})
    with pytest.raises(EngineError, match="no version was given for the fixed adapters a.b.d"):
        Running(engine({"a.b.c"}), fixed=FIXED, gate=gate)


def test_serve_refuses_an_ungated_adapter_and_changes_nothing(tmp_path: Path) -> None:
    served = served_file(tmp_path / "served.json", ["a.b.c"])
    fake = engine({"a.b.c"})
    server = Running(fake, served, fixed=FIXED, gate=gated(tmp_path, {"a.b.c@1.0.0": []}))
    try:
        reply = ask(server.client(), {"op": "serve", "id": 1, "adapters": ["a.b.c", "a.b.d"]})
    finally:
        server.close()
    assert reply["ok"] is False and reply["error"]["type"] == "EngineError"
    message = reply["error"]["message"]
    assert "a.b.d@1.0.0 is not in the gate" in message and "jeff runtime gate --base" in message
    assert "--adapters a.b.c a.b.d" in message
    assert fake.installed() == {"a.b.c"}
    assert json.loads(served.read_text())["adapters"] == ["a.b.c"]


def test_a_failed_gate_serves_no_adapter_at_start_or_on_serve(tmp_path: Path) -> None:
    """gate.json is read again on every change: a gate that failed since the engine started refuses the next serve."""
    gate = gated(tmp_path, {"a.b.c@1.0.0": [], "a.b.d@1.0.0": ["agreement 980/1000 (98.00%), below 99%"]})
    with pytest.raises(EngineError, match="failed its gate .*a.b.d@1.0.0: agreement 980/1000.*refusing a.b.c@1.0.0"):
        Running(engine({"a.b.c"}), fixed=FIXED, gate=gate)
    fake_gate(gate.identity.folder, {"a.b.c@1.0.0": [], "a.b.d@1.0.0": []})
    server = Running(engine({"a.b.c"}), fixed=FIXED, gate=gate)
    try:
        fake_gate(gate.identity.folder, {"a.b.c@1.0.0": [], "a.b.d@1.0.0": ["agreement 980/1000 (98.00%)"]})
        reply = ask(server.client(), {"op": "serve", "id": 1, "adapters": ["a.b.c"]})
    finally:
        server.close()
    assert reply["ok"] is False and "failed its gate" in reply["error"]["message"]
    assert "refusing a.b.c@1.0.0" in reply["error"]["message"]


def test_serve_serves_gated_adapters(tmp_path: Path) -> None:
    fake = engine({"a.b.c"})
    server = Running(fake, fixed=FIXED, gate=gated(tmp_path, {"a.b.c@1.0.0": [], "a.b.d@1.0.0": []}))
    try:
        reply = ask(server.client(), {"op": "serve", "id": 1, "adapters": ["a.b.d"]})
    finally:
        server.close()
    assert reply["result"] == {"adapters": ["a.b.d"], "adapters_version": 1}


def test_reload_refuses_an_ungated_adapter_in_the_served_file_and_changes_nothing(tmp_path: Path) -> None:
    served = served_file(tmp_path / "served.json", ["a.b.c"])
    fake = engine({"a.b.c"})
    server = Running(fake, served, fixed=FIXED, gate=gated(tmp_path, {"a.b.c@1.0.0": []}))
    try:
        served_file(served, ["a.b.d"])
        reply = ask(server.client(), {"op": "reload", "id": 1})
    finally:
        server.close()
    assert reply["ok"] is False and "a.b.d@1.0.0 is not in the gate" in reply["error"]["message"]
    assert fake.installed() == {"a.b.c"}


def test_reload_refuses_a_found_adapter_whose_version_changed(tmp_path: Path) -> None:
    """An adapter folder rewritten with a new version is checked at the version the reload's scan finds."""
    roots = tmp_path / "adapters"
    folder = fake_adapter_folder(roots / "x", "jeff.support.x")
    version = yaml.safe_load((folder / "manifest.yaml").read_text())["version"]
    served = served_file(tmp_path / "served.json", ["jeff.support.x"])
    fake = engine({"jeff.support.x"})
    server = Running(fake, served, fixed={}, adapter_roots=[roots],
                     gate=gated(tmp_path, {f"jeff.support.x@{version}": []}, versions={}))
    try:
        client = server.client()
        manifest = yaml.safe_load((folder / "manifest.yaml").read_text())
        manifest["version"] = "9.9.9"
        (folder / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
        reply = ask(client, {"op": "reload", "id": 1})
        installed = ask(client, {"op": "installed", "id": 2})
    finally:
        server.close()
    assert reply["ok"] is False
    assert (f"jeff.support.x@9.9.9 is not in the gate of jeff-base@v1.3+int4-g64-gptq.0123abcd, which gated "
            f"jeff.support.x@{version} (another version)") in reply["error"]["message"]
    assert installed["result"] == {"adapters": ["jeff.support.x"], "adapters_version": 0}


def test_a_bfloat16_base_has_no_gate_check() -> None:
    fake = FakeEngine(lambda adapter, row: [0.4, 0.6], {"a.b.c"})
    server = Running(fake, fixed=FIXED)
    try:
        reply = ask(server.client(), {"op": "serve", "id": 1, "adapters": ["a.b.d"]})
    finally:
        server.close()
    assert reply["result"]["adapters"] == ["a.b.d"]
