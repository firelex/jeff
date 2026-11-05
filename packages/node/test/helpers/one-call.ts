/** Run as its own Node process: makes one predict over the engine socket at argv[2], prints its probabilities and
 * never closes the engine, so the process ends only once nothing keeps it alive. */
import { SocketEngine } from "../../src/socket-engine.js";

const engine = await SocketEngine.connect(process.argv[2] as string, { timeoutMs: 5000 });
const result = await engine.predict("jeff.safety.spam", [{ state: "x", question: { type: "noul", instructions: "?" } }]);
console.log(JSON.stringify(result.probabilities));
