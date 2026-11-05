// Latency of one Jeff function through the Node SDK, on this machine:
//   --path node-socket: Jeff.local over the engine socket (--socket)
//   --path node-server: Jeff.remote over HTTP to a jeff-server on this machine (--server-url, and --token or
//                       --no-credentials)
// The Python in-process path is scripts/bench_sdk_python.py: the same function/input/context/labels/calls/
// concurrency/warmup options, and the same statistics, but it cannot share this script's --rounds interleaving
// (it is a separate process with its own in-process model, not another path onto this one engine).
//
// Honesty: `calls` warm-up calls are discarded first (--warmup; a cold connection or a cold model run is not the
// number to publish). round_trip_ms, model_ms, overhead_ms, queue_ms and batch_rows are always measured one call
// at a time (concurrency 1) -- model_ms is the engine's own timing (how long the model itself took), overhead_ms
// is round trip minus model_ms (everything this path adds: binding, the wire, the engine's queue, the server, if
// one is in the path), queue_ms and batch_rows are the engine's scheduler's own numbers (absent, so null, for a
// path with no scheduler, such as Python's in-process mode). throughput_per_s is measured separately, at
// --concurrency concurrent callers. The model's own time drifts between runs (seen on this Mac: 53-81 ms for the
// same call); --rounds measures node-socket and node-server's round trips interleaved, one call to each in turn,
// against the same running engine, so that drift lands on both paths equally and the paths' overhead -- not their
// shared, drifting model time -- is what the comparison shows.
//
// Every run prints one JSON line per path to stdout, and one environment line (machine, backend, engine settings,
// versions) to stderr first.
import { arch, cpus, hostname, platform } from "node:os";
import { readFileSync } from "node:fs";
import { parseArgs } from "node:util";
import { fileURLToPath } from "node:url";
import { Jeff } from "../src/client.js";
import { type MeasureResult, measure, measureThroughput, sampleOf, summariseSamples } from "./stats.js";

// `pnpm --filter jeff-sdk bench -- --path ...` forwards the "--" itself (pnpm, unlike npm, does not consume it),
// so a single leading "--" here is pnpm's own separator, not an argument of this script; drop it before parsing.
const rawArgs = process.argv.slice(2);
const args = rawArgs[0] === "--" ? rawArgs.slice(1) : rawArgs;
const { values } = parseArgs({ args, options: {
  path: { type: "string" }, socket: { type: "string" }, "server-url": { type: "string" }, token: { type: "string" },
  "no-credentials": { type: "boolean" }, function: { type: "string" }, input: { type: "string" },
  context: { type: "string" }, labels: { type: "string" }, calls: { type: "string", default: "300" },
  concurrency: { type: "string" }, warmup: { type: "string", default: "10" }, rounds: { type: "string" },
  "timeout-ms": { type: "string" }, backend: { type: "string" }, "engine-batch-window-ms": { type: "string" },
  "engine-max-batch-rows": { type: "string" }, "engine-prepare-workers": { type: "string" }, help: { type: "boolean" },
} });

if (values.help) {
  console.log("pnpm --filter jeff-sdk bench -- --path node-socket|node-server --function <ref> --input <text> "
    + "[--context <json>] [--labels <json>] [--calls <n>=300] --concurrency <n> [--warmup <n>=10] "
    + "--timeout-ms <ms> (--socket <path> | --server-url <url> (--token <token> | --no-credentials))\n"
    + "  (--socket and --server-url, with --token or --no-credentials, together, and --rounds <n> instead of "
    + "--path): --rounds R runs R rounds of node-socket and node-server interleaved, one call to each in turn "
    + "against the same engine, so model-time drift lands on both equally, before each path's own --concurrency "
    + "throughput run.\n"
    + "  [--backend mlx|torch|fast] [--engine-batch-window-ms <ms>] [--engine-max-batch-rows <rows>] "
    + "[--engine-prepare-workers <n>]: not read by this script -- it never asks the engine or server for its own "
    + "settings -- printed in the environment line exactly as given, so the person who started that engine names "
    + "them.");
  process.exit(0);
}

function required(name: keyof typeof values): string {
  const value = values[name];
  if (typeof value !== "string") throw new Error(`--${name} is required (see --help)`);
  return value;
}

function sdkVersion(): string {
  const packageJsonPath = fileURLToPath(new URL("../package.json", import.meta.url));
  return (JSON.parse(readFileSync(packageJsonPath, "utf8")) as { version: string }).version;
}

/** Printed once per run, to stderr (so stdout stays one JSON line per path): the machine, the backend and engine
 * settings as given on the command line (this script never asks the engine or server for them), and versions. */
