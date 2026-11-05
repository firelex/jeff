"""Helpers shared by the factory's tests (ported from jeff-server's tests/test_factory.py): a task spec for trading
desk messages, a teacher that answers the factory's prompts offline, and data built from them."""

import json
import re
from pathlib import Path
from typing import Any

from jeff_server.factory import checks, generate
from jeff_server.factory.config import FactoryConfig
from jeff_server.factory.runner import WorkerHooks
from jeff_server.factory.store import Run, new_run, spec_hash, write_json, write_jsonl
from jeff_server.factory.teacher import InvalidReply, Usage


def trading_spec() -> dict[str, Any]:
    return {
        "name": "trading-calls", "summary": "Which trading function a desk chat message calls.",
        "family": {"noun": "trading desk", "description": "One desk of traders.",
                   "fields": [{"name": "desk", "description": "The desk's name"},
                              {"name": "asset_classes", "description": "What the desk may trade"}]},
        "live_field": {"name": "request", "description": "The trader's message"},
        "questions": [
            {"id": "function", "type": "choice", "instruction": "Which function does this request call?",
             "options": [{"key": "place_order", "description": "Buy or sell"}, {"key": "cancel_order", "description": "Cancel"},
                         {"key": "none", "description": "Not a trading instruction"}], "applies_when": None},
            {"id": "side", "type": "choice", "instruction": "Which side?",
             "options": [{"key": "buy", "description": "Buy"}, {"key": "sell", "description": "Sell"}],
             "applies_when": "only when the function is place_order", "requires": [{"question": "function", "equals": "place_order"}]},
            {"id": "urgent", "type": "noul", "instruction": "Is it urgent?",
             "options": [{"key": "true", "description": "Urgent"}, {"key": "false", "description": "Not urgent"}],
             "applies_when": None},
        ],
        "hard_cases": [{"kind": "two_actions", "description": "Two actions; the last one counts."}],
        "styles": ["terse", "formal"],
        "sizes": {"train": 100, "updates": 50, "dev": 50, "calibration": 50, "test": 50, "families": 20},
    }


class FakeTeacher:
    """Answers the factory's prompts by the request name, the way the real teacher's JSON would look."""

    def __init__(self) -> None:
        self.usage = Usage()
        self.counter = 0
        self.bad_requests = 0

    def ask(self, prompt: str, schema: dict[str, Any], name: str, check: Any = None, max_tokens: int = 0) -> tuple[Any, str]:
        self.usage.add(calls=1)
        self.counter += 1
        if name == "families":
            count = int(re.search(r"Invent (\d+)", prompt).group(1))
            value: Any = {"families": [{"label": f"Desk {self.counter}-{i}", "profile": "a desk", "desk": f"Desk {self.counter}-{i}",
                                        "asset_classes": "equity"} for i in range(count)]}
        elif name == "rewordings":
            value = {"rewordings": [f"Rewording {self.counter}-{i}" for i in range(35)]}
        elif name == "rewording_check":
            count = len(re.findall(r"^\d+\. ", prompt, flags=re.M))
            value = {"verdicts": [i % 10 != 0 for i in range(count)]}
        elif name == "requests":
            target = re.search(r'must be "([^"]+)"', prompt).group(1)
            if self.bad_requests:
                self.bad_requests -= 1
                raise InvalidReply("bad reply")
            value = {"requests": [{"text": f"message {self.counter} number {i} about {target}",
                                   "answers": {"function": target, "side": "buy" if target == "place_order" else None,
                                               "urgent": "true" if i % 2 else "false"}} for i in range(10)]}
        else:
            raise AssertionError(name)
        if check is not None:
            check(value)
        return value, "fake-teacher-1"


def build(spec: dict[str, Any], teacher: FakeTeacher) -> tuple[list[dict[str, Any]], dict[str, list[str]], list[Any], list[Any]]:
    families = generate.make_families(teacher, spec, spec["sizes"]["families"])
    pools = {q["id"]: generate.make_pool(teacher, q) for q in spec["questions"]}
    plan = generate.plan_splits(spec, families, 1)
    lines: list[dict[str, Any]] = []
    scenarios, failures = generate.generate_scenarios(teacher, spec, families, plan, 1, lines.append)
    rows = generate.make_rows(spec, scenarios, pools, "hash", "run", 1)
    return rows, plan, failures, lines


def label_key(row: dict[str, Any]) -> str:
    """A row's request and question, without the question's wording or option order: training rows reword the
    instruction and shuffle the options, while publishing scores the canonical question (as Jeff sends it)."""
    question = row["question"]
    return json.dumps([row["state"], question["type"], sorted(question.get("criteria", []))], sort_keys=True)


def fake_scorer(labels: dict[str, Any]) -> Any:
    """A scorer whose logits pick each row's labelled option (looked up in `labels`, by label_key), as a
    well-trained adapter would."""
    def score(base: str, adapter: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
        logits = []
        for row in rows:
            options = list(row["question"]["criteria"]) if row["question"]["type"] == "choice" else ["false", "true"]
            label = labels[label_key(row)]
            key = label if isinstance(label, str) else str(label).lower()
            logits.append([4.0 if option == key else 0.0 for option in options])
        return {"logits": logits, "seconds": 0.5}
    return score


def trained_run(config: FactoryConfig, spec: dict[str, Any]) -> tuple[Run, dict[str, Any]]:
    """An approved run whose data was generated (FakeTeacher) and checked and whose trainer has written its adapter
    (decision_config.json, adapter_config.json and a weights file): ready for calibrate, evaluate and publish, with
    a gate of 0.5. Returns the run and each row's label by label_key, for fake_scorer."""
    rows, _, _, _ = build(spec, FakeTeacher())
    files, report = checks.run_checks(spec, rows, 1, lambda row: 1)
    run = new_run(config.runs, spec["name"], "describe", None)
    write_json(run.folder / "spec.json", spec)
    labels: dict[str, Any] = {}
    for split, split_rows in files.items():
        write_jsonl(run.data / f"{split}.jsonl", split_rows)
        labels.update({label_key(row): row["label"] for row in split_rows})
    write_json(run.folder / "checks.json", report)
    adapter = run.folder / "training" / "checkpoints" / "final"
    adapter.mkdir(parents=True)
    write_json(adapter / "decision_config.json", {"temperature": 1.0, "max_options": 26,
                                                  "adapter": {"base_checkpoint": "/models/jeff-base"}})
    write_json(adapter / "adapter_config.json", {"r": 16})
    (adapter / "adapter_model.safetensors").write_bytes(b"weights")
    write_json(run.folder / "train.json", {"adapter": str(adapter), "trainer_commit": "abc123"})
    for step in ("data_prep", "generate", "checks", "train"):
        run.set_step(step, status="done")
    run.update(approved="2026-10-23T10:00:00.000+00:00", spec_sha256=spec_hash(spec), gate={"min_accuracy": 0.5})
    return run, labels


def worker_hooks(root: Path, engines: tuple[Path, ...], score: Any, catalogue: Path | None = None) -> WorkerHooks:
    """The worker's hooks over `engines`, publishing into root/adapters (made here), on base jeff-base v1.3."""
    (root / "adapters").mkdir(exist_ok=True)
    return WorkerHooks(base=str(root), base_id="jeff-base", base_version="v1.3", model_name="jeff-v1.3",
                       adapter_dirs=(root / "adapters",), catalogue=catalogue, engines=engines, score=score)
