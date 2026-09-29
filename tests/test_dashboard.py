import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jeff import dashboard


def write_lines(path: Path, rows: list[dict], tail: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows) + tail)


def test_complete_lines_skips_unfinished_tail(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    write_lines(path, [{"a": 1}, {"a": 2}], tail='{"a": ')
    assert dashboard.complete_lines(path) == [{"a": 1}, {"a": 2}]


def test_complete_lines_raises_on_bad_terminated_line(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    path.write_text('{"a": 1}\nnot json\n')
    with pytest.raises(ValueError, match="a.jsonl line 2"):
        dashboard.complete_lines(path)


def test_generation_status(tmp_path: Path) -> None:
    out = tmp_path / "gen" / "full"
    out.mkdir(parents=True)
    (out / "run.json").write_text(json.dumps({"slots": 100, "seed": 1, "started": "2026-09-26T00:00:00+00:00"}))
    outcomes = [{"slot": f"s{i}", "family": "sarcasm", "kept": i % 2 == 0, "reason": "kept" if i % 2 == 0 else "ambiguous",
                 "row": None, "time": f"2026-09-26T0{i // 10}:00:00+00:00"} for i in range(40)]
    write_lines(out / "outcomes.jsonl", outcomes)
    result = dashboard.generation_status(out)
    assert result["done"] == 40 and "total" not in result and result["kept"] == 20 and result["updated"] is not None
    assert result["started"] == "2026-09-26T00:00:00+00:00"
    assert result["keep_rate"] == pytest.approx(0.5)
    assert result["by_family"]["sarcasm"] == {"kept": 20, "ambiguous": 20}
    assert result["retries"] == 0


CONFIG = {"base_model": "Qwen/Qwen3.5-2B", "train": "data/mix-a/public.jsonl", "train_rows": 51200, "epochs": 2, "lr": 1e-5,
          "effective_batch_size": 256, "eval_every": 40}


def test_training_status(tmp_path: Path) -> None:
    run = tmp_path / "runs" / "public"
    write_lines(run / "events.jsonl", [{"kind": "training_started", "total_steps": 200, "timestamp": "2026-09-26T00:00:00+00:00"}])
    (run / "config.json").write_text(json.dumps(CONFIG))
    write_lines(run / "training.jsonl", [{"step": s, "loss": 1.0 / s, "learning_rate": 1e-5,
                                         "timestamp": f"2026-09-26T00:{s:02d}:00+00:00"} for s in range(1, 11)])
    write_lines(run / "evaluations.jsonl", [{"step": 10, "raw": {"accuracy": 0.7, "nll": 0.9}, "fitted": {"accuracy": 0.7, "nll": 0.8}, "selected_step": 10}])
    result = dashboard.training_status(run)
    assert result["step"] == 10 and result["total_steps"] == 200
    assert result["loss"] == pytest.approx(0.1)
    assert len(result["losses"]) == 10
    assert result["evaluations"] == [{"step": 10, "accuracy": 0.7, "nll": 0.8, "raw_nll": 0.9}]
    assert result["selected_step"] == 10
    assert result["eta_seconds"] == pytest.approx(190 * 60)
    assert result["finished"] is False
    assert result["model_size"] == "Qwen3.5 2B"
    assert result["started"] == "2026-09-26T00:00:00+00:00" and result["updated"] == "2026-09-26T00:10:00+00:00"
    assert result["recipe"] == {"data": "mix-a/public.jsonl", "train_rows": 51200, "epochs": 2, "peak_learning_rate": 1e-5,
                                "batch": 256, "eval_every": 40}
    assert result["finished_at"] is None and result["early_stopped"] is False


def test_status_reports_section_errors_without_hiding_others(tmp_path: Path) -> None:
    write_lines(tmp_path / "runs" / "public" / "training.jsonl", [], tail="garbage\n")
    write_lines(tmp_path / "runs" / "synthetic" / "training.jsonl", [{"step": 1, "loss": 2.0, "timestamp": "2026-09-26T00:00:00+00:00"}])
    (tmp_path / "runs" / "synthetic" / "config.json").write_text(json.dumps({**CONFIG, "base_model": "Qwen/Qwen3.5-0.8B"}))
    result = dashboard.status(tmp_path, gpu=lambda: {"name": "fake"})
    assert "error" in result["training"]["public"]
    assert result["training"]["synthetic"]["step"] == 1


def test_api_and_page(tmp_path: Path) -> None:
    client = TestClient(dashboard.create_app(tmp_path, gpu=lambda: {"name": "fake"}))
    assert client.get("/api/status").json()["gpu"] == {"name": "fake"}
    page = client.get("/")
    assert page.status_code == 200 and "/api/status" in page.text
    assert "by_family" in page.text


def test_panel_rows_carry_per_benchmark_accuracy_and_published_reference(tmp_path: Path) -> None:
    suites = {name: {"accuracy": 0.5, "count": 10} for name in dashboard.SUITES}
    (tmp_path / "runs" / "eval").mkdir(parents=True)
    (tmp_path / "runs" / "eval" / "public-calibrated.json").write_text(json.dumps(
        {"overall": {"accuracy": 0.6, "ece": 0.1, "brier": 0.4, "nll": 0.7}, "by_suite": suites}))
    (tmp_path / "runs" / "eval" / "public-calibrated.manifest.json").write_text(json.dumps({"model": "Qwen/Qwen3.5-0.8B"}))
    block = {"by_benchmark": {**suites, "JevBench public hard": {"accuracy": 0.0, "count": 99}}}
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "results.json").write_text(json.dumps(
        {"checkpoints": {"0": {"fitted": block}, "200": {"fitted": block}}, "jev": {"metrics": block}}))
    result = dashboard.status(tmp_path, gpu=lambda: {"name": "fake"})
    assert result["panel"]["public-calibrated"]["by_suite"]["BBH"] == 0.5
    assert result["panel"]["public-calibrated"]["model_size"] == "Qwen3.5 0.8B"
    assert result["published"]["Jev (published)"]["accuracy"] == pytest.approx(0.5)  # JevBench excluded
    assert set(result["published"]) == {"AutoJev-27B (published)", "Jev (published)"}


def test_panel_rows_name_the_training_data_of_their_run(tmp_path: Path) -> None:
    suites = {name: {"accuracy": 0.5, "count": 10} for name in dashboard.SUITES}
    for name in ("0.8b-20260927-1630", "0.8b-base"):
        (tmp_path / "runs" / "eval").mkdir(parents=True, exist_ok=True)
        (tmp_path / "runs" / "eval" / f"{name}-calibrated.json").write_text(json.dumps(
            {"overall": {"accuracy": 0.6, "ece": 0.1, "brier": 0.4, "nll": 0.7}, "by_suite": suites}))
        (tmp_path / "runs" / "eval" / f"{name}-calibrated.manifest.json").write_text(json.dumps({"model": "Qwen/Qwen3.5-0.8B"}))
    (tmp_path / "runs" / "0.8b-20260927-1630").mkdir()
    (tmp_path / "runs" / "0.8b-20260927-1630" / "config.json").write_text(json.dumps(CONFIG))
    panel = dashboard.status(tmp_path, gpu=lambda: {"name": "fake"})["panel"]
    assert panel["0.8b-20260927-1630-calibrated"]["data"] == "mix-a/public.jsonl"
    assert panel["0.8b-base-calibrated"]["data"] is None  # untrained: no run


def finished_run(root: Path, name: str) -> None:
    run = root / "runs" / name
    write_lines(run / "events.jsonl", [{"kind": "training_started", "total_steps": 1, "timestamp": "2026-09-26T00:00:00+00:00"},
                                       {"kind": "training_finished", "timestamp": "2026-09-26T00:01:00+00:00"}])
    write_lines(run / "training.jsonl", [{"step": 1, "loss": 1.0, "timestamp": "2026-09-26T00:01:00+00:00"}])
    (run / "config.json").write_text(json.dumps({**CONFIG, "base_model": "Qwen/Qwen3.5-0.8B"}))
    (root / "checkpoints" / name).mkdir(parents=True)


def test_archive_moves_run_and_checkpoint_and_hides_it(tmp_path: Path) -> None:
    finished_run(tmp_path, "sweep-1e-5")
    client = TestClient(dashboard.create_app(tmp_path, gpu=lambda: {"name": "fake"}))
    assert client.post("/api/runs/archive", json={"names": ["sweep-1e-5"]}).json() == {"archived": ["sweep-1e-5"]}
    assert (tmp_path / "runs" / "_archive" / "sweep-1e-5" / "training.jsonl").exists()
    assert not (tmp_path / "checkpoints" / "sweep-1e-5").exists() and not (tmp_path / "checkpoints" / "_archive").exists()
    assert "sweep-1e-5" not in client.get("/api/status").json()["training"]


def test_archive_also_moves_the_runs_panel_scores_but_not_similar_names(tmp_path: Path) -> None:
    finished_run(tmp_path, "public")
    evals = tmp_path / "runs" / "eval"
    evals.mkdir(parents=True)
    for name in ("public-calibrated.json", "public-calibrated.manifest.json", "public-raw.json", "public-all-calibrated.json"):
        (evals / name).write_text("{}")
    dashboard.archive_runs(tmp_path, ["public"])
    assert sorted(p.name for p in (evals / "_archive").iterdir()) == ["public-calibrated.json", "public-calibrated.manifest.json", "public-raw.json"]
    assert (evals / "public-all-calibrated.json").exists()


def test_archive_refuses_active_runs_and_bad_names(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    write_lines(tmp_path / "runs" / "live" / "training.jsonl", [{"step": 1, "loss": 1.0, "timestamp": now}])
    (tmp_path / "runs" / "live" / "config.json").write_text(json.dumps({**CONFIG, "base_model": "Qwen/Qwen3.5-0.8B"}))
    client = TestClient(dashboard.create_app(tmp_path, gpu=lambda: {"name": "fake"}))
    response = client.post("/api/runs/archive", json={"names": ["live"]})
    assert response.status_code == 400 and "still training" in response.json()["detail"]
    for bad in ["../data", "eval", "_archive", "missing"]:
        assert client.post("/api/runs/archive", json={"names": [bad]}).status_code == 400
    assert (tmp_path / "runs" / "live" / "training.jsonl").exists()


def test_model_labels_name_family_and_size_or_fail() -> None:
    assert dashboard.model_size("Qwen/Qwen3.5-0.8B") == "Qwen3.5 0.8B" and dashboard.model_size("Qwen/Qwen3.5-2B") == "Qwen3.5 2B"
    assert dashboard.model_size("answerdotai/ModernBERT-large") == "ModernBERT-large (0.4B)"
    assert dashboard.model_size("google/gemma-4-E2B-it") == "Gemma 4 E2B (2B)"
    assert dashboard.model_size("microsoft/Phi-4-mini-instruct") == "Phi-4-mini (3.8B)"
    with pytest.raises(ValueError, match="display name"):
        dashboard.model_size("some/model")


def test_speed_results_are_read_per_device(tmp_path: Path) -> None:
    folder = tmp_path / "runs" / "latency"
    folder.mkdir(parents=True)
    batch = {"median_ms": 22.1, "p95_ms": 28.0, "mean_ms": 23.0, "min_ms": 20.0, "max_ms": 35.0, "decisions_per_second": 43.7,
             "median_input_tokens_per_batch": 203}
    (folder / "0.8b-gpu.json").write_text(json.dumps({"base_model": "Qwen/Qwen3.5-0.8B", "device": "cuda", "device_name": "RTX",
                                                      "cpu_threads": None, "dtype": "torch.bfloat16", "rows": 200, "batches": {"1": batch}}))
    (folder / "broken.json").write_text("{}")
    speed = dashboard.status(tmp_path, gpu=lambda: {})["speed"]
    assert speed["0.8b-gpu"]["model_size"] == "Qwen3.5 0.8B"
    assert speed["0.8b-gpu"]["batches"] == {"1": {"median_ms": 22.1, "p95_ms": 28.0, "decisions_per_second": 43.7}}
    assert speed["broken"]["error"].startswith("KeyError")


def test_jevbench_scores_are_read_separately_from_the_panel(tmp_path: Path) -> None:
    folder = tmp_path / "runs" / "eval" / "jevbench"
    folder.mkdir(parents=True)
    (folder / "base-calibrated.json").write_text(json.dumps(
        {"overall": {"accuracy": 0.5, "ece": 0.2}, "by_suite": {"JevBench public hard": {"accuracy": 0.5, "count": 105}}}))
    (folder / "base-calibrated.manifest.json").write_text(json.dumps({"model": "Qwen/Qwen3.5-0.8B"}))
    result = dashboard.status(tmp_path, gpu=lambda: {})
    assert result["jevbench"] == {"base-calibrated": {"accuracy": 0.5, "count": 105, "ece": 0.2, "model_size": "Qwen3.5 0.8B"}}
    assert result["panel"] == {}


def test_game_results_name_the_model_wording_and_score(tmp_path: Path) -> None:
    games = tmp_path / "runs" / "games"
    games.mkdir(parents=True)
    (games / "doom-jeff.json").write_text(json.dumps({"game": "doom", "player": "jeff", "criteria": "situation",
        "episodes": [{"score": 6}, {"score": 7}], "mean_score": 6.5, "stdev_score": 0.7, "median_ms_per_decision": 42.0,
        "server": {"checkpoint": "/Users/x/jeff-models/0.8b-20260928-0540"}, "created": "2026-09-28T10:00:00+00:00"}))
    (games / "doom-rule.json").write_text(json.dumps({"game": "doom", "player": "rule", "criteria": None,
        "episodes": [{"score": 6}], "mean_score": 6.0, "stdev_score": 0.0, "median_ms_per_decision": 0.01}))
    games_status = dashboard.status(tmp_path, gpu=lambda: {})["games"]
    assert games_status["doom-jeff"] == {"game": "doom", "player": "Jeff-Qwen3.5-0.8B · 20260928-0540", "wording": "situation", "episodes": 2,
                                        "mean": 6.5, "stdev": 0.7, "ms_per_decision": 42.0, "created": "2026-09-28T10:00:00+00:00",
                                        "note": None, "video": None, "featured": False}
    assert games_status["doom-rule"]["player"] == "rule bot" and games_status["doom-rule"]["wording"] is None


def test_model_names_for_checkpoint_paths() -> None:
    assert dashboard.model_name("checkpoints/2b-20260928-0735/selected") == "Jeff-Qwen3.5-2B · 20260928-0735"
    assert dashboard.model_name("/Users/x/jeff-models/0.8b-20260928-0540") == "Jeff-Qwen3.5-0.8B · 20260928-0540"
    assert dashboard.model_name("checkpoints/untrained-0.8b") == "Qwen3.5 0.8B untrained"


def test_game_videos_are_served_by_name_only(tmp_path: Path) -> None:
    videos = tmp_path / "runs" / "games" / "videos"
    videos.mkdir(parents=True)
    (videos / "doom-x.mp4").write_bytes(b"not really a video")
    client = TestClient(dashboard.create_app(tmp_path, gpu=lambda: {}))
    assert client.get("/videos/doom-x.mp4").content == b"not really a video"
    assert client.get("/videos/missing.mp4").status_code == 404
    assert client.get("/videos/..%2Fsecret.mp4").status_code == 404
