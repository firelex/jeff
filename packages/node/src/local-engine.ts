/** Local mode with a list of adapters: find the engine running for this user on this machine, or start one through
 * uvx (contract/local-engine.md). The pure parts (platform, folder, uvx, adapters, command line, log tail, messages)
 * are exported for the conformance tests; startOrFind runs the steps in the contract's order. */

import { spawn } from "node:child_process";
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, rmSync, statSync, writeSync, chmodSync, accessSync, constants as fsConstants } from "node:fs";
import { createRequire } from "node:module";
import { arch as osArch, constants as osConstants } from "node:os";
import type { Catalogue } from "./catalogue.js";
import {
  BackendUnavailable, EngineError, InvalidRequest, JeffError, ManifestError, NotInstalled, ProprietaryWeights,
  RuntimeMissing,
} from "./errors.js";
import { pyRepr } from "./pyformat.js";
import { SocketEngine } from "./socket-engine.js";

/** The options of the adapters form; each is a whole number of milliseconds of at least 1. */
export interface StartOptions {
  /** How long to wait for the engine's hello and for each reply (60,000 ms when not given). */
  timeoutMs?: number;
  /** How long everything before the client is ready may take: waiting for another program's start, and starting the
   * engine, whose first start downloads about 2 GB (1,800,000 ms, 30 minutes, when not given). */
  startTimeoutMs?: number;
}

export const DEFAULT_TIMEOUT_MS = 60_000;
export const DEFAULT_START_TIMEOUT_MS = 1_800_000;
/** The longest timeout Node's timers keep (2**31 - 1 ms). */
const MAX_TIMER_MS = 2147483647;
const POLL_MS = 200;
const STALE_UNNAMED_LOCK_MS = 10_000;
const TAIL_LINES = 20;
const EXIT_WHEN_UNUSED_S = 300;

export type Extra = "mac" | "cuda";

/** The machine as local mode sees it; processEnvironment() reads the real one, tests give their own. */
export interface LocalEnvironment {
  /** `macos`, `linux`, `windows` or the language's own name for any other system. */
  os: string;
  /** `arm64`, `x86_64` or the language's own name for any other processor. */
  arch: string;
  home: string | undefined;
  path: string | undefined;
  xdgCacheHome: string | undefined;
  /** Whether /proc/driver/nvidia/version exists (asked only on Linux, before nvidia-smi is looked for on PATH). */
  nvidiaDriver(): boolean;
  /** Whether `path` is a file this process may execute. */
  isExecutable(path: string): boolean;
  /** Writes one line to standard error. */
  stderr(line: string): void;
}

export function osName(platform: string): string {
  return platform === "darwin" ? "macos" : platform === "win32" ? "windows" : platform;
}

export function archName(arch: string): string {
  return arch === "x64" ? "x86_64" : arch;
}

function isExecutableFile(path: string): boolean {
  try {
    if (!statSync(path).isFile()) return false;
    accessSync(path, fsConstants.X_OK);
    return true;
  } catch (error) {
    // no such file, a path through something that is not a folder, or not executable: not this one
    if (["ENOENT", "ENOTDIR", "EACCES"].includes((error as NodeJS.ErrnoException).code ?? "")) return false;
    throw error;
  }
}

export function processEnvironment(): LocalEnvironment {
  return {
    os: osName(process.platform),
    arch: archName(osArch()),
    home: process.env.HOME,
    path: process.env.PATH,
    xdgCacheHome: process.env.XDG_CACHE_HOME,
    nvidiaDriver: () => existsSync("/proc/driver/nvidia/version"),
    isExecutable: isExecutableFile,
    stderr: (line) => { process.stderr.write(`${line}\n`); },
  };
}

/** The version of this package, which pins the engine it starts. */
export function sdkVersion(): string {
  const require = createRequire(import.meta.url);
  return (require("../package.json") as { version: string }).version;
}

