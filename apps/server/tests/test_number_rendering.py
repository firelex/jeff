"""A number in a call means its value, never the way it was written (contract/socket.md, "How the engine writes a
row's numbers for the model"). The hub's refund check sent its amount as 189.0 from Python and as 189 from Node,
Rust, C# and Java, and the model, reading "189.0" in one and "189" in the other, answered 0.848 and 0.818. Here both
bodies go through the real server to an engine that answers from the model text the real engine builds
(jeff_runtime.engine.model.decision_messages), so the answers agree only when the texts do."""

import hashlib
import json
import string
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient

from jeff.testing import FakeEngine
from jeff_runtime.engine.model import decision_messages
from jeff_runtime.engine.types import DecisionInput
from jeff_server.app import create_app
from jeff_server.settings import ServerSettings

ROOT = Path(__file__).resolve().parents[3]
CATALOGUE = ROOT / "catalogue"
CODES = list(string.ascii_uppercase)

# The hub's worked-support-check-refund call (apps/hub/content/examples/worked-support-agent.yaml), with the amount
# left as a placeholder so each body can write it its own way.
BODY = (
    '{"calls": [{"function": "jeff.agents.action-check.action", "input": {"tool": "refund", "arguments": '
    '{"order_id": "4471", "amount": AMOUNT, "card_last4": "3054"}}, "context": {"app": "Brightfield Outfitters, an '
    'online shop for outdoor gear.", "today": "Wednesday 4 November 2026", "rules": ["Refund only the amount the '
    'customer paid for the order."], "people": ["Maya Okafor <maya.okafor@example.com> (the customer, order 4471)"], '
    '"tools": "def refund(order_id: str, amount: float, card_last4: str) -> dict:", "conversation": [{"role": "user", '
    '"content": "Cancel my order #4471 and refund it to my card."}, {"role": "assistant", "content": "Order 4471 '
    '(Ridgeline 2 tent, 189.00, paid with Visa ending 3054) has not shipped yet."}], "latest_user_message": "Cancel '
    'my order #4471 and refund it to my card."}}]}'
)


def test_189_and_189_0_give_the_same_model_text_and_the_same_answer() -> None:
    texts: list[str] = []

    def from_text(adapter: str, row: dict[str, Any]) -> list[float]:
        content = decision_messages(cast(DecisionInput, row), CODES, "state-first")[1]["content"]
        text = str(cast(list[dict[str, Any]], content)[-1]["text"])
        texts.append(text)
        weights = [1 + digest for digest in hashlib.sha256(text.encode()).digest()[:len(row["question"]["criteria"])]]
        return [weight / sum(weights) for weight in weights]

    installed = {path.name for path in CATALOGUE.iterdir() if path.is_dir()} | {"base"}
    settings = ServerSettings(engine_sockets=(Path("/tmp/engine-0.sock"),), model_name="jeff-v1.3",
                              catalogue=CATALOGUE)
    with TestClient(create_app(settings, engine=FakeEngine(from_text, installed))) as client:
        decisions = []
        for amount in ("189.0", "189", "189.00", "1.89e2"):
            response = client.post("/v1/decide", content=BODY.replace("AMOUNT", amount),
                                   headers={"content-type": "application/json"})
            assert response.status_code == 200, response.text
            decision = response.json()["decisions"][0]
            decisions.append((decision["value"], decision["confidence"], decision["probabilities"]))

    assert len(texts) == 4 and len(set(texts)) == 1
    assert '"amount": 189,' in texts[0] and "189.00, paid with Visa" in texts[0]  # text inside strings is kept
    assert all(decision == decisions[0] for decision in decisions)
    assert json.loads(BODY.replace("AMOUNT", "189.0"))["calls"][0]["input"]["arguments"]["amount"] == 189.0
