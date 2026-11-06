"""jeff.cli: argument parsing and error paths for `jeff runtime serve` that do not need a real engine."""

import argparse
import json
import subprocess
import sys

import pytest

from jeff.cli import cmd_runtime_serve, main
from jeff.errors import RuntimeMissing

SERVE_ARGS = ["runtime", "serve", "--socket", "/tmp/does-not-matter.sock", "--adapter", "triage",
              "--batch-window-ms", "20", "--max-batch-rows", "64", "--queue-limit-rows", "4096", "--prepare-workers", "2"]
# For tests of the command's own checks, not of a backend: left on auto, a CUDA machine resolves to the fast backend,
# which also requires --max-adapters and --prefix-pool-mb and refuses before the check under test.
BACKEND_FREE = ["--backend", "torch"]


def test_runtime_serve_needs_the_scheduler_flags() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["runtime", "serve", "--socket", "/tmp/does-not-matter.sock", "--adapter", "triage"])
    assert excinfo.value.code == 2


def test_runtime_serve_needs_a_source_for_the_starting_adapters(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["runtime", "serve", "--socket", "/tmp/does-not-matter.sock", "--backend", "torch",  # see BACKEND_FREE
                 "--batch-window-ms", "20", "--max-batch-rows", "64", "--queue-limit-rows", "4096", "--prepare-workers", "2"])
    err = capsys.readouterr().err
    assert code == 1
    assert "--adapter" in err and "--served-file" in err and "--base-version" in err


@pytest.mark.parametrize("bad", ["jeff-base", "@v1.3", "jeff-base@"])
def test_runtime_serve_rejects_a_malformed_base_version(bad: str) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["runtime", "serve", "--socket", "/tmp/does-not-matter.sock", "--base-version", bad,
              "--batch-window-ms", "20", "--max-batch-rows", "64", "--queue-limit-rows", "4096", "--prepare-workers", "2"])
    assert excinfo.value.code == 2


def test_runtime_serve_refuses_a_missing_adapters_dir(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main([*SERVE_ARGS, *BACKEND_FREE, "--adapters-dir", str(tmp_path / "nope")])
    assert code == 1 and str(tmp_path / "nope") in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["ID=", "=folder", "noequals"])
def test_runtime_serve_rejects_a_malformed_path(bad: str) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["runtime", "serve", "--socket", "/tmp/does-not-matter.sock", "--adapter", "triage", "--path", bad,
              "--batch-window-ms", "20", "--max-batch-rows", "64", "--queue-limit-rows", "4096", "--prepare-workers", "2"])
    assert excinfo.value.code == 2


def test_runtime_serve_without_jeff_runtime_raises_runtime_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "jeff_runtime", None)
    args = argparse.Namespace(socket="/tmp/does-not-matter.sock", adapter=["triage"], path=[], catalogue=None,
                               served_file=None, adapters_dir=[], base_version=None, batch_window_ms=20.0,
                               max_batch_rows=64, queue_limit_rows=4096, backend="auto", max_tokens=None)
    with pytest.raises(RuntimeMissing, match=r"jeff-runtime\[mac\]"):
        cmd_runtime_serve(args)


def test_importing_cli_does_not_load_jeff_runtime_or_torch() -> None:
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; import jeff.cli; "
         "assert 'jeff_runtime' not in sys.modules, 'jeff_runtime loaded eagerly'; "
         "assert 'torch' not in sys.modules, 'torch loaded eagerly'; "
         "print('ok')"],
        capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "ok"


@pytest.mark.parametrize(("flag", "value"), [("--batch-window-ms", "0"), ("--max-batch-rows", "0"),
                                             ("--queue-limit-rows", "32")])