// ---- the pure parts (contract/conformance/local_engine.json)

const PLATFORM_PREFIX = "The Jeff engine runs on macOS with Apple silicon, or on Linux with an NVIDIA GPU; ";
const REMOTE = "so local mode cannot start it here. Use a Jeff server instead (remote mode)";

/** The jeff-runtime extra for this machine, or BackendUnavailable. */
export function chooseExtra(os: string, arch: string, nvidia: boolean): Extra {
  let rest: string;
  if (os === "macos") {
    if (arch === "arm64") return "mac";
    rest = arch === "x86_64" ? `this is an Intel Mac (or a program running under Rosetta), ${REMOTE}`
      : `this Mac's processor is ${arch}, ${REMOTE}`;
  } else if (os === "linux") {
    if (nvidia) return "cuda";
    rest = `this Linux machine has no NVIDIA GPU (no /proc/driver/nvidia/version, and no nvidia-smi on PATH), ${REMOTE}`;
  } else if (os === "windows") {
    rest = "the engine cannot run on Windows itself, so local mode cannot start it here. Run your program in WSL 2 on a "
      + "machine with an NVIDIA GPU, or use a Jeff server instead (remote mode)";
  } else {
    rest = `this machine runs ${os}, ${REMOTE}`;
  }
  throw new BackendUnavailable(PLATFORM_PREFIX + rest);
}

/** The first `name` on PATH (split at ':', empty entries skipped) that is an executable file, or null. */
function onPath(name: string, path: string | undefined, isExecutable: (path: string) => boolean): string | null {
  for (const folder of (path ?? "").split(":")) {
    if (!folder.startsWith("/")) continue; // empty and relative entries: never the current folder
    const candidate = `${folder}/${name}`;
    if (isExecutable(candidate)) return candidate;
  }
  return null;
}

/** Whether this Linux machine has an NVIDIA GPU: the driver's file, or nvidia-smi on PATH. */
export function hasNvidia(env: LocalEnvironment): boolean {
  return env.nvidiaDriver() || onPath("nvidia-smi", env.path, env.isExecutable) !== null;
}

export function homeMissing(): EngineError {
  return new EngineError("HOME is not set, so local mode cannot find the folder for the Jeff engine (the user's cache folder)");
}

/** The engine folder: the user's cache folder's jeff/engine. */
export function engineFolder(os: string, home: string | undefined, xdgCacheHome: string | undefined): string {
  if (home === undefined || home === "") throw homeMissing();
  if (os === "macos") return `${home}/Library/Caches/jeff/engine`;
  if (xdgCacheHome !== undefined && xdgCacheHome.startsWith("/")) return `${xdgCacheHome}/jeff/engine`;
  return `${home}/.cache/jeff/engine`;
}

/** The uvx to run: on PATH, then where uv's installer and cargo put it; RuntimeMissing when none is found. */
export function findUvx(path: string | undefined, home: string, isExecutable: (path: string) => boolean): string {
  const found = onPath("uvx", path, isExecutable);
  if (found !== null) return found;
  for (const candidate of [`${home}/.local/bin/uvx`, `${home}/.cargo/bin/uvx`]) {
    if (isExecutable(candidate)) return candidate;
  }
  throw new RuntimeMissing(`Local mode needs uv to start the Jeff engine, and uvx is not on PATH, nor at ${home}/.local/bin/uvx `
    + `or ${home}/.cargo/bin/uvx. Install uv once with: curl -LsSf https://astral.sh/uv/install.sh | sh (see `
    + "https://docs.astral.sh/uv/). Or start an engine yourself (jeff runtime serve) and give its socket instead of the adapters");
}

export function proprietary(adapterId: string): ProprietaryWeights {
  return new ProprietaryWeights(`${adapterId} is proprietary: its weights download only through JeffHub with a licence, so `
    + `local mode cannot start an engine for it. Start an engine yourself (jeff runtime serve --path ${adapterId}=<folder>) `
    + "and give its socket instead of the adapters", { adapter_id: adapterId });
}

