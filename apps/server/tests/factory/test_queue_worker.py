"""The factory's job queue and worker process (jeff_server.factory.queue, .worker), the owner-only /v1/factory routes
and the factory settings: the API only queues jobs, one worker runs them in order, a worker that stopped leaves no
step "running" forever, and the API holds no state of its own."""

import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jeff.errors import Conflict, EngineError, InvalidRequest
from jeff.testing import FakeEngine
from jeff_runtime.testing import Running

from jeff_server.app import create_app
from jeff_server.factory import describe, queue, runner, worker
from jeff_server.factory.config import FactoryConfig
from jeff_server.factory.runner import Factory
from jeff_server.factory.routes import complete_lines
from jeff_server.factory.store import Run, new_run, read_json, write_json
from jeff_server.factory.teacher import InvalidReply
from jeff_server.factory.worker import STALE_S, Heartbeat, run_worker, worker_status
from jeff_server.settings import ServerSettings

from server_testing import hub_keys, token
from factory_testing import fake_scorer, trading_spec, trained_run, worker_hooks

MINIMAL = {"JEFF_ENGINE_SOCKETS": "/tmp/engine-0.sock", "JEFF_MODEL_NAME": "jeff-v1.3"}


def owner_config(root: Path) -> FactoryConfig:
    return FactoryConfig(mode="owner", root=root, teacher_url="u", teacher_key="k", teacher_model="m",
                         trainer=root / "trainer", min_free_gb=16.0)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Path]:
    """An engine socket whose base is a folder on this machine, as the worker needs."""
    fake = FakeEngine(lambda adapter, row: [1.0], set())
    fake.base_checkpoint = str(tmp_path / "base")
    (tmp_path / "base").mkdir()
    running = Running(fake)
    try:
        yield running.path
    finally:
        running.close()


def copied_run(config: FactoryConfig, name: str) -> Run:
    """A run whose interpret step copies an earlier run's spec, so it needs no teacher call."""
    run = new_run(config.runs, name, "describe", None, spec_from="earlier-run")
    write_json(run.folder / "copied-spec.json", {**trading_spec(), "name": name})
    return run


def app_client(settings: ServerSettings) -> TestClient:
    return TestClient(create_app(settings, engine=FakeEngine(lambda adapter, row: [1.0], set())))


def test_a_second_job_for_a_run_is_a_conflict(tmp_path: Path) -> None:
    first = queue.enqueue(tmp_path, "run-1", ["interpret"])
    with pytest.raises(Conflict, match="already has a job waiting"):
        queue.enqueue(tmp_path, "run-1", ["data_prep"])
    queue.enqueue(tmp_path, "run-2", ["interpret"])  # another run queues behind it
    job = queue.claim(tmp_path)
    assert job is not None and job.id == first and (tmp_path / "queue" / "claimed" / f"{first}.json").exists()
    with pytest.raises(Conflict, match="already has a job running"):
        queue.enqueue(tmp_path, "run-1", ["data_prep"])
    queue.finish(job)
    queue.enqueue(tmp_path, "run-1", ["data_prep"])


def test_two_jobs_run_in_order_one_at_a_time(tmp_path: Path, engine: Path) -> None:
    config = owner_config(tmp_path)
    runs = [copied_run(config, "first"), copied_run(config, "second")]
    for run in runs:
        Factory(config, None).launch(run, ["interpret"])
    run_worker(config, [engine], [tmp_path], None, "jeff-v1.3", once=True)
    states = [Run(run.folder).state for run in runs]
    assert [state["steps"]["interpret"]["status"] for state in states] == ["done", "done"]
    assert states[0]["steps"]["interpret"]["ended"] <= states[1]["steps"]["interpret"]["started"]
    assert [state["status"] for state in states] == ["review", "review"]
    assert queue.waiting(tmp_path) == [] and queue.claimed(tmp_path) == []
    status = read_json(tmp_path / "worker.json")
    assert status["current_job"] is None and status["pid"] > 0


