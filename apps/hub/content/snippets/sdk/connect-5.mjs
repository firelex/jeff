// offline
import { InvalidRequest, Jeff } from "jeff-sdk";

// Both mistakes are refused at once, before any network call or connection.
try {
  await Jeff.remote("{{origin}}", null, { timeoutMs: 0 });
} catch (error) {
  if (!(error instanceof InvalidRequest)) throw error;
  console.log(error.message);
}

try {
  await Jeff.local("/tmp/jeff.sock", { timeoutMs: 0 });
} catch (error) {
  if (!(error instanceof InvalidRequest)) throw error;
  console.log(error.message);
}