export function differentBases(bases: readonly string[]): ManifestError {
  return new ManifestError(`The adapters are trained on different bases (${[...bases].sort().join(", ")}); load one base at a time`);
}

/** The adapters' IDs, each once in the order first named, and their one base (`<id>@<version>`). */
export function resolveAdapters(catalogue: Catalogue, refs: readonly string[]): { ids: string[]; base: string } {
  if (refs.length === 0) throw new ManifestError("local mode needs at least one adapter (none were given)");
  const ids: string[] = [];
  const bases = new Set<string>();
  for (const ref of refs) {
    const manifest = catalogue.adapter(ref);
    if (ids.includes(manifest.id)) continue;
    ids.push(manifest.id);
    bases.add(`${manifest.baseId}@${manifest.baseVersion}`);
  }
  if (bases.size !== 1) throw differentBases([...bases]);
  for (const id of ids) {
    if (!("open" in catalogue.adapter(id).weights)) throw proprietary(id);
  }
  return { ids, base: [...bases][0] as string };
}

/** The argument list that starts the engine (run as is, never through a shell). */
export function engineCommand(uvx: string, extra: Extra, version: string, folder: string, ids: readonly string[],
  base: string): string[] {
  const argv = [uvx, "--from", `jeff-runtime[${extra}]==${version}`, "jeff", "runtime", "serve",
    "--socket", `${folder}/engine.sock`, "--base-version", base];
  for (const id of ids) argv.push("--adapter", id);
  argv.push("--batch-window-ms", "0.1", "--max-batch-rows", "64", "--queue-limit-rows", "4096", "--prepare-workers", "2");
  if (extra === "cuda") argv.push("--max-adapters", String(Math.max(8, ids.length)), "--prefix-pool-mb", "7345");
  argv.push("--exit-when-unused-s", String(EXIT_WHEN_UNUSED_S));
  return argv;
}

/** The log's last 20 lines: split at line feeds, the empty piece after a final line feed dropped. */
export function logTail(text: string): string {
  const pieces = text.split("\n");
  if (pieces.length > 0 && pieces[pieces.length - 1] === "") pieces.pop();
  return pieces.slice(-TAIL_LINES).join("\n");
}

function withLog(head: string, logPath: string, log: string): string {
  if (log === "") return `${head} Its log, ${logPath}, is empty`;
  return `${head} Its log is ${logPath}; its last lines:\n${logTail(log)}`;
}

export function startingLine(ids: readonly string[], logPath: string): string {
  return `Jeff: starting the Jeff engine for ${ids.join(", ")}; the first time, this downloads the engine and the base model `
    + `(about 2 GB, once). Its log: ${logPath}`;
}

export function waitingLine(logPath: string): string {
  return `Jeff: waiting for the Jeff engine that another program is starting. Its log: ${logPath}`;
}

export function exitedError(status: number, logPath: string, log: string): EngineError {
  return new EngineError(withLog(`The Jeff engine exited while starting, with status ${status}.`, logPath, log));
}

export function startTimeoutError(socketPath: string, startTimeoutMs: number, logPath: string, log: string): EngineError {
  return new EngineError(withLog(`The Jeff engine did not answer on ${socketPath} within ${startTimeoutMs} ms, so it was stopped.`,
    logPath, log));
}

export function lockTimeoutError(startTimeoutMs: number, lockPath: string, pid: number | string, logPath: string): EngineError {
  return new EngineError(`Another program has been starting the Jeff engine for longer than ${startTimeoutMs} ms (the lock `
    + `${lockPath}, held by process ${pid}). Its log is ${logPath}`);
}