def test_a_job_left_by_a_worker_that_died_is_failed_at_the_next_start(tmp_path: Path, engine: Path) -> None:
    config = owner_config(tmp_path)
    running, waiting_only = copied_run(config, "running"), copied_run(config, "waiting")
    factory = Factory(config, None)
    factory.launch(running, ["interpret"])
    queue.claim(tmp_path)
    running.set_step("interpret", status="running", started="2026-10-23T10:00:00.000+00:00")
    running.update(status="running interpret")
    factory.launch(waiting_only, ["interpret"])
    queue.claim(tmp_path)  # claimed, but its worker died before the step started
    stale = (datetime.now(timezone.utc) - timedelta(seconds=STALE_S * 4)).isoformat()
    write_json(tmp_path / "worker.json", {"pid": 1, "started": stale, "heartbeat": stale, "current_job": None})
    assert worker_status(tmp_path)["running"] is False and worker_status(tmp_path)["current_job"] is not None

    run_worker(config, [engine], [tmp_path], None, "jeff-v1.3", once=True)
    state = Run(running.folder).state
    assert state["steps"]["interpret"] == {"status": "failed", "started": "2026-10-23T10:00:00.000+00:00",
                                           "error": "the worker stopped during interpret"}
    assert state["status"] == "failed" and not state["clean"]
    assert state["unclean_reasons"] == ["the worker stopped during interpret"]
    other = Run(waiting_only.folder)
    assert other.state["status"] == "failed" and other.state["steps"] == {}
    assert other.events()[-1]["kind"] == "job.failed"
    assert queue.claimed(tmp_path) == []


def test_a_claimed_job_whose_steps_all_finished_is_only_cleared(tmp_path: Path, engine: Path) -> None:
    config = owner_config(tmp_path)
    run = copied_run(config, "finished")
    Factory(config, None).launch(run, ["interpret"])
    queue.claim(tmp_path)
    run.set_step("interpret", status="done")
    run.update(status="review")  # the worker stopped after the step, before deleting the claim
    run_worker(config, [engine], [tmp_path], None, "jeff-v1.3", once=True)
    assert Run(run.folder).state["status"] == "review" and queue.claimed(tmp_path) == []
    assert [event["kind"] for event in Run(run.folder).events()] == []


def test_a_trainer_still_running_stops_the_worker_starting(tmp_path: Path, engine: Path) -> None:
    config = owner_config(tmp_path)
    run = copied_run(config, "training")
    run.set_step("train", status="running", pid=os.getpid())  # a live process
    with pytest.raises(EngineError, match=f"trainer \\(process {os.getpid()}\\) is still running.*stop that "
                                          "trainer first"):
        run_worker(config, [engine], [tmp_path], None, "jeff-v1.3", once=True)
    assert Run(run.folder).state["steps"]["train"]["status"] == "running"  # nothing changed
    finished = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    run.set_step("train", pid=int(finished.stdout))  # a process that has ended
    run_worker(config, [engine], [tmp_path], None, "jeff-v1.3", once=True)
    assert Run(run.folder).state["steps"]["train"]["error"] == "the worker stopped during train"


def test_the_worker_stops_when_its_heartbeat_dies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "HEARTBEAT_S", 0.01)
    heartbeat = Heartbeat(tmp_path)
    heartbeat.start()
    heartbeat.check()  # alive
    monkeypatch.setattr(heartbeat, "path", tmp_path / "missing" / "worker.json")  # the next write fails
    heartbeat.thread.join(5)
    with pytest.raises(EngineError, match="heartbeat stopped \\(FileNotFoundError"):
        heartbeat.check()


def test_the_event_stream_sends_only_complete_lines() -> None:
    assert complete_lines('{"a": 1}\n{"b": 2}\n{"c":') == ['{"a": 1}', '{"b": 2}']
    assert complete_lines("") == [] and complete_lines('{"half"') == []


def test_a_review_waiting_for_the_person_is_not_failed_at_worker_start(tmp_path: Path, engine: Path) -> None:
    config = owner_config(tmp_path)
    run = copied_run(config, "reviewing")
    run.set_step("interpret", status="done")
    run.set_step("review", status="running")
    run.update(status="review")
    run_worker(config, [engine], [tmp_path], None, "jeff-v1.3", once=True)
    assert Run(run.folder).state["status"] == "review" and Run(run.folder).state["clean"]