def test_runtime_serve_refuses_bad_scheduler_settings_before_loading_anything(
        flag: str, value: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import jeff.cli

    def must_not_run(*args: object) -> None:
        raise AssertionError("resolve_base ran before the settings were checked")

    monkeypatch.setattr(jeff.cli, "resolve_base", must_not_run)
    args = list(SERVE_ARGS)
    args[args.index(flag) + 1] = value
    assert main(args) == 1
    assert flag.removeprefix("--").replace("-", "_").replace("batch_window_ms", "window_ms") in capsys.readouterr().err


def test_runtime_health_prints_a_ready_engines_health(capsys: pytest.CaptureFixture[str]) -> None:
    from jeff.testing import FakeEngine
    from jeff_runtime.testing import Running

    running = Running(FakeEngine(lambda adapter, row: [1.0, 0.0], {"a.b.c"}))
    try:
        assert main(["runtime", "health", "--socket", str(running.path), "--timeout-s", "5"]) == 0
    finally:
        running.close()
    health = json.loads(capsys.readouterr().out)
    assert health["status"] == "ready"
    assert health["base"]["id"] == "jeff-base"


def test_runtime_health_names_a_socket_nothing_listens_on(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    socket_path = tmp_path / "engine-0.sock"
    assert main(["runtime", "health", "--socket", str(socket_path), "--timeout-s", "5"]) == 1
    assert str(socket_path) in capsys.readouterr().err


def test_runtime_health_fails_for_a_broken_engine(monkeypatch: pytest.MonkeyPatch,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    import jeff.socket_engine

    class Broken:
        def __init__(self, path: str, *, timeout_s: float) -> None:
            pass

        def health(self) -> dict[str, str]:
            return {"status": "broken", "cause": "the GPU thread died"}

        def close(self) -> None:
            pass

    monkeypatch.setattr(jeff.socket_engine, "SocketEngine", Broken)
    assert main(["runtime", "health", "--socket", "/tmp/e.sock", "--timeout-s", "5"]) == 1
    err = capsys.readouterr().err
    assert "/tmp/e.sock" in err and "broken" in err and "the GPU thread died" in err


def test_runtime_health_needs_a_timeout() -> None:
    with pytest.raises(SystemExit):
        main(["runtime", "health", "--socket", "/tmp/e.sock"])


def _no_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    import jeff.cli

    def must_not_run(*args: object) -> None:
        raise AssertionError("resolve_base ran before --max-adapters was checked")

    monkeypatch.setattr(jeff.cli, "resolve_base", must_not_run)


def test_runtime_serve_on_the_fast_backend_needs_max_adapters(monkeypatch: pytest.MonkeyPatch,
                                                              capsys: pytest.CaptureFixture[str]) -> None:
    """Checked from the flags alone, before looking for a GPU: on the Mac too, the error names --max-adapters."""
    _no_loading(monkeypatch)
    assert main([*SERVE_ARGS, "--backend", "fast"]) == 1
    err = capsys.readouterr().err
    assert "--max-adapters" in err and "fast backend" in err


@pytest.mark.parametrize("backend", ["mlx", "torch"])
def test_runtime_serve_refuses_max_adapters_where_it_has_no_effect(
        backend: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _no_loading(monkeypatch)
    assert main([*SERVE_ARGS, "--backend", backend, "--max-adapters", "8"]) == 1
    err = capsys.readouterr().err
    assert "--max-adapters" in err and f"runs on {backend}" in err


@pytest.mark.parametrize(("chosen", "flags", "expected"), [
    ("fast", [], "needs --max-adapters"),
    ("mlx", ["--max-adapters", "8"], "only applies to the fast backend"),
])
def test_runtime_serve_checks_max_adapters_against_the_backend_auto_picks(
        chosen: str, flags: list[str], expected: str, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    import jeff_runtime

    _no_loading(monkeypatch)
    monkeypatch.setattr(jeff_runtime, "select_backend", lambda requested: chosen)
    assert main([*SERVE_ARGS, *flags]) == 1
    assert expected in capsys.readouterr().err


@pytest.mark.parametrize("value", ["0", "-2"])
def test_runtime_serve_refuses_a_max_adapters_below_one(value: str, monkeypatch: pytest.MonkeyPatch,
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    _no_loading(monkeypatch)
    assert main([*SERVE_ARGS, "--backend", "fast", "--max-adapters", value]) == 1
    assert f"--max-adapters {value}" in capsys.readouterr().err


def test_runtime_serve_refuses_more_starting_adapters_than_max_adapters(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import jeff_runtime

    _no_loading(monkeypatch)
    monkeypatch.setattr(jeff_runtime, "select_backend", lambda requested: "fast")
    assert main([*SERVE_ARGS, "--adapter", "spam", "--max-adapters", "1", "--prefix-pool-mb", "8192"]) == 1
    err = capsys.readouterr().err
    assert "2 adapters were asked for" in err and "at most 1 (--max-adapters 1)" in err


def test_runtime_serve_counts_an_adapter_named_twice_once_against_max_adapters(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """--adapter triage and --adapter jeff.support.triage (its ID) are one adapter: it fits --max-adapters 1, so the
    command goes on to load (and stops at the test's guard there)."""
    import jeff_runtime

    _no_loading(monkeypatch)
    monkeypatch.setattr(jeff_runtime, "select_backend", lambda requested: "fast")
    with pytest.raises(AssertionError, match="resolve_base ran"):
        main([*SERVE_ARGS, "--adapter", "jeff.support.triage", "--max-adapters", "1", "--prefix-pool-mb", "8192"])


@pytest.mark.parametrize("requested", ["fast", "auto"])
def test_runtime_serve_on_the_fast_backend_needs_prefix_pool_mb(
        requested: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import jeff_runtime

    _no_loading(monkeypatch)
    monkeypatch.setattr(jeff_runtime, "select_backend", lambda requested: "fast")
    assert main([*SERVE_ARGS, "--backend", requested, "--max-adapters", "8"]) == 1
    err = capsys.readouterr().err
    assert "needs --prefix-pool-mb" in err and "2048 holds 66 prefixes of 1,000 tokens" in err


@pytest.mark.parametrize("requested", ["fast", "auto"])
def test_runtime_serve_refuses_a_prefix_pool_too_small_for_max_batch_rows(
        requested: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Design 2b-1 Risk 5: the fast backend's prefix pool must hold --max-batch-rows prefixes of the longest prefix
    at once; checked from the flags alone, before anything loads."""
    import jeff_runtime

    _no_loading(monkeypatch)
    monkeypatch.setattr(jeff_runtime, "select_backend", lambda requested: "fast")
    assert main([*SERVE_ARGS, "--backend", requested, "--max-adapters", "8", "--prefix-pool-mb", "4096"]) == 1
    err = capsys.readouterr().err
    assert "--prefix-pool-mb 4096" in err and "--max-batch-rows 64" in err and "at least 7345" in err


def test_runtime_serve_takes_a_prefix_pool_that_holds_64_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    import jeff_runtime

    _no_loading(monkeypatch)
    monkeypatch.setattr(jeff_runtime, "select_backend", lambda requested: "fast")
    with pytest.raises(AssertionError, match="resolve_base ran"):  # every setting passed; loading would start
        main([*SERVE_ARGS, "--max-adapters", "8", "--prefix-pool-mb", "7345"])


@pytest.mark.parametrize("value", ["115", "0", "-1"])
def test_runtime_serve_refuses_a_prefix_pool_below_the_smallest(value: str, monkeypatch: pytest.MonkeyPatch,
                                                                capsys: pytest.CaptureFixture[str]) -> None:
    _no_loading(monkeypatch)
    assert main([*SERVE_ARGS, "--backend", "fast", "--max-adapters", "8", "--prefix-pool-mb", value]) == 1
    assert f"--prefix-pool-mb {value}" in capsys.readouterr().err


@pytest.mark.parametrize("backend", ["mlx", "torch"])
def test_runtime_serve_refuses_prefix_pool_mb_where_it_has_no_effect(
        backend: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _no_loading(monkeypatch)
    assert main([*SERVE_ARGS, "--backend", backend, "--prefix-pool-mb", "8192"]) == 1
    err = capsys.readouterr().err
    assert "--prefix-pool-mb" in err and f"runs on {backend}" in err


def test_runtime_serve_needs_prepare_workers() -> None:
    with pytest.raises(SystemExit):
        main([arg for arg in SERVE_ARGS if arg not in ("--prepare-workers", "2")])


def test_runtime_serve_refuses_bad_prepare_workers_before_loading_anything(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import jeff.cli

    def must_not_run(*args: object) -> None:
        raise AssertionError("resolve_base ran before --prepare-workers was checked")

    monkeypatch.setattr(jeff.cli, "resolve_base", must_not_run)
    args = list(SERVE_ARGS)
    args[args.index("--prepare-workers") + 1] = "0"
    assert main(args) == 1
    assert "--prepare-workers 0" in capsys.readouterr().err


# ---- --base: an INT4 base, served only for adapters its gate passed (plan 2b-2 Task 7)

# These tests use triage, on the v1.4 base, so each fake INT4 base is made from jeff-base v1.4.
INT4_SERVE_ARGS = ["runtime", "serve", "--socket", "/tmp/does-not-matter.sock", "--adapter", "triage",
                   "--batch-window-ms", "20", "--max-batch-rows", "64", "--queue-limit-rows", "4096",
                   "--prepare-workers", "2"]


def _guard_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails the test with "loading would start" once the command gets past its checks to fetching weights."""
    import jeff.cli

    def must_not_run(*args: object) -> None:
        raise AssertionError("loading would start")

    monkeypatch.setattr(jeff.cli, "resolve_base", must_not_run)
    monkeypatch.setattr(jeff.cli, "adapter_folder", must_not_run)


def test_runtime_serve_refuses_an_int4_base_without_a_gate_before_loading(
        tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from jeff.testing import fake_int4_base

    _guard_loading(monkeypatch)
    base = fake_int4_base(tmp_path / "int4", source=("jeff-base", "v1.4"))
    assert main([*INT4_SERVE_ARGS, *BACKEND_FREE, "--base", str(base)]) == 1
    err = capsys.readouterr().err
    assert "has no gate.json" in err and "jeff.support.triage@1.4.0" in err
    assert f"jeff runtime gate --base {base} --reference" in err and "--adapters jeff.support.triage" in err


def test_runtime_serve_refuses_an_adapter_the_gate_has_at_another_version(
        tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from jeff.testing import fake_gate, fake_int4_base

    _guard_loading(monkeypatch)
    base = fake_int4_base(tmp_path / "int4", source=("jeff-base", "v1.4"))
    fake_gate(base, {"jeff.support.triage@1.3.0": []})
    assert main([*INT4_SERVE_ARGS, *BACKEND_FREE, "--base", str(base)]) == 1
    assert "jeff.support.triage@1.4.0 is not in the gate" in capsys.readouterr().err


def test_runtime_serve_loads_an_int4_base_whose_gate_passed(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jeff.testing import fake_gate, fake_int4_base

    _guard_loading(monkeypatch)
    base = fake_int4_base(tmp_path / "int4", source=("jeff-base", "v1.4"))
    fake_gate(base, {"jeff.support.triage@1.4.0": []})
    with pytest.raises(AssertionError, match="loading would start"):
        main([*INT4_SERVE_ARGS, *BACKEND_FREE, "--base", str(base)])


def test_runtime_serve_base_must_be_an_int4_base_of_the_adapters_base(
        tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from jeff.testing import fake_int4_base

    _guard_loading(monkeypatch)
    other = fake_int4_base(tmp_path / "other", source=("jeff-base", "v1.2"))
    assert main([*INT4_SERVE_ARGS, *BACKEND_FREE, "--base", str(other)]) == 1
    assert "is an INT4 base of jeff-base@v1.2, but the adapters are trained on jeff-base@v1.4" in \
        capsys.readouterr().err
    bf16 = tmp_path / "bf16"
    bf16.mkdir()
    (bf16 / "decision_config.json").write_text('{"format_version": 1}')
    assert main([*INT4_SERVE_ARGS, *BACKEND_FREE, "--base", str(bf16)]) == 1
    assert "is a bfloat16 base" in capsys.readouterr().err
    assert main([*INT4_SERVE_ARGS, *BACKEND_FREE, "--base", str(tmp_path / "missing")]) == 1
    assert "is not a base folder" in capsys.readouterr().err


def test_runtime_gate_needs_its_settings() -> None:
    for missing in (["--reference", "r", "--adapters", "triage"], ["--base", "b", "--adapters", "triage"],
                    ["--base", "b", "--reference", "r"]):
        with pytest.raises(SystemExit):
            main(["runtime", "gate", *missing])


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_runtime_serve_refuses_an_exit_when_unused_that_is_not_above_0(
        value: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import jeff.cli

    def must_not_run(*args: object) -> None:
        raise AssertionError("resolve_base ran before --exit-when-unused-s was checked")

    monkeypatch.setattr(jeff.cli, "resolve_base", must_not_run)
    assert main([*SERVE_ARGS, "--exit-when-unused-s", value]) == 1
    assert "--exit-when-unused-s" in capsys.readouterr().err