export function notLoadableError(socketPath: string, missing: readonly string[], errorType: string,
  errorMessage: string): NotInstalled {
  return new NotInstalled(`The Jeff engine already running at ${socketPath} does not serve ${missing.join(", ")}, and refused `
    + `to load them: ${errorType}: ${errorMessage}. Start an engine yourself with every adapter you need (jeff runtime serve) `
    + "and give its socket instead of the adapters; the shared engine stops by itself 5 minutes after the last program "
    + "using it closes", { function_id: missing[0] as string });
}

// ---- the steps

function checkMs(name: string, value: unknown): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < 1) {
    throw new InvalidRequest(`${name} must be a whole number of milliseconds of at least 1; got ${pyRepr(value)}`);
  }
  if (value > MAX_TIMER_MS) {
    throw new InvalidRequest(`${name} must be at most ${MAX_TIMER_MS} milliseconds (about 24.8 days), the longest timeout `
      + `Node keeps; got ${pyRepr(value)}`);
  }
  return value;
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

function readLog(logPath: string): string {
  return existsSync(logPath) ? readFileSync(logPath, "utf8") : "";
}

/** Whether a process with this ID is running (a process of another user counts: kill answers EPERM). */
function alive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "EPERM") return true;
    if ((error as NodeJS.ErrnoException).code === "ESRCH") return false;
    throw error;
  }
}

/** The lock's holder: its process ID (null when the file holds none) and when it was written; null when it is gone. */
function lockHolder(lockPath: string): { pid: number | null; writtenMs: number } | null {
  let text: string;
  let writtenMs: number;
  try {
    writtenMs = statSync(lockPath).mtimeMs;
    text = readFileSync(lockPath, "utf8");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return null; // released between our create and read
    throw error;
  }
  const match = /^(\d+)\n/.exec(text);
  return { pid: match === null ? null : Number(match[1]), writtenMs };
}

/** Takes engine.lock (contract "The start lock"), waiting while a running process holds it, until `deadline`. */
async function takeLock(lockPath: string, logPath: string, deadline: number, startTimeoutMs: number,
  env: LocalEnvironment): Promise<void> {
  let told = false;
  for (;;) {
    let fd: number | null = null;
    try {
      fd = openSync(lockPath, "wx", 0o600);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
    }
    if (fd !== null) {
      try {
        writeSync(fd, `${process.pid}\n`);
      } finally {
        closeSync(fd);
      }
      return;
    }
    const holder = lockHolder(lockPath);
    if (holder === null) continue;
    const held = holder.pid !== null ? alive(holder.pid) : Date.now() - holder.writtenMs <= STALE_UNNAMED_LOCK_MS;
    if (!held) {
      rmSync(lockPath, { force: true });
      continue;
    }
    if (Date.now() >= deadline) throw lockTimeoutError(startTimeoutMs, lockPath, holder.pid ?? "(none written yet)", logPath);
    if (!told) {
      env.stderr(waitingLine(logPath));
      told = true;
    }
    await sleep(Math.min(POLL_MS, Math.max(1, deadline - Date.now())));
  }
}

/** Releases engine.lock if it still holds this process's ID. */
function releaseLock(lockPath: string): void {
  const holder = lockHolder(lockPath);
  if (holder !== null && holder.pid === process.pid) rmSync(lockPath, { force: true });
}