function printEnvironment(): void {
  console.error(JSON.stringify({
    machine: { hostname: hostname(), platform: platform(), arch: arch(), cpu: cpus()[0]?.model ?? null },
    node_version: process.version,
    sdk_version: sdkVersion(),
    engine: {
      backend: values.backend ?? null,
      batch_window_ms: values["engine-batch-window-ms"] ?? null,
      max_batch_rows: values["engine-max-batch-rows"] ?? null,
      prepare_workers: values["engine-prepare-workers"] ?? null,
    },
  }));
}

function printNote(throughputConcurrency: number): void {
  console.error(`round_trip_ms, model_ms, overhead_ms, queue_ms and batch_rows are measured at concurrency 1 `
    + `(one caller, one call at a time); throughput_per_s is measured at concurrency ${throughputConcurrency}.`);
}

async function connectSocket(timeoutMs: number): Promise<Jeff> {
  return Jeff.local(required("socket"), { timeoutMs });
}

async function connectServer(timeoutMs: number): Promise<Jeff> {
  if (values["no-credentials"] === (values.token !== undefined)) {
    throw new Error("give exactly one of --token and --no-credentials");
  }
  return Jeff.remote(required("server-url"), values["no-credentials"] ? null : required("token"), { timeoutMs });
}

async function boundFunctionOf(jeff: Jeff): Promise<(input: string) => Promise<{ timing: Readonly<Record<string, unknown>> }>> {
  const bound = await jeff.function(required("function"), {
    context: values.context === undefined ? null : JSON.parse(values.context),
    labels: values.labels === undefined ? null : JSON.parse(values.labels),
  });
  return (input: string) => bound.call(input);
}

/** `counts` is the JSON's own call-count field(s): {calls} in --path mode (round_trip_ms, model_ms etc. and the
 * throughput run all made that many calls), or {rounds, throughput_calls} in --rounds mode, where the interleaved
 * round-trip phase made `rounds` calls to this path and the separate throughput phase after it made
 * `throughput_calls` (--calls) -- two different counts, reported under their own names rather than one "calls"
 * field that would silently mean the throughput phase's count in --path mode and the round-trip phase's count in
 * --rounds mode. */
function printResult(path: string, counts: Record<string, number>, warmup: number, result: MeasureResult): void {
  console.log(JSON.stringify({ path, function: required("function"), ...counts, warmup, ...result }));
}

const timeoutMs = Number(required("timeout-ms"));
const calls = Number(required("calls"));
const warmup = Number(required("warmup"));
const input = required("input");

printEnvironment();

if (values.rounds !== undefined) {
  if (values.path !== undefined) {
    throw new Error("do not give --path with --rounds: interleaved mode always compares node-socket and "
      + "node-server against the same engine");
  }
  const rounds = Number(required("rounds"));
  const concurrency = Number(required("concurrency"));

  const socketJeff = await connectSocket(timeoutMs);
  const serverJeff = await connectServer(timeoutMs);
  const socketCall = await boundFunctionOf(socketJeff);
  const serverCall = await boundFunctionOf(serverJeff);

  for (let index = 0; index < warmup; index += 1) {
    await socketCall(input);
    await serverCall(input);
  }

  const socketSamples = [];
  const serverSamples = [];
  for (let round = 0; round < rounds; round += 1) {
    socketSamples.push(await sampleOf(() => socketCall(input)));
    serverSamples.push(await sampleOf(() => serverCall(input)));
  }

  const socketThroughput = await measureThroughput(() => socketCall(input), calls, concurrency);
  const serverThroughput = await measureThroughput(() => serverCall(input), calls, concurrency);

  printNote(concurrency);
  printResult("node-socket", { rounds, throughput_calls: calls }, warmup, { ...summariseSamples(socketSamples),
    throughput_per_s: socketThroughput, round_trip_concurrency: 1, throughput_concurrency: concurrency });
  printResult("node-server", { rounds, throughput_calls: calls }, warmup, { ...summariseSamples(serverSamples),
    throughput_per_s: serverThroughput, round_trip_concurrency: 1, throughput_concurrency: concurrency });

  await socketJeff.close();
  await serverJeff.close();
} else {
  const path = required("path");
  const concurrency = Number(required("concurrency"));
  let jeff: Jeff;
  if (path === "node-socket") jeff = await connectSocket(timeoutMs);
  else if (path === "node-server") jeff = await connectServer(timeoutMs);
  else throw new Error(`--path must be node-socket or node-server, not ${path}`);

  const call = await boundFunctionOf(jeff);
  const result = await measure(() => call(input), calls, concurrency, warmup);
  printNote(concurrency);
  printResult(path, { calls }, warmup, result);
  await jeff.close();
}
