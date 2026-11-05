import pytest

from jeff import Jeff

pytestmark = pytest.mark.slow

TEAMS = {"deliveries": "Deliveries: late, lost or damaged deliveries, and delivery bookings",
         "refunds": "Refunds and payments: refunds, charges and invoices",
         "assembly": "Assembly service: booking and complaints about assembly",
         "account": "Account access: logins, passwords and account details"}
CONTEXT = {"company": "A mid-sized online furniture retailer shipping across Europe.", "channel": "email"}
MESSAGE = ("Subject: Crushed wardrobe\n\nHi, the wardrobe I ordered (order 55120) arrived today and the box was "
           "crushed. Two doors are split. I want a replacement or my money back. This is the second time.\n\nAnna")


def test_local_triage_end_to_end() -> None:
    jeff = Jeff.local(["triage"])
    route = jeff.function("triage.route", context=CONTEXT, labels=TEAMS)
    decision = route(MESSAGE)
    assert decision.value == "deliveries"
    assert decision.timing["path"] == "mlx"
    urgency = jeff.rate(MESSAGE, function="triage.urgency", context=CONTEXT)
    assert isinstance(urgency.value, int) and urgency.score is not None
    assert isinstance(jeff.requires_escalation(MESSAGE, function="triage.needs_human", context=CONTEXT).value, bool)