/** Starts the engine (contract "Starting") and waits for it to answer; the connected engine. */
async function start(argv: readonly string[], folder: string, socketPath: string, logPath: string, ids: readonly string[],
  deadline: number, startTimeoutMs: number, timeoutMs: number, env: LocalEnvironment): Promise<SocketEngine> {
  env.stderr(startingLine(ids, logPath));
  const out = openSync(logPath, "w", 0o600); // emptied at each start
  let exited: number | null = null;
  let spawnFailure: Error | null = null;
  let pid: number | undefined;
  try {
    const child = spawn(argv[0] as string, argv.slice(1), { cwd: folder, detached: true, stdio: ["ignore", out, out] });
    pid = child.pid;
    child.on("error", (error) => { spawnFailure = error; });
    child.on("exit", (code, signal) => {
      exited = code ?? 128 + (signal === null ? 0 : osConstants.signals[signal]);
    });
    child.unref(); // the engine outlives this program; nothing here keeps it alive
  } finally {
    closeSync(out);
  }
  for (;;) {
    if (spawnFailure !== null) {
      throw new EngineError(`Local mode could not run ${argv[0]} to start the Jeff engine: ${(spawnFailure as Error).message}`);
    }
    if (exited !== null) throw exitedError(exited, logPath, readLog(logPath));
    const engine = await SocketEngine.connectIfListening(socketPath, { timeoutMs });
    if (engine !== null) return engine;
    if (exited !== null) throw exitedError(exited, logPath, readLog(logPath));
    if (Date.now() >= deadline) {
      if (pid !== undefined) {
        try {
          process.kill(-pid, "SIGTERM"); // the engine's own process group: uvx and the engine it runs
        } catch (error) {
          if ((error as NodeJS.ErrnoException).code !== "ESRCH") throw error; // ESRCH: it has just exited
        }
      }
      throw startTimeoutError(socketPath, startTimeoutMs, logPath, readLog(logPath));
    }
    await sleep(Math.min(POLL_MS, Math.max(1, deadline - Date.now())));
  }
}

/** Asks the engine for the adapters it lacks, with one `serve` keeping every adapter it serves (contract "Sharing"). */
async function ensureServed(engine: SocketEngine, socketPath: string, ids: readonly string[]): Promise<void> {
  const installed = [...(await engine.installed())];
  const missing = ids.filter((id) => !installed.includes(id));
  if (missing.length === 0) return;
  try {
    await engine.serve([...installed.filter((id) => id !== "base"), ...missing]);
  } catch (error) {
    if (error instanceof JeffError) throw notLoadableError(socketPath, missing, error.type, error.message);
    throw error;
  }
}

/** The engine for `adapters`: the one running for this user, or one started now (contract/local-engine.md, steps 1 to 9). */
export async function startOrFind(adapters: readonly string[], options: StartOptions, catalogue: Catalogue,
  env: LocalEnvironment, version: string): Promise<SocketEngine> {
  const began = Date.now();
  const timeoutMs = checkMs("timeoutMs", options.timeoutMs ?? DEFAULT_TIMEOUT_MS);
  const startTimeoutMs = checkMs("startTimeoutMs", options.startTimeoutMs ?? DEFAULT_START_TIMEOUT_MS);
  const deadline = began + startTimeoutMs;
  const { ids, base } = resolveAdapters(catalogue, adapters);
  const extra = chooseExtra(env.os, env.arch, env.os === "linux" && hasNvidia(env));
  const folder = engineFolder(env.os, env.home, env.xdgCacheHome);
  const socketPath = `${folder}/engine.sock`;
  const lockPath = `${folder}/engine.lock`;
  const logPath = `${folder}/engine.log`;

  let engine = await SocketEngine.connectIfListening(socketPath, { timeoutMs });
  if (engine === null) {
    const uvx = findUvx(env.path, env.home as string, env.isExecutable);
    mkdirSync(folder, { recursive: true, mode: 0o700 });
    chmodSync(folder, 0o700);
    await takeLock(lockPath, logPath, deadline, startTimeoutMs, env);
    try {
      engine = await SocketEngine.connectIfListening(socketPath, { timeoutMs });
      if (engine === null) {
        engine = await start(engineCommand(uvx, extra, version, folder, ids, base), folder, socketPath, logPath, ids,
          deadline, startTimeoutMs, timeoutMs, env);
      }
    } finally {
      releaseLock(lockPath);
    }
  }
  try {
    await ensureServed(engine, socketPath, ids);
  } catch (error) {
    await engine.close();
    throw error;
  }
  return engine;
}
