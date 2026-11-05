"""deploy/run.sh and deploy/stop.sh, run against fakes: a `tmux` first on PATH and a fake `.venv/bin` (`jeff`,
`jeff-server`) in a temporary copy of the repository, each appending its arguments to one log file, so a test reads
back every command the scripts issued, in order. The scripts find the repository and its .venv from their own path
(deploy/..), which is why each test runs a copy of deploy/ under a temporary folder rather than the real one.

The fake tmux keeps its sessions in a state file: a test seeds it with sessions that already exist, `new-session`
adds one (unless the test names it in FAKE_TMUX_DIES, an engine that exits at once), `kill-session` removes one and
`has-session` answers from it; it also records, for every call, the names of any JEFF_* variable or Hugging Face token
in its environment (none may reach it). The fake `jeff runtime health` succeeds for the sockets listed in
FAKE_HEALTHY, and the fake `curl` (the API's /health) while FAKE_API_UP is 1.

The scripts run under /bin/bash, which on the Mac is bash 3.2, so these tests also hold the scripts to it."""

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[3] / "deploy"
SCRIPTS = ("run.sh", "stop.sh", "instance.sh", "session.sh")

FAKE_TMUX = """\
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(["tmux", *args]) + "\\n")
# The names of the variables this tmux was started with that it must never hold (deploy/instance.sh, tmux_clean).
with open(os.environ["FAKE_TMUX_ENV"], "a") as seen:
    seen.write(json.dumps(sorted(name for name in os.environ
                                 if name.startswith("JEFF_") or name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"))) + "\\n")
state = Path(os.environ["FAKE_TMUX_STATE"])
sessions = state.read_text().split()
def target():
    return args[args.index("-t") + 1].removeprefix("=")
if args[0] == "has-session":
    sys.exit(0 if target() in sessions else 1)
if args[0] == "new-session":
    name = args[args.index("-s") + 1]
    if name not in os.environ.get("FAKE_TMUX_DIES", "").split(","):
        sessions.append(name)
elif args[0] == "kill-session":
    sessions.remove(target())
state.write_text("\\n".join(sessions))
"""

FAKE_JEFF = """\
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps([os.path.basename(sys.argv[0]), *args]) + "\\n")
if args[:2] == ["runtime", "health"]:
    path = args[args.index("--socket") + 1]
    if path not in os.environ.get("FAKE_HEALTHY", "").split(":"):
        print(f"jeff: No engine is listening at {path}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"status": "ready"}))
"""

# curl, as run.sh asks the API's /health: answers while FAKE_API_UP is 1, otherwise fails as curl does when nothing
# listens (exit 7).
FAKE_CURL = """\
import json, os, sys
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(["curl", *sys.argv[1:]]) + "\\n")
if os.environ.get("FAKE_API_UP") != "1":
    print("curl: (7) Failed to connect to the API", file=sys.stderr)
    sys.exit(7)
print(json.dumps({"status": "ready"}))
"""


def _script(path: Path, body: str) -> None:
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(0o755)


