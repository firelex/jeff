import os

import jeff

shop = {"company": "A furniture shop.", "channel": "email"}

message = "My wardrobe arrived with a broken door and I need it replaced before Friday."

with jeff.Jeff.remote("{{origin}}", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:

    team = client.route(message, function="triage.route", context=shop,
                        labels={"deliveries": "Deliveries and damaged goods", "refunds": "Refunds and returns"})

    urgency = client.rate(message, function="triage.urgency", context=shop)

    needs_human = client.requires_escalation(message, function="triage.needs_human", context=shop)

    kind = client.classify(message, function="guard.kind",
                           context={"application": "A support assistant for a furniture shop.", "source": "email body"})

    print("team:", team.value)

    print("urgency level:", urgency.value, "expected level:", round(urgency.score, 2))

    print("a person must act now:", needs_human.value)

    print("kind of text:", kind.value)
