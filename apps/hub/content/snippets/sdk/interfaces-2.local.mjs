// local
import { Jeff } from "jeff-sdk";

const shop = { company: "A furniture shop.", channel: "email" };

const message = "My wardrobe arrived with a broken door and I need it replaced before Friday.";

const jeff = await Jeff.local("/tmp/jeff.sock", { timeoutMs: 30000 });

const team = await jeff.route(message, { function: "triage.route", context: shop,
  labels: { deliveries: "Deliveries and damaged goods", refunds: "Refunds and returns" } });

const urgency = await jeff.rate(message, { function: "triage.urgency", context: shop });

const needsHuman = await jeff.requiresEscalation(message, { function: "triage.needs_human", context: shop });

const kind = await jeff.classify(message, { function: "guard.kind",
  context: { application: "A support assistant for a furniture shop.", source: "email body" } });

console.log("team:", team.value);

console.log("urgency level:", urgency.value, "expected level:", urgency.score.toFixed(2));

console.log("a person must act now:", needsHuman.value);

console.log("kind of text:", kind.value);

await jeff.close();
