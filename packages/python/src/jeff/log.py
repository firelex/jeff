"""The decision log (off unless you pass one): one JSON line per decision, the seam for the learning loop."""

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jeff.client import Call
    from jeff.decision import Decision


class DecisionLog:
    def __init__(self, path: Path, include_input: bool = False) -> None:
        self.path, self.include_input = Path(path), include_input
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, call: "Call", decision: "Decision") -> None:
        labels = dict(call.labels) if isinstance(call.labels, Mapping) else list(call.labels or [])
        key = json.dumps({"input": call.input, "context": dict(call.context or {}), "labels": labels},
                         sort_keys=True, ensure_ascii=False)
        data = decision.to_json()
        line: dict[str, Any] = {
            "time": datetime.now(timezone.utc).isoformat(), "function": decision.function, "version": decision.version,
            "input_sha256": hashlib.sha256(key.encode()).hexdigest(),
            "value": decision.value, "confidence": decision.confidence, "abstained": decision.abstained,
            "calibrated": decision.calibrated, "route": data["route"], "escalations": data["escalations"],
            "decided_by": decision.decided_by,
        }
        if self.include_input:
            line["input"] = call.input
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
