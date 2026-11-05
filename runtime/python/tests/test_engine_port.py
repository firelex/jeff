from jeff_runtime.engine.model import decision_messages, options


def test_live_last_prompt_is_unchanged() -> None:
    row = {"state": {"company": "Shop", "message": "Broken"},
           "question": {"type": "choice", "instructions": "Which team?", "criteria": {"other": "None", "k1": "Deliveries"}}}
    messages = decision_messages(row, ["A", "B"], "live-last")
    text = messages[1]["content"][-1]["text"]
    assert text.startswith("Question:\nWhich team?\n\nState:\n{\"company\": \"Shop\"}\n\nOptions:\nA: other: None\nB: k1: Deliveries")
    assert text.endswith("Latest:\n{\"message\": \"Broken\"}\n\nReturn only the letter code of the best option.")


def test_noul_options_are_false_then_true() -> None:
    assert options({"type": "noul"})[0] == ["false", "true"]