def test_a_second_worker_is_refused_while_one_runs(tmp_path: Path, engine: Path) -> None:
    import fcntl

    config = owner_config(tmp_path)
    write_json(tmp_path / "worker.json", {"pid": 4242, "started": "2026-10-23T10:00:00+00:00",
                                          "heartbeat": "2026-10-23T10:00:00+00:00", "current_job": None})
    with (tmp_path / "worker.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(EngineError, match="Another factory worker is running \\(process 4242"):
            run_worker(config, [engine], [tmp_path], None, "jeff-v1.3", once=True)


def test_the_worker_needs_owner_mode_and_an_engine(tmp_path: Path) -> None:
    with pytest.raises(InvalidRequest, match="JEFF_FACTORY=owner"):
        run_worker(FactoryConfig(mode="off", root=tmp_path), [tmp_path / "e.sock"], [tmp_path], None, "jeff-v1.3", once=True)
    with pytest.raises(InvalidRequest, match="at least one --engine-socket"):
        run_worker(owner_config(tmp_path), [], [tmp_path], None, "jeff-v1.3", once=True)


def test_the_api_restarting_leaves_a_running_step_alone(tmp_path: Path) -> None:
    config = owner_config(tmp_path)
    run = copied_run(config, "training")
    Factory(config, None).launch(run, ["interpret"])
    job = queue.claim(tmp_path)
    assert job is not None
    run.set_step("interpret", status="running", started="2026-10-23T10:00:00.000+00:00")
    settings = ServerSettings(engine_sockets=(tmp_path / "e.sock",), model_name="jeff-v1.3", factory=config)
    for _ in range(2):  # start the API, stop it, start it again
        with app_client(settings) as client:
            answer = client.get(f"/v1/factory/runs/{run.id}").json()
            assert answer["run"]["steps"]["interpret"]["status"] == "running"
            assert answer["busy"] == run.id and answer["job"]["state"] == "running"
            assert client.get("/v1/factory/worker").json() == {"running": False, "heartbeat_age_s": None,
                                                               "current_job": job.public()}
    assert Run(run.folder).state["steps"]["interpret"]["status"] == "running"


def test_the_worker_status_follows_the_heartbeat(tmp_path: Path) -> None:
    fresh = datetime.now(timezone.utc).isoformat()
    write_json(tmp_path / "worker.json", {"pid": 1, "started": fresh, "heartbeat": fresh, "current_job": None})
    status = worker_status(tmp_path)
    assert status["running"] is True and status["heartbeat_age_s"] < STALE_S and status["current_job"] is None
    old = (datetime.now(timezone.utc) - timedelta(seconds=STALE_S + 1)).isoformat()
    write_json(tmp_path / "worker.json", {"pid": 1, "started": old, "heartbeat": old, "current_job": None})
    assert worker_status(tmp_path)["running"] is False


def test_starting_a_run_queues_its_interpretation_and_never_runs_it(tmp_path: Path) -> None:
    config = owner_config(tmp_path)
    write_json(config.folder("description-checks") / "check-1.json", {
        "id": "check-1", "started": "2026-10-17T10:00:00.000+00:00", "ended": "2026-10-17T10:00:09.000+00:00",
        "description": "Route emails.", "examples": None, "teacher_model": "glm", "teacher": {"calls": 1},
        "result": {"ready": True, "summary": "s", "problems": [], "questions": [], "improved_description": "Route.",
                   "changes": []}, "used_by": None})
    settings = ServerSettings(engine_sockets=(tmp_path / "e.sock",), model_name="jeff-v1.3", factory=config)
    with app_client(settings) as client:
        answer = client.post("/v1/factory/runs", json={"check_id": "check-1", "choice": "original",
                                                       "description": "Route emails."}).json()
        assert answer["job"]["state"] == "waiting" and answer["job"]["steps"] == ["interpret"]
        assert answer["run"]["steps"].get("interpret") is None  # queued, not run
        again = client.post(f"/v1/factory/runs/{answer['run']['id']}/steps/data_prep")
        assert again.status_code == 409 and again.json()["error"]["type"] == "Conflict"
        missing = client.get("/v1/factory/runs/nothing-here")
        assert missing.status_code == 422 and missing.json()["error"]["message"] == "No run 'nothing-here'"
        bad_name = client.post("/v1/factory/runs/import", json={"reference": "none", "name": "Bad Name",
                                                                "description": "d"})
        assert bad_name.status_code == 422 and bad_name.json()["error"]["type"] == "InvalidRequest"
    assert [job.run for job in queue.waiting(tmp_path)] == [answer["run"]["id"]]


def test_the_factory_routes_answer_factory_off_when_it_is_off(tmp_path: Path) -> None:
    settings = ServerSettings.from_env({**MINIMAL, "JEFF_FACTORY": "off", "JEFF_FACTORY_DIR": str(tmp_path)})
    with app_client(settings) as client:
        for response in (client.get("/v1/factory/runs"), client.get("/v1/factory/worker"),
                         client.post("/v1/factory/previews", json={"check_id": "c", "choice": "original",
                                                                   "description": "d"})):
            assert response.status_code == 403
            assert response.json()["error"]["type"] == "FactoryOff"
    # With the teacher settings, previews (and description checks) are offered with the factory off.
    with app_client(ServerSettings.from_env({**MINIMAL, **off_with_teacher(tmp_path)})) as client:
        assert client.get("/v1/factory/runs").json()["error"]["type"] == "FactoryOff"
        assert client.get("/v1/factory/mode").json()["preview"] is True
        missing = client.post("/v1/factory/previews", json={"check_id": "c", "choice": "original", "description": "d"})
        assert missing.status_code == 422 and "No description check 'c'" in missing.json()["error"]["message"]


def off_with_teacher(root: Path) -> dict[str, str]:
    return {"JEFF_FACTORY": "off", "JEFF_FACTORY_DIR": str(root), "JEFF_TEACHER_URL": "http://teacher",
            "JEFF_TEACHER_KEY": "k", "JEFF_TEACHER_MODEL": "glm"}


def write_check(root: Path, check_id: str) -> None:
    (root / "description-checks").mkdir(exist_ok=True)
    write_json(root / "description-checks" / f"{check_id}.json", {
        "id": check_id, "started": "2026-10-17T10:00:00.000+00:00", "ended": "2026-10-17T10:00:09.000+00:00",
        "description": "Route emails.", "examples": None, "teacher_model": "glm", "teacher": {"calls": 1},
        "result": {"ready": True, "summary": "s", "problems": [], "questions": [], "improved_description": "Route.",
                   "changes": []}, "used_by": None})


@pytest.fixture
def interpreted(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """Previews interpret with no teacher: each waits for the returned event, then writes the trading spec."""
    release = threading.Event()

    def interpret(self: Factory, run: Run, teacher: Any) -> dict[str, Any]:
        assert release.wait(10), "the test never released the preview"
        write_json(run.folder / "spec.json", trading_spec())
        return {"teacher_model": "fake"}

    monkeypatch.setattr(runner.Factory, "do_interpret", interpret)
    return release


def wait_for(check: Any) -> None:
    for _ in range(500):
        if check():
            return
        time.sleep(0.01)
    raise AssertionError("timed out")


def test_a_preview_is_interpreted_in_the_api_with_the_factory_off(tmp_path: Path,
                                                                  interpreted: threading.Event) -> None:
    write_check(tmp_path, "check-1")
    interpreted.set()
    with app_client(ServerSettings.from_env({**MINIMAL, **off_with_teacher(tmp_path)})) as client:
        started = client.post("/v1/factory/previews", json={"check_id": "check-1", "choice": "original",
                                                            "description": "Route emails."})
        assert started.status_code == 200 and started.json()["job"] is None
        run_id = started.json()["run"]["id"]
        wait_for(lambda: client.get(f"/v1/factory/previews/{run_id}").json()["run"]["status"] == "review")
        answer = client.get(f"/v1/factory/previews/{run_id}").json()
        assert answer["run"]["steps"]["interpret"]["status"] == "done" and answer["spec"]["name"] == "trading-calls"
    assert not (tmp_path / "queue").exists()  # a preview never creates a queue file


NAMES = ["alpha", "bravo", "charlie", "delta", "echo"]


def test_a_fifth_preview_at_once_is_a_conflict(tmp_path: Path, interpreted: threading.Event) -> None:
    for number in range(5):
        write_check(tmp_path, f"check-{number}")
    with app_client(ServerSettings.from_env({**MINIMAL, **off_with_teacher(tmp_path)})) as client:
        ids = []
        for number in range(4):
            # Distinct names: a run folder is <name>-<time to the second>.
            answer = client.post("/v1/factory/previews", json={"check_id": f"check-{number}", "choice": "original",
                                                               "description": "Route emails.", "name": NAMES[number]})
            assert answer.status_code == 200
            ids.append(answer.json()["run"]["id"])
        fifth = client.post("/v1/factory/previews", json={"check_id": "check-4", "choice": "original",
                                                          "description": "Route emails.", "name": NAMES[4]})
        assert fifth.status_code == 409 and fifth.json()["error"]["type"] == "Conflict"
        assert "Several previews are being interpreted" in fifth.json()["error"]["message"]
        # Refused before anything was written: no run folder, and the check is still unused.
        assert sorted(folder.name for folder in (tmp_path / "previews").iterdir()) == sorted(ids)
        assert read_json(tmp_path / "description-checks" / "check-4.json")["used_by"] is None
        interpreted.set()
        for run_id in ids:
            wait_for(lambda: client.get(f"/v1/factory/previews/{run_id}").json()["run"]["status"] == "review")
    assert not (tmp_path / "queue").exists()


def test_a_preview_left_interpreting_is_failed_when_the_api_starts(tmp_path: Path) -> None:
    preview = new_run(tmp_path / "previews", "demo", "describe", None)
    preview.set_step("interpret", status="running", started="2026-10-23T10:00:00.000+00:00")
    with app_client(ServerSettings.from_env({**MINIMAL, **off_with_teacher(tmp_path)})) as client:
        answer = client.get(f"/v1/factory/previews/{preview.id}").json()
    assert answer["run"]["steps"]["interpret"]["error"] == "the server restarted during interpret"
    assert answer["run"]["status"] == "failed" and not answer["run"]["clean"]
    assert answer["run"]["unclean_reasons"] == ["the server restarted during interpret"]


def test_a_preview_a_live_sibling_api_process_interprets_is_left_alone(tmp_path: Path) -> None:
    sibling = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    ended = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    try:
        live, dead = (new_run(tmp_path / "previews", name, "describe", None) for name in ("live", "dead"))
        live.set_step("interpret", status="running", started="2026-10-23T10:00:00.000+00:00", pid=sibling.pid)
        dead.set_step("interpret", status="running", started="2026-10-23T10:00:00.000+00:00", pid=int(ended.stdout))
        with app_client(ServerSettings.from_env({**MINIMAL, **off_with_teacher(tmp_path)})):
            pass
        assert Run(live.folder).state["steps"]["interpret"]["status"] == "running"
        assert Run(dead.folder).state["steps"]["interpret"]["error"] == "the server restarted during interpret"
    finally:
        sibling.kill()
        sibling.wait()


def test_a_preview_records_the_api_process_interpreting_it(tmp_path: Path, interpreted: threading.Event) -> None:
    write_check(tmp_path, "check-1")
    with app_client(ServerSettings.from_env({**MINIMAL, **off_with_teacher(tmp_path)})) as client:
        run_id = client.post("/v1/factory/previews", json={"check_id": "check-1", "choice": "original",
                                                           "description": "Route emails."}).json()["run"]["id"]
        wait_for(lambda: client.get(f"/v1/factory/previews/{run_id}").json()["run"]["steps"].get("interpret"))
        assert client.get(f"/v1/factory/previews/{run_id}").json()["run"]["steps"]["interpret"]["pid"] == os.getpid()
        interpreted.set()
        wait_for(lambda: client.get(f"/v1/factory/previews/{run_id}").json()["run"]["status"] == "review")


def test_a_teacher_failure_in_a_description_check_is_an_engine_error(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenTeacher:
        def __init__(self, *args: Any) -> None:
            self.usage = None

        def ask(self, *args: Any, **kwargs: Any) -> Any:
            raise InvalidReply("The teacher's reply to 'description_check' stayed invalid after 5 attempts: x")

    monkeypatch.setattr(describe, "Teacher", BrokenTeacher)
    with app_client(ServerSettings.from_env({**MINIMAL, **off_with_teacher(tmp_path)})) as client:
        broken = client.post("/v1/factory/description-checks", json={"description": "Route emails."})
        assert broken.status_code == 500 and broken.json()["error"]["type"] == "EngineError"
        assert broken.json()["error"]["message"].startswith("The teacher failed: InvalidReply: ")
        empty = client.post("/v1/factory/description-checks", json={"description": "  "})
        assert empty.status_code == 422 and empty.json()["error"]["message"] == "Describe the decision first"


def test_a_hub_token_is_not_the_owner(tmp_path: Path) -> None:
    private, public = hub_keys(tmp_path)
    settings = ServerSettings(engine_sockets=(tmp_path / "e.sock",), model_name="jeff-v1.3", api_key="the-key",
                              hub_public_key=public, factory=owner_config(tmp_path))
    customer = {"Authorization": f"Bearer {token(private, sub='customer')}"}
    with app_client(settings) as client:
        for response in (client.get("/v1/factory/runs", headers=customer),
                         client.get("/v1/factory/mode", headers=customer),
                         client.post("/v1/factory/description-checks", json={"description": "d"}, headers=customer)):
            assert response.status_code == 403
            assert response.json()["error"]["type"] == "NotLicensed"
        owner = client.get("/v1/factory/runs", headers={"Authorization": "Bearer the-key"})
        assert owner.status_code == 200 and owner.json() == {"runs": [], "busy": None}


def test_calibration_and_evaluation_run_in_the_worker(tmp_path: Path) -> None:
    config = owner_config(tmp_path)
    run, labels = trained_run(config, trading_spec())
    adapter = run.folder / "training" / "checkpoints" / "final"
    factory = Factory(config, worker_hooks(tmp_path, (), fake_scorer(labels)))

    assert factory.step(run, "calibrate") and factory.step(run, "evaluate")
    metrics = read_json(run.folder / "metrics.json")
    assert metrics["synthetic"]["accuracy"] == 1.0 and metrics["synthetic"]["rows"] == 50
    assert metrics["calibration"]["trainer_temperature"] == 1.0
    assert read_json(adapter / "decision_config.json")["temperature"] == metrics["calibration"]["temperature"]
    assert (run.folder / "test-logits.json").exists() and (run.folder / "calibration-logits.json").exists()
    assert run.state["clean"]


def test_the_api_never_runs_a_step(tmp_path: Path) -> None:
    run = copied_run(owner_config(tmp_path), "api")
    with pytest.raises(EngineError, match="factory worker"):
        Factory(owner_config(tmp_path), None).step(run, "interpret")
    assert run.state["steps"] == {}


def test_queueing_one_run_from_many_threads_at_once_gives_one_job(tmp_path: Path) -> None:
    """Many threads queueing the same run at once: exactly one job, the rest Conflict (the queue's flock, which also
    orders the API's worker processes)."""
    results: list[str] = []

    def one() -> None:
        try:
            queue.enqueue(tmp_path, "run-1", ["interpret"])
            results.append("queued")
        except Conflict:
            results.append("conflict")

    threads = [threading.Thread(target=one) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["conflict"] * 7 + ["queued"] and len(queue.waiting(tmp_path)) == 1


# --- settings ------------------------------------------------------------------------------------------------------


def trainer(tmp_path: Path) -> Path:
    folder = tmp_path / "trainer"
    (folder / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    (folder / ".venv" / "bin" / "jeff-train").write_text("")
    (folder / "TRAINER_COMMIT").write_text("abc\n")
    return folder


def owner_env(tmp_path: Path) -> dict[str, str]:
    return {"JEFF_FACTORY": "owner", "JEFF_FACTORY_DIR": str(tmp_path), "JEFF_TEACHER_URL": "http://teacher/",
            "JEFF_TEACHER_KEY": "k", "JEFF_TEACHER_MODEL": "glm", "JEFF_TRAINER": str(trainer(tmp_path)),
            "JEFF_FACTORY_MIN_FREE_GB": "16"}


def test_owner_mode_reads_every_setting(tmp_path: Path) -> None:
    config = FactoryConfig.from_env(owner_env(tmp_path))
    assert config == FactoryConfig(mode="owner", root=tmp_path, teacher_url="http://teacher", teacher_key="k",
                                   teacher_model="glm", trainer=tmp_path / "trainer", min_free_gb=16.0)
    adapters = tmp_path / "adapters"
    adapters.mkdir()
    settings = ServerSettings.from_env({**MINIMAL, **owner_env(tmp_path), "JEFF_ADAPTER_DIRS": str(adapters)})
    assert settings.factory == config
    with pytest.raises(InvalidRequest, match="JEFF_FACTORY=owner needs JEFF_ADAPTER_DIRS"):
        ServerSettings.from_env({**MINIMAL, **owner_env(tmp_path)})


@pytest.mark.parametrize("name", ["JEFF_FACTORY_DIR", "JEFF_TEACHER_URL", "JEFF_TEACHER_KEY", "JEFF_TEACHER_MODEL",
                                  "JEFF_TRAINER", "JEFF_FACTORY_MIN_FREE_GB"])
def test_owner_mode_needs_every_setting(tmp_path: Path, name: str) -> None:
    env = owner_env(tmp_path)
    del env[name]
    with pytest.raises(InvalidRequest, match=f"JEFF_FACTORY=owner needs {name}"):
        FactoryConfig.from_env(env)


def test_factory_settings_without_jeff_factory_are_refused(tmp_path: Path) -> None:
    assert FactoryConfig.from_env({}) == FactoryConfig.off()
    with pytest.raises(InvalidRequest, match="JEFF_FACTORY_DIR set without JEFF_FACTORY"):
        FactoryConfig.from_env({"JEFF_FACTORY_DIR": str(tmp_path)})
    with pytest.raises(InvalidRequest, match="JEFF_TRAINER only apply with JEFF_FACTORY=owner"):
        FactoryConfig.from_env({"JEFF_FACTORY": "off", "JEFF_TRAINER": str(tmp_path)})
    with pytest.raises(InvalidRequest, match="set JEFF_FACTORY_DIR, or unset the teacher settings"):
        FactoryConfig.from_env({"JEFF_FACTORY": "off", "JEFF_TEACHER_URL": "u", "JEFF_TEACHER_KEY": "k",
                                "JEFF_TEACHER_MODEL": "m"})
    with pytest.raises(InvalidRequest, match="JEFF_FACTORY='on'; use off or owner"):
        FactoryConfig.from_env({"JEFF_FACTORY": "on"})
    with pytest.raises(InvalidRequest, match="JEFF_FACTORY_MIN_FREE_GB='lots'"):
        FactoryConfig.from_env({**owner_env(tmp_path), "JEFF_FACTORY_MIN_FREE_GB": "lots"})
    with pytest.raises(InvalidRequest, match="jeff-train is missing"):
        FactoryConfig.from_env({**owner_env(tmp_path), "JEFF_TRAINER": str(tmp_path / "nowhere")})


def test_a_public_server_refuses_the_factory(tmp_path: Path) -> None:
    public = {**MINIMAL, "JEFF_PUBLIC": "1", "JEFF_API_KEY": "k", "JEFF_RATE_LIMIT": "10/4"}
    assert ServerSettings.from_env({**public, "JEFF_FACTORY": "off"}).factory.mode == "off"
    with pytest.raises(InvalidRequest, match="JEFF_PUBLIC=1 runs without the adapter factory; unset "
                                             "JEFF_FACTORY, JEFF_FACTORY_DIR"):
        ServerSettings.from_env({**public, "JEFF_FACTORY": "owner", "JEFF_FACTORY_DIR": str(tmp_path)})
    with pytest.raises(InvalidRequest, match="unset JEFF_TEACHER_URL"):
        ServerSettings.from_env({**public, "JEFF_TEACHER_URL": "http://teacher"})


def test_the_worker_command_needs_the_adapter_folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jeff_server import cli

    for name, value in owner_env(tmp_path).items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("JEFF_ADAPTER_DIRS", raising=False)
    with pytest.raises(SystemExit, match="set JEFF_ADAPTER_DIRS"):
        cli.main(["factory", "worker", "--engine-socket", str(tmp_path / "e.sock")])


def test_an_engine_whose_health_has_no_base_id_or_version_stops_the_worker(
        tmp_path: Path, engine: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jeff.socket_engine import SocketEngine

    health = SocketEngine.health

    def older(self: SocketEngine, timeout_s: float | None = None) -> dict[str, Any]:
        reply = health(self, timeout_s)
        return {**reply, "base": {"checkpoint": reply["base"]["checkpoint"]}}

    monkeypatch.setattr(SocketEngine, "health", older)
    with pytest.raises(EngineError, match=f"The engine at {engine} reports no base id or version in its health"):
        run_worker(owner_config(tmp_path), [engine], [tmp_path], None, "jeff-v1.3", once=True)
