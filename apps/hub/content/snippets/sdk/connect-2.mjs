import { Jeff } from "jeff-sdk";

// Called before every request, so a token refreshed here (read again from the environment, or fetched from your
// secret store) is used from the next request on. It may be async.
async function currentToken() {
  return process.env.JEFF_API_KEY;
}

const jeff = await Jeff.remote("{{origin}}", () => currentToken(), { timeoutMs: 30000 });

const guard = await jeff.function("guard.attempt", {
  context: { application: "A support chat assistant for an online shop.", source: "user message" },
});

console.log((await guard.call("What are your opening hours?")).value);

await jeff.close();