@dataclass
class Instance:
    root: Path  # the temporary repository copy: deploy/ and .venv/bin
    home: Path
    env_file: Path
    sockets: Path
    served: Path
    adapters: tuple[Path, Path]
    fake_bin: Path
    log: Path
    state: Path
    tmux_env: Path

    @property
    def jeff(self) -> str:
        return str(self.root / ".venv" / "bin" / "jeff")

    @property
    def jeff_server(self) -> str:
        return str(self.root / ".venv" / "bin" / "jeff-server")

    @property
    def session_sh(self) -> str:
        return str(self.root / "deploy" / "session.sh")

    def socket(self, gpu: str) -> str:
        return str(self.sockets / f"engine-{gpu}.sock")

    def write_env(self, settings: dict[str, str]) -> None:
        self.env_file.write_text("".join(f"{name}={shlex.quote(value)}\n" for name, value in settings.items()))

    def seed(self, *sessions: str) -> None:
        self.state.write_text("\n".join(sessions))

    def sessions(self) -> list[str]:
        return self.state.read_text().split()

    def run(self, script: str, *, healthy: tuple[str, ...] = (), dies: tuple[str, ...] = (), api_up: bool = True,
            extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = {"PATH": f"{self.fake_bin}:/usr/bin:/bin", "HOME": str(self.home), "FAKE_LOG": str(self.log),
               "FAKE_TMUX_STATE": str(self.state), "FAKE_TMUX_ENV": str(self.tmux_env),
               "FAKE_HEALTHY": ":".join(healthy), "FAKE_TMUX_DIES": ",".join(dies),
               "FAKE_API_UP": "1" if api_up else "0", **(extra_env or {})}
        return subprocess.run(["/bin/bash", str(self.root / "deploy" / script), str(self.env_file)],
                              env=env, capture_output=True, text=True, timeout=30)

    def commands(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def tmux_commands(self) -> list[list[str]]:
        return [command for command in self.commands() if command[0] == "tmux"]

    def tmux_saw(self) -> list[list[str]]:
        """For each tmux call, the JEFF_* variables and Hugging Face tokens in its environment."""
        if not self.tmux_env.exists():
            return []
        return [json.loads(line) for line in self.tmux_env.read_text().splitlines()]


@pytest.fixture
def instance(tmp_path: Path) -> Iterator[Instance]:
    root = tmp_path / "repo"
    shutil.copytree(DEPLOY, root / "deploy")
    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    _script(venv_bin / "jeff", FAKE_JEFF)
    _script(venv_bin / "jeff-server", FAKE_JEFF)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _script(fake_bin / "tmux", FAKE_TMUX)
    _script(fake_bin / "curl", FAKE_CURL)
    home = tmp_path / "home"
    home.mkdir()
    sockets, served = tmp_path / "run", tmp_path / "served"
    adapters = (tmp_path / "adapters-a", tmp_path / "adapters-b")
    for folder in (sockets, served, *adapters):
        folder.mkdir()
    made = Instance(root=root, home=home, env_file=tmp_path / "private.env", sockets=sockets, served=served,
                    adapters=adapters, fake_bin=fake_bin, log=tmp_path / "commands.log", state=tmp_path / "tmux",
                    tmux_env=tmp_path / "tmux-env.log")
    made.seed()
    made.write_env(settings(made))
    yield made


def settings(instance: Instance) -> dict[str, str]:
    return {
        "JEFF_SESSION_PREFIX": "jeff",
        "JEFF_GPUS": "0,1",
        "JEFF_SOCKET_DIR": str(instance.sockets),
        "JEFF_SERVED_FILE_DIR": str(instance.served),
        "JEFF_ADAPTER_DIRS": f"{instance.adapters[0]}:{instance.adapters[1]}",
        "JEFF_BASE_VERSION": "jeff-base@v1.3",
        "JEFF_HOST": "127.0.0.1",
        "JEFF_PORT": "8795",
        "JEFF_WORKERS": "2",
        "JEFF_BATCH_WINDOW_MS": "2",
        "JEFF_MAX_BATCH_ROWS": "64",
        "JEFF_QUEUE_LIMIT_ROWS": "4096",
        "JEFF_PREPARE_WORKERS": "4",
        "JEFF_MAX_ADAPTERS": "32",
        "JEFF_PREFIX_POOL_MB": "8192",
        "JEFF_ENGINE_START_S": "5",
        "JEFF_API_START_S": "5",
        "JEFF_MODEL_NAME": "jeff-v1.3",
        "JEFF_API_KEY": "owner-key",
        "JEFF_FACTORY": "owner",
        "JEFF_FACTORY_GPU": "2",
        "HF_TOKEN": "hf-token",
    }


REQUIRED = ("JEFF_SESSION_PREFIX", "JEFF_GPUS", "JEFF_SOCKET_DIR", "JEFF_SERVED_FILE_DIR", "JEFF_ADAPTER_DIRS",
            "JEFF_BASE_VERSION", "JEFF_HOST", "JEFF_PORT", "JEFF_WORKERS", "JEFF_BATCH_WINDOW_MS",
            "JEFF_MAX_BATCH_ROWS", "JEFF_QUEUE_LIMIT_ROWS", "JEFF_PREPARE_WORKERS", "JEFF_ENGINE_START_S",
            "JEFF_API_START_S")


def engine_command(instance: Instance, gpu: str, *adapters: str) -> list[str]:
    command = [] if gpu == "mlx" else ["env", f"CUDA_VISIBLE_DEVICES={gpu}"]
    command += [instance.jeff, "runtime", "serve", "--socket", instance.socket(gpu),
                "--served-file", str(instance.served / f"served-{gpu}.json"),
                "--adapters-dir", str(instance.adapters[0]), "--adapters-dir", str(instance.adapters[1]),
                "--base-version", "jeff-base@v1.3", "--batch-window-ms", "2", "--max-batch-rows", "64",
                "--queue-limit-rows", "4096", "--prepare-workers", "4"]
    if gpu != "mlx":
        command += ["--max-adapters", "32", "--prefix-pool-mb", "8192"]  # the fast backend's, required there
    for adapter in adapters:
        command += ["--adapter", adapter]
    return command


def new_session(instance: Instance, session: str, command: list[str]) -> list[str]:
    return ["tmux", "new-session", "-d", "-s", session, "-c", str(instance.root), instance.session_sh,
            str(instance.env_file), str(instance.home / f"{session}.log"), *command]


def health(instance: Instance, gpu: str) -> list[str]:
    return ["jeff", "runtime", "health", "--socket", instance.socket(gpu), "--timeout-s", "5"]


def api_health(host: str) -> list[str]:
    return ["curl", "-fsS", "--max-time", "5", f"http://{host}:8795/health"]


def test_two_gpus_and_the_factory(instance: Instance) -> None:
    result = instance.run("run.sh", healthy=(instance.socket("0"), instance.socket("1")))
    assert result.returncode == 0, result.stderr
    sockets = f"{instance.socket('0')}:{instance.socket('1')}"
    assert instance.commands() == [
        ["tmux", "has-session", "-t", "=jeff-engine-0"],
        ["tmux", "has-session", "-t", "=jeff-engine-1"],
        ["tmux", "has-session", "-t", "=jeff-api"],
        ["tmux", "has-session", "-t", "=jeff-factory"],
        new_session(instance, "jeff-engine-0", engine_command(instance, "0")),
        new_session(instance, "jeff-engine-1", engine_command(instance, "1")),
        ["tmux", "has-session", "-t", "=jeff-engine-0"],
        health(instance, "0"),
        ["tmux", "has-session", "-t", "=jeff-engine-1"],
        health(instance, "1"),
        new_session(instance, "jeff-api", ["env", f"JEFF_ENGINE_SOCKETS={sockets}", instance.jeff_server, "serve",
                                           "--host", "127.0.0.1", "--port", "8795", "--workers", "2"]),
        ["tmux", "has-session", "-t", "=jeff-api"],
        api_health("127.0.0.1"),
        new_session(instance, "jeff-factory", ["env", "CUDA_VISIBLE_DEVICES=2", instance.jeff_server, "factory",
                                               "worker", "--engine-socket", instance.socket("0"),
                                               "--engine-socket", instance.socket("1")]),
    ]
    assert instance.sessions() == ["jeff-engine-0", "jeff-engine-1", "jeff-api", "jeff-factory"]
    assert str(instance.home / "jeff-api.log") in result.stdout
    assert "jeff-api answers at http://127.0.0.1:8795/health" in result.stdout
    assert instance.tmux_saw() == [[]] * len(instance.tmux_commands())  # no key ever reached tmux


def test_start_adapters_only_for_an_engine_with_no_served_file(instance: Instance) -> None:
    (instance.served / "served-1.json").write_text('{"format": "jeff-engine-served", "version": 1, "adapters": []}')
    instance.write_env({**settings(instance), "JEFF_START_ADAPTERS": "spam,jeff.legal.legal-clauses"})
    result = instance.run("run.sh", healthy=(instance.socket("0"), instance.socket("1")))
    assert result.returncode == 0, result.stderr
    started = [command for command in instance.tmux_commands() if command[1] == "new-session"]
    assert started[0] == new_session(instance, "jeff-engine-0",
                                     engine_command(instance, "0", "spam", "jeff.legal.legal-clauses"))
    assert started[1] == new_session(instance, "jeff-engine-1", engine_command(instance, "1"))
    assert "served-1.json exists" in result.stdout


def test_an_int4_base_folder_goes_to_every_gpu_engine(instance: Instance, tmp_path: Path) -> None:
    """JEFF_BASE_FOLDER: each GPU engine serves that INT4 base (`jeff runtime serve --base`, which refuses it unless
    its gate passed for every adapter served)."""
    folder = tmp_path / "base-int4-gptq"
    folder.mkdir()
    instance.write_env({**settings(instance), "JEFF_BASE_FOLDER": str(folder)})
    result = instance.run("run.sh", healthy=(instance.socket("0"), instance.socket("1")))
    assert result.returncode == 0, result.stderr
    started = [command for command in instance.tmux_commands() if command[1] == "new-session"]
    for gpu, command in zip(("0", "1"), started[:2], strict=True):
        assert command == new_session(instance, f"jeff-engine-{gpu}",
                                      [*engine_command(instance, gpu), "--base", str(folder)])


def test_an_int4_base_folder_must_exist_and_is_refused_with_mlx(instance: Instance, tmp_path: Path) -> None:
    instance.write_env({**settings(instance), "JEFF_BASE_FOLDER": str(tmp_path / "missing")})
    result = instance.run("run.sh")
    assert result.returncode == 1 and "JEFF_BASE_FOLDER" in result.stderr and "is not a folder" in result.stderr
    instance.write_env({**{key: value for key, value in settings(instance).items()
                           if key not in ("JEFF_FACTORY_GPU", "JEFF_MAX_ADAPTERS", "JEFF_PREFIX_POOL_MB")},
                        "JEFF_GPUS": "mlx", "JEFF_FACTORY": "off", "JEFF_BASE_FOLDER": str(tmp_path)})
    result = instance.run("run.sh")
    assert result.returncode == 1 and "JEFF_BASE_FOLDER serves an INT4 base, which needs NVIDIA GPUs" in result.stderr
    assert not [command for command in instance.tmux_commands() if command[1] == "new-session"]


def test_mlx_on_the_mac_without_the_factory(instance: Instance) -> None:
    instance.write_env({**{key: value for key, value in settings(instance).items()
                           if key not in ("JEFF_FACTORY_GPU", "JEFF_MAX_ADAPTERS", "JEFF_PREFIX_POOL_MB")},
                        "JEFF_GPUS": "mlx", "JEFF_FACTORY": "off"})
    result = instance.run("run.sh", healthy=(instance.socket("mlx"),))
    assert result.returncode == 0, result.stderr
    started = [command for command in instance.tmux_commands() if command[1] == "new-session"]
    assert started == [
        new_session(instance, "jeff-engine-mlx", engine_command(instance, "mlx")),
        new_session(instance, "jeff-api", ["env", f"JEFF_ENGINE_SOCKETS={instance.socket('mlx')}",
                                           instance.jeff_server, "serve", "--host", "127.0.0.1", "--port", "8795",
                                           "--workers", "2"]),
    ]


def test_max_adapters_is_required_on_gpus_and_refused_with_mlx(instance: Instance) -> None:
    """JEFF_MAX_ADAPTERS sizes the fast backend's stacked adapters, so a GPU engine needs it; the MLX engine has no
    such limit, so there it would do nothing and is refused, as `jeff runtime serve --max-adapters` refuses it."""
    instance.write_env({key: value for key, value in settings(instance).items() if key != "JEFF_MAX_ADAPTERS"})
    result = instance.run("run.sh", extra_env={"JEFF_MAX_ADAPTERS": "8"})  # only the env file counts
    assert result.returncode == 1
    assert "JEFF_MAX_ADAPTERS" in result.stderr and str(instance.env_file) in result.stderr
    instance.write_env({**{key: value for key, value in settings(instance).items()
                           if key not in ("JEFF_FACTORY_GPU", "JEFF_PREFIX_POOL_MB")},
                        "JEFF_GPUS": "mlx", "JEFF_FACTORY": "off"})
    result = instance.run("run.sh")
    assert result.returncode == 1
    assert "JEFF_MAX_ADAPTERS only applies to engines on NVIDIA GPUs" in result.stderr, result.stderr
    assert not [command for command in instance.tmux_commands() if command[1] == "new-session"]


def test_prefix_pool_mb_is_required_on_gpus_and_refused_with_mlx(instance: Instance) -> None:
    """JEFF_PREFIX_POOL_MB sizes the fast backend's prefix pool (`jeff runtime serve --prefix-pool-mb`, no default),
    so a GPU engine needs it; the MLX engine has no prefix pool, so there it is refused."""
    instance.write_env({key: value for key, value in settings(instance).items() if key != "JEFF_PREFIX_POOL_MB"})
    result = instance.run("run.sh", extra_env={"JEFF_PREFIX_POOL_MB": "8192"})  # only the env file counts
    assert result.returncode == 1
    assert "JEFF_PREFIX_POOL_MB" in result.stderr and str(instance.env_file) in result.stderr
    instance.write_env({**{key: value for key, value in settings(instance).items()
                           if key not in ("JEFF_FACTORY_GPU", "JEFF_MAX_ADAPTERS")},
                        "JEFF_GPUS": "mlx", "JEFF_FACTORY": "off"})
    result = instance.run("run.sh")
    assert result.returncode == 1
    assert "JEFF_PREFIX_POOL_MB only applies to engines on NVIDIA GPUs" in result.stderr, result.stderr
    assert not [command for command in instance.tmux_commands() if command[1] == "new-session"]


@pytest.mark.parametrize("name", REQUIRED)
def test_a_missing_setting_names_the_variable(instance: Instance, name: str) -> None:
    instance.write_env({key: value for key, value in settings(instance).items() if key != name})
    result = instance.run("run.sh", extra_env={name: "from-the-caller"})  # only the env file counts
    assert result.returncode == 1
    assert name in result.stderr and str(instance.env_file) in result.stderr
    assert not [command for command in instance.tmux_commands() if command[1] == "new-session"]


@pytest.mark.parametrize(("name", "value"), [("JEFF_GPUS", "0,,1"), ("JEFF_GPUS", "0,1,0"), ("JEFF_GPUS", "mlx,0"),
                                             ("JEFF_SESSION_PREFIX", "jeff.v13"), ("JEFF_ENGINE_START_S", "soon"),
                                             ("JEFF_FACTORY", "yes"), ("JEFF_START_ADAPTERS", "spam,"),
                                             ("JEFF_ENGINE_SOCKETS", "/tmp/e.sock"), ("JEFF_API_START_S", "0"),
                                             ("JEFF_FACTORY_GPU", "0,1"), ("JEFF_FACTORY_GPU", "mlx"),
                                             ("JEFF_MAX_ADAPTERS", "0"), ("JEFF_MAX_ADAPTERS", "many"),
                                             ("JEFF_MAX_ADAPTERS", "-4"), ("JEFF_PREFIX_POOL_MB", "8G"),
                                             ("JEFF_PREFIX_POOL_MB", "0"), ("JEFF_PREPARE_WORKERS", "0"),
                                             ("JEFF_PREPARE_WORKERS", "four")])
def test_a_malformed_setting_names_the_variable(instance: Instance, name: str, value: str) -> None:
    instance.write_env({**settings(instance), name: value})
    result = instance.run("run.sh")
    assert result.returncode == 1
    assert name in result.stderr
    assert not [command for command in instance.tmux_commands() if command[1] == "new-session"]


def test_a_missing_folder_is_named(instance: Instance) -> None:
    instance.adapters[1].rmdir()
    result = instance.run("run.sh")
    assert result.returncode == 1
    assert "JEFF_ADAPTER_DIRS" in result.stderr and str(instance.adapters[1]) in result.stderr


def test_a_missing_socket_folder_is_named(instance: Instance) -> None:
    instance.sockets.rmdir()
    result = instance.run("run.sh")
    assert result.returncode == 1
    assert f"JEFF_SOCKET_DIR={instance.sockets} is not a folder" in result.stderr


def test_a_missing_venv_is_named(instance: Instance) -> None:
    (instance.root / ".venv" / "bin" / "jeff-server").unlink()
    result = instance.run("run.sh")
    assert result.returncode == 1
    assert instance.jeff_server in result.stderr and "uv sync" in result.stderr


def test_an_existing_session_is_refused(instance: Instance) -> None:
    instance.seed("jeff-api")
    result = instance.run("run.sh", healthy=(instance.socket("0"), instance.socket("1")))
    assert result.returncode == 1
    assert "jeff-api" in result.stderr and "stop it first with deploy/stop.sh" in result.stderr
    assert all(command[1] == "has-session" for command in instance.tmux_commands())
    assert instance.sessions() == ["jeff-api"]


def test_an_engine_already_answering_at_the_socket_is_refused(instance: Instance) -> None:
    short = Path(tempfile.mkdtemp(prefix="jeff", dir="/tmp"))  # a Unix socket path must stay under 104 bytes
    try:
        instance.sockets = short
        instance.write_env(settings(instance))
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(instance.socket("1"))
        try:
            result = instance.run("run.sh", healthy=(instance.socket("1"),))
        finally:
            listener.close()
    finally:
        shutil.rmtree(short)
    assert result.returncode == 1
    assert instance.socket("1") in result.stderr
    assert not [command for command in instance.tmux_commands() if command[1] == "new-session"]


def test_an_engine_that_never_answers_is_named_with_its_log(instance: Instance) -> None:
    instance.write_env({**settings(instance), "JEFF_ENGINE_START_S": "1"})
    result = instance.run("run.sh", healthy=(instance.socket("0"),))
    assert result.returncode == 1
    assert "jeff-engine-1" in result.stderr and str(instance.home / "jeff-engine-1.log") in result.stderr
    assert "deploy/stop.sh" in result.stderr
    started = [command[4] for command in instance.tmux_commands() if command[1] == "new-session"]
    assert started == ["jeff-engine-0", "jeff-engine-1"]  # no API in front of a missing engine
    assert not [command for command in instance.tmux_commands() if command[1] == "kill-session"]


def test_an_engine_session_that_ends_fails_at_once(instance: Instance) -> None:
    instance.write_env({**settings(instance), "JEFF_ENGINE_START_S": "600"})
    result = instance.run("run.sh", healthy=(instance.socket("1"),), dies=("jeff-engine-0",))
    assert result.returncode == 1
    assert "jeff-engine-0" in result.stderr and str(instance.home / "jeff-engine-0.log") in result.stderr
    assert not [command for command in instance.tmux_commands() if command[1] == "kill-session"]


def test_stop_kills_each_session_of_the_instance(instance: Instance) -> None:
    instance.seed("jeff-engine-0", "jeff-engine-1", "jeff-api", "jeff-factory", "other")
    result = instance.run("stop.sh")
    assert result.returncode == 0, result.stderr
    assert instance.commands() == [
        ["tmux", "has-session", "-t", "=jeff-factory"],
        ["tmux", "kill-session", "-t", "=jeff-factory"],
        ["tmux", "has-session", "-t", "=jeff-api"],
        ["tmux", "kill-session", "-t", "=jeff-api"],
        ["tmux", "has-session", "-t", "=jeff-engine-0"],
        ["tmux", "kill-session", "-t", "=jeff-engine-0"],
        ["tmux", "has-session", "-t", "=jeff-engine-1"],
        ["tmux", "kill-session", "-t", "=jeff-engine-1"],
    ]
    assert instance.sessions() == ["other"]


def test_stop_skips_sessions_that_are_not_running(instance: Instance) -> None:
    instance.seed("jeff-engine-1")
    result = instance.run("stop.sh")
    assert result.returncode == 0, result.stderr
    assert [command for command in instance.commands() if command[1] == "kill-session"] == [
        ["tmux", "kill-session", "-t", "=jeff-engine-1"]]
    assert "jeff-api is not running" in result.stdout


def test_the_scripts_stop_nothing_but_their_own_sessions() -> None:
    """Only `tmux kill-session` of a named session: no kill, pkill or killall anywhere (comments aside)."""
    for name in SCRIPTS:
        code = [line.split("#", 1)[0] for line in (DEPLOY / name).read_text().splitlines()]
        words = " ".join(code).replace("kill-session", "").split()
        assert not [word for word in words if "kill" in word], name
    run = (DEPLOY / "run.sh").read_text() + (DEPLOY / "instance.sh").read_text()
    assert "kill-session" not in run


def test_the_scripts_parse_and_are_executable() -> None:
    for name in SCRIPTS:
        subprocess.run(["/bin/bash", "-n", str(DEPLOY / name)], check=True)
    for name in ("run.sh", "stop.sh", "session.sh"):
        assert os.access(DEPLOY / name, os.X_OK), name


def test_session_runs_the_command_with_only_the_env_file_settings(tmp_path: Path) -> None:
    env_file = tmp_path / "x.env"
    env_file.write_text("JEFF_MODEL_NAME=jeff-v1.3\nHF_HOME=/models\n")
    log = tmp_path / "jeff-api.log"
    result = subprocess.run(["/bin/bash", str(DEPLOY / "session.sh"), str(env_file), str(log), "env"],
                            env={"PATH": "/usr/bin:/bin", "JEFF_PUBLIC": "1"}, capture_output=True, text=True,
                            check=True)
    shown = result.stdout.splitlines()
    assert "JEFF_MODEL_NAME=jeff-v1.3" in shown and "HF_HOME=/models" in shown
    assert not [line for line in shown if line.startswith("JEFF_PUBLIC=")]
    logged = log.read_text()
    assert "JEFF_MODEL_NAME=jeff-v1.3" in logged and logged.splitlines()[0].endswith(" env")


def _example(name: str) -> dict[str, str]:
    """The settings an env example sets, as the scripts read it (sourced by bash, with HOME=/home/owner)."""
    shown = subprocess.run(["/bin/bash", "-c", f"set -a; . {shlex.quote(str(DEPLOY / name))}; set +a; env -0"],
                           env={"PATH": "/usr/bin:/bin", "HOME": "/home/owner"}, capture_output=True, text=True,
                           check=True).stdout
    pairs = (item.split("=", 1) for item in shown.split("\0") if item.startswith("JEFF_"))
    return {name: value for name, value in pairs}


def test_the_private_example_runs_the_factory_with_blank_keys() -> None:
    private = _example("private.env.example")
    assert set(REQUIRED) <= set(private)
    assert private["JEFF_FACTORY"] == "owner" and "JEFF_PUBLIC" not in private
    assert private["JEFF_FACTORY_GPU"] in private["JEFF_GPUS"].split(",")  # in the example, one an engine uses too
    assert private["JEFF_ACCESS_LOG"] == "/home/owner/jeff-private/access.log" and "JEFF_ADMINS" in private
    assert private["JEFF_API_KEY"] == private["JEFF_TEACHER_KEY"] == ""
    assert private["JEFF_MAX_ADAPTERS"] == "32" and private["JEFF_PREFIX_POOL_MB"] == "8192"  # GPU engines
    assert "chmod 600" in (DEPLOY / "private.env.example").read_text()


def test_the_public_example_is_limited_and_has_no_factory() -> None:
    public = _example("public.env.example")
    assert set(REQUIRED) <= set(public)
    assert public["JEFF_PUBLIC"] == "1" and public["JEFF_RATE_LIMIT"] == "20/8"
    assert not [name for name in public if name.startswith(("JEFF_FACTORY", "JEFF_TEACHER", "JEFF_TRAINER"))]
    assert public["JEFF_API_KEY"] == ""
    assert public["JEFF_MAX_ADAPTERS"] == "32" and public["JEFF_PREFIX_POOL_MB"] == "8192"  # GPU engines
    assert "chmod 600" in (DEPLOY / "public.env.example").read_text()
    private = _example("private.env.example")
    for name in ("JEFF_SESSION_PREFIX", "JEFF_SOCKET_DIR", "JEFF_SERVED_FILE_DIR", "JEFF_PORT"):
        assert public[name] != private[name], name  # the two instances can run side by side
    assert not set(public["JEFF_GPUS"].split(",")) & set(private["JEFF_GPUS"].split(","))


def test_the_factory_needs_its_gpu_and_only_with_the_factory(instance: Instance) -> None:
    instance.write_env({key: value for key, value in settings(instance).items() if key != "JEFF_FACTORY_GPU"})
    result = instance.run("run.sh")
    assert result.returncode == 1 and "JEFF_FACTORY_GPU" in result.stderr, result.stderr
    instance.write_env({**settings(instance), "JEFF_FACTORY": "off"})
    result = instance.run("run.sh")
    assert result.returncode == 1 and "JEFF_FACTORY_GPU only applies with JEFF_FACTORY=owner" in result.stderr
    assert not [command for command in instance.tmux_commands() if command[1] == "new-session"]


def test_an_api_that_never_answers_is_named_with_its_log(instance: Instance) -> None:
    instance.write_env({**settings(instance), "JEFF_API_START_S": "1"})
    result = instance.run("run.sh", healthy=(instance.socket("0"), instance.socket("1")), api_up=False)
    assert result.returncode == 1
    assert "jeff-api" in result.stderr and str(instance.home / "jeff-api.log") in result.stderr, result.stderr
    assert "http://127.0.0.1:8795/health" in result.stderr and "deploy/stop.sh" in result.stderr
    started = [command[4] for command in instance.tmux_commands() if command[1] == "new-session"]
    assert started == ["jeff-engine-0", "jeff-engine-1", "jeff-api"]  # no factory behind a missing API


def test_an_api_session_that_ends_fails_at_once(instance: Instance) -> None:
    instance.write_env({**settings(instance), "JEFF_API_START_S": "600"})
    result = instance.run("run.sh", healthy=(instance.socket("0"), instance.socket("1")), dies=("jeff-api",),
                          api_up=False)
    assert result.returncode == 1
    assert "the API session jeff-api ended" in result.stderr and str(instance.home / "jeff-api.log") in result.stderr


@pytest.mark.parametrize(("host", "asked"), [("0.0.0.0", "127.0.0.1"), ("::", "[::1]"), ("10.0.0.5", "10.0.0.5")])
def test_an_api_on_every_address_is_asked_on_the_loopback(instance: Instance, host: str, asked: str) -> None:
    instance.write_env({**settings(instance), "JEFF_HOST": host})
    result = instance.run("run.sh", healthy=(instance.socket("0"), instance.socket("1")))
    assert result.returncode == 0, result.stderr
    assert api_health(asked) in instance.commands()


def test_stop_gives_tmux_no_key(instance: Instance) -> None:
    instance.seed("jeff-engine-0", "jeff-api")
    result = instance.run("stop.sh", extra_env={"JEFF_API_KEY": "from-the-caller"})
    assert result.returncode == 0, result.stderr
    assert instance.tmux_saw() == [[]] * len(instance.tmux_commands())
