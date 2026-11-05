// The playground's live call (POST /api/playground/decide, server/endpoints/playground.ts). The browser sends
// {"sample": <example id>, "input": <the edited input>}; the hub sends one call to PLAYGROUND_API_URL/v1/decide with
// the playground's key and answers with the decision, the latency, the code of that call in every language the site
// shows, and (an open adapter's sample) the same call "On your machine". The context and labels always come from the
// sample, so a visitor cannot send a large context. The run itself always goes through the API, whichever code the
// page is showing.
//
// A signed-in subscriber runs on their own account (X-Jeff-Account: <their id>; jeff-server counts and limits it as
// theirs), with no playground limits. A visitor needs a Turnstile pass, checked once and then kept as a signed cookie
// bound to their address for an hour, and is held to PLAYGROUND_RUNS_PER_MINUTE runs a minute per address, counted in
// Postgres so every function instance shares the count; jeff-server holds all visitors together to
// PLAYGROUND_RUNS_PER_SECOND.
//
// Every refusal is {"refusal": {kind, message, retryAfterS}} in plain words for the visitor. A message never names a
// number of seconds that retryAfterS carries: the page counts those down beside it, and a fixed number would go stale. The Jeff API's own
// errors are never passed on as they came: each is refused as one of these kinds, and nothing in an answer names the
// playground's key or the API's internal address.
import { createHmac, timingSafeEqual } from 'node:crypto';
import { isIP } from 'node:net';
import { and, asc, eq, gt, lt, sql } from 'drizzle-orm';
import { json } from '../../server/http.js';
import type { AccountsSettings } from './accounts-settings.server.js';
import { clientIp } from './client-ip.server.js';
import type { Db } from './db.server.js';
import type { ExampleBlock } from './example-block.js';
import { callCode, localAdaptersFrom, localCallOf, requestBody, type LocalAdapters } from './examples.server.js';
import { LOCAL_ADAPTERS_SETTING } from './function-content.generated.js';
import { playgroundLanguages, sampleById } from './playground-samples.server.js';
import { playgroundRun as runs } from './schema.js';
import { TURNSTILE_HEADER, TurnstileRefused, type VerifyTurnstile } from './turnstile.server.js';

// The catalogue's open adapters, by the name their functions start with, for a run's local call (examples.server.ts:
// localCallOf), from the generated content module -- never the catalogue itself (catalogue.server.ts, site.server.ts
// and catalogue-call.server.ts read files and must stay out of everything the function reaches), built once and kept.
let cachedLocalAdapters: LocalAdapters | null = null;
function localAdaptersOf(): LocalAdapters {
  if (cachedLocalAdapters === null) cachedLocalAdapters = localAdaptersFrom(LOCAL_ADAPTERS_SETTING, 'LOCAL_ADAPTERS_SETTING');
  return cachedLocalAdapters;
}

export const MAX_INPUT_CHARACTERS = 2000;
export const PASS_COOKIE = 'jeff_playground_pass';
export const PASS_SECONDS = 3600;
// How long the hub waits for the Jeff API's answer.
export const API_TIMEOUT_MS = 30_000;
// The window the visitor's runs are counted in; rows older than it count for nothing (sweepPlaygroundRuns).
export const WINDOW_MS = 60_000;
const BODY_FIELDS = ['input', 'sample'];

export interface Refusal {
  kind: 'input' | 'sample' | 'bot-check' | 'per-visitor' | 'busy' | 'api';
  message: string;
  retryAfterS: number | null;
}

// The answer of a run: the decision as the API gave it, the time the call took, and its code (the code, curl and
// local fields of a page's worked example, for the edited input): the same call, "Through the API" or "On your
// machine" (null for a proprietary adapter or a via-http sample, which offer no choice).
export type PlaygroundAnswer = {
  ran_as: 'visitor' | 'account';
  decision: Record<string, unknown>;
  latency_ms: number;
} & Pick<ExampleBlock, 'code' | 'curl' | 'local'>;

export interface PlaygroundDeps {
  db: Db;
  settings: AccountsSettings;
  apiOrigin: string; // JEFF_HUB_API_ORIGIN, the address the code shown calls
  fetchApi: typeof fetch;
  verifyTurnstile: VerifyTurnstile;
  now: () => Date;
  clock: () => number; // milliseconds, for the latency
  // The signed-in user of the request when their subscription may use keys (canUseKeys), else null.
  subscriberOf: (request: Request) => Promise<{ id: string } | null>;
}

function refuse(status: number, refusal: Refusal, headers: Record<string, string> = {}): Response {
  return json(status, { refusal }, refusal.retryAfterS === null ? headers : { ...headers, 'retry-after': String(refusal.retryAfterS) });
}

// Keys for the pass cookie and the address hash, each derived from PLAYGROUND_PASS_SECRET for its one purpose.
function derivedKey(secret: string, purpose: string): Buffer {
  return createHmac('sha256', secret).update(`jeff playground ${purpose}`).digest();
}

// The visitor an address counts as: an IPv4 address itself; an IPv6 address by its /64 (the network one household or
// machine is usually given, so one visitor cannot take a fresh limit with each of its addresses), written as its first
// four groups in full. An IPv4 address written as IPv6 (::ffff:192.0.2.1) is that IPv4 address. `ip` is one valid
// address (clientIp checked it).
export function visitorOf(ip: string): string {
  if (isIP(ip) === 4) return ip;
  if (isIP(ip) !== 6) throw new Error(`visitorOf needs one IP address, not ${JSON.stringify(ip)}`);
  let text = ip.toLowerCase().split('%')[0];
  // A trailing dotted IPv4 part is the last two groups.
  const dotted = /(\d+)\.(\d+)\.(\d+)\.(\d+)$/.exec(text);
  if (dotted !== null) {
    const [a, b, c, d] = dotted.slice(1).map(Number);
    text = `${text.slice(0, dotted.index)}${((a << 8) | b).toString(16)}:${((c << 8) | d).toString(16)}`;
  }
  const halves = text.split('::');
  const front = halves[0] === '' ? [] : halves[0].split(':');
  const back = halves.length === 1 || halves[1] === '' ? [] : halves[1].split(':');
  const groups = (halves.length === 1 ? front : [...front, ...Array<string>(8 - front.length - back.length).fill('0'), ...back])
    .map((group) => Number.parseInt(group, 16));
  if (groups.length !== 8 || groups.some((group) => !Number.isInteger(group))) {
    throw new Error(`visitorOf could not read the IPv6 address ${JSON.stringify(ip)}`);
  }
  if (groups.slice(0, 5).every((group) => group === 0) && groups[5] === 0xffff) {
    return [groups[6] >> 8, groups[6] & 0xff, groups[7] >> 8, groups[7] & 0xff].join('.');
  }
  return `${groups.slice(0, 4).map((group) => group.toString(16).padStart(4, '0')).join(':')}::/64`;
}

// The visitor as the count and the pass know it: a keyed hash of visitorOf, so the database never holds an address.
export function addressHash(secret: string, ip: string): string {
  return createHmac('sha256', derivedKey(secret, 'address')).update(visitorOf(ip)).digest('hex');
}

function signature(secret: string, expires: string, ipHash: string): string {
  return createHmac('sha256', derivedKey(secret, 'pass')).update(`${expires}.${ipHash}`).digest('base64url');
}

function passCookie(secret: string, ipHash: string, now: Date): string {
  const expires = String(Math.floor(now.getTime() / 1000) + PASS_SECONDS);
  return `${PASS_COOKIE}=${expires}.${signature(secret, expires, ipHash)}; HttpOnly; Secure; SameSite=Strict; Path=/api/playground; Max-Age=${PASS_SECONDS}`;
}

// True when the request carries a pass this hub signed for this address that has not expired.
function hasPass(secret: string, cookieHeader: string | null, ipHash: string, now: Date): boolean {
  if (cookieHeader === null) return false;
  const entry = cookieHeader.split(';').map((part) => part.trim()).find((part) => part.startsWith(`${PASS_COOKIE}=`));
  if (entry === undefined) return false;
  const match = /^(\d{1,12})\.([A-Za-z0-9_-]+)$/.exec(entry.slice(PASS_COOKIE.length + 1));
  if (match === null) return false;
  const [, expires, given] = match;
  if (Number(expires) * 1000 <= now.getTime()) return false;
  const expected = Buffer.from(signature(secret, expires, ipHash));
  const sent = Buffer.from(given);
  return expected.length === sent.length && timingSafeEqual(expected, sent);
}

// At most `limit` runs per address in any 60 seconds, across every function instance: the count is in Postgres, and
// an advisory lock per address, the transaction's first statement, makes the count and the insert one step. Only this
// address's old rows are deleted, so runs from different addresses never wait on each other's rows.
export async function admitRun(db: Db, ipHash: string, limit: number, now: Date):
  Promise<{ admitted: true } | { admitted: false; retryAfterS: number }> {
  const windowStart = new Date(now.getTime() - WINDOW_MS);
  return db.transaction(async (tx) => {
    await tx.execute(sql`select pg_advisory_xact_lock(hashtext(${ipHash}))`);
    await tx.delete(runs).where(and(eq(runs.ipHash, ipHash), lt(runs.at, windowStart)));
    const recent = await tx.select({ at: runs.at }).from(runs)
      .where(and(eq(runs.ipHash, ipHash), gt(runs.at, windowStart))).orderBy(asc(runs.at));
    if (recent.length >= limit) {
      const retryAfterS = Math.max(1, Math.ceil((recent[0].at.getTime() + WINDOW_MS - now.getTime()) / 1000));
      return { admitted: false as const, retryAfterS };
    }
    await tx.insert(runs).values({ ipHash, at: now });
    return { admitted: true as const };
  });
}

// Deletes every visitor's runs older than the counting window, by the playground_run_at index: admitRun deletes only
// the old rows of the address it counts, so the rows of an address that never comes back would stay. The internal
// usage endpoint (server/endpoints/internal.ts), which jeff-server calls every minute, runs it. Returns the rows
// deleted.
export async function sweepPlaygroundRuns(db: Db, now: Date): Promise<number> {
  const deleted = await db.delete(runs).where(lt(runs.at, new Date(now.getTime() - WINDOW_MS))).returning({ at: runs.at });
  return deleted.length;
}

// The input's size as the 2,000-character limit counts it: a string's characters (code points, so an emoji is one),
// otherwise its JSON's.
function inputLength(input: unknown): number {
  return [...(typeof input === 'string' ? input : JSON.stringify(input))].length;
}

// The deepest nesting of lists and maps in the input (a plain value is 0), counted no further than `limit` + 1.
function depthOf(value: unknown, limit: number): number {
  if (value === null || typeof value !== 'object') return 0;
  if (limit < 0) return 1;
  let deepest = 0;
  for (const item of Array.isArray(value) ? value : Object.values(value)) {
    deepest = Math.max(deepest, depthOf(item, limit - 1));
    if (deepest > limit) break;
  }
  return deepest + 1;
}

export const MAX_INPUT_DEPTH = 32;
const MAX_SAMPLE_ID_SHOWN = 100;

// pg's own errors for a database it cannot reach, and Postgres's codes for one that refuses connections now.
const UNREACHABLE_CODES = new Set(['ECONNREFUSED', 'ECONNRESET', 'ENOTFOUND', 'ETIMEDOUT', 'EAI_AGAIN', 'EHOSTUNREACH',
  '57P01', '57P03', '53300']);
const UNREACHABLE_MESSAGES = [/timeout exceeded when trying to connect/i, /Connection terminated/i];

// True when `error`, or an error it was caused by, says the database cannot be reached (Better Auth wraps the errors
// of its own queries).
export function isDatabaseUnreachable(error: unknown): boolean {
  for (let e = error, depth = 0; e instanceof Error && depth < 10; e = e.cause, depth += 1) {
    const code = (e as { code?: unknown }).code;
    if (typeof code === 'string' && UNREACHABLE_CODES.has(code)) return true;
    if (e instanceof AggregateError && e.errors.some((inner) => isDatabaseUnreachable(inner))) return true;
    if (UNREACHABLE_MESSAGES.some((pattern) => pattern.test(e.message))) return true;
  }
  return false;
}

// `work`'s result, or unreachable (logged) when the database cannot be reached, which playgroundRun answers with a
// 503 busy refusal; any other error is left to fail the request. A full connection pool looks the same as a database
// that cannot be reached (pg's connection timeout), so the log and the refusal say "busy or unreachable".
async function withDatabase<T>(what: string, work: () => Promise<T>): Promise<{ value: T } | 'unreachable'> {
  try {
    return { value: await work() };
  } catch (error) {
    if (!isDatabaseUnreachable(error)) throw error;
    console.error(`playground: the database was busy or unreachable while ${what}: ${(error as Error).message}`);
    return 'unreachable';
  }
}

export const DATABASE_RETRY_S = 60;
const DATABASE_BUSY: Refusal = { kind: 'busy', retryAfterS: DATABASE_RETRY_S,
  message: 'The playground\'s database is busy or unreachable right now; try again shortly.' };

const characters = (n: number) => n.toLocaleString('en-US');

// The seconds of a Retry-After header, or null when it sent none that is a number of seconds above 0.
function retryAfterOf(headers: Headers): number | null {
  const text = headers.get('retry-after');
  if (text === null) return null;
  const seconds = Number(text);
  return Number.isFinite(seconds) && seconds > 0 ? Math.ceil(seconds) : null;
}

// The Jeff API's error answer, refused in the visitor's words (contract/errors.yaml names the types).
function apiRefusal(status: number, type: string, message: string, retryAfterS: number | null, asAccount: boolean): [number, Refusal] {
  const busy: Refusal = { kind: 'busy', message: 'The playground is busy; try again in a few seconds.', retryAfterS };
  switch (type) {
    case 'RateLimited':
      if (!asAccount) return [503, busy];
      return [429, { kind: 'api', retryAfterS,
        message: retryAfterS === null ? 'Your account is making calls faster than its rate limit allows; try again in a few seconds.'
          : 'Your account is making calls faster than its rate limit allows.' }];
    case 'Busy':
    case 'NotReady':
    case 'BackendUnavailable':
      return [503, busy];
    case 'UsageLimitReached':
      // jeff-server writes this one for the customer (the allowance and when calls resume).
      return [402, { kind: 'api', message, retryAfterS: null }];
    case 'InputTooLong':
      return [422, { kind: 'input', message: 'The input is longer than the model can read; shorten it and run it again.', retryAfterS: null }];
    case 'InvalidRequest':
      return [422, { kind: 'input', message: `The API could not run this input: ${message}`, retryAfterS: null }];
    case 'DeadlineExceeded':
    case 'EscalationBudgetExceeded':
      return [504, { kind: 'api', message: 'The API took too long to decide; try again.', retryAfterS: null }];
    case 'Unauthorised':
    case 'NotLicensed':
      return [502, { kind: 'api', retryAfterS: null, message: asAccount
        ? 'The API did not accept the playground\'s call for your account; if you subscribed in the last minute, try again shortly.'
        : 'The API did not accept the playground\'s call; this is a fault on our side, not in your input.' }];
    default:
      return [status >= 500 ? 502 : status, { kind: 'api', retryAfterS: null,
        message: `The API refused the call (${type}); try another input, or try again later.` }];
  }
}

export async function playgroundRun(request: Request, deps: PlaygroundDeps): Promise<Response> {
  const text = await request.text();
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
    return refuse(422, { kind: 'input', message: 'The request body is not JSON.', retryAfterS: null });
  }
  if (body === null || typeof body !== 'object' || Array.isArray(body)) {
    return refuse(422, { kind: 'input', message: 'Send {"sample": <sample id>, "input": <the input>}.', retryAfterS: null });
  }
  const fields = body as Record<string, unknown>;
  const extra = Object.keys(fields).filter((name) => !BODY_FIELDS.includes(name));
  if (extra.length > 0) {
    return refuse(422, { kind: 'input', retryAfterS: null,
      message: `The playground takes only a sample and an input (the context and labels come from the sample); remove ${extra.join(', ')}.` });
  }
  const { sample: sampleId, input } = fields;
  if (typeof sampleId !== 'string') return refuse(422, { kind: 'input', message: 'Choose a sample to run.', retryAfterS: null });
  const sample = sampleById(sampleId);
  if (sample === null) {
    const shown = [...sampleId].length > MAX_SAMPLE_ID_SHOWN ? `${[...sampleId].slice(0, MAX_SAMPLE_ID_SHOWN).join('')}…` : sampleId;
    return refuse(404, { kind: 'sample', message: `There is no sample ${JSON.stringify(shown)}.`, retryAfterS: null });
  }
  if (input === undefined || input === null || input === '') return refuse(422, { kind: 'input', message: 'Give an input to run.', retryAfterS: null });
  if (depthOf(input, MAX_INPUT_DEPTH) > MAX_INPUT_DEPTH) {
    return refuse(422, { kind: 'input', retryAfterS: null,
      message: `The input nests lists and maps more than ${MAX_INPUT_DEPTH} levels deep; the playground takes at most ${MAX_INPUT_DEPTH}.` });
  }
  const length = inputLength(input);
  if (length > MAX_INPUT_CHARACTERS) {
    return refuse(422, { kind: 'input', retryAfterS: null,
      message: `The input is ${characters(length)} characters; the playground takes at most ${characters(MAX_INPUT_CHARACTERS)}.` });
  }

  const now = deps.now();
  const secret = deps.settings.playground.passSecret;
  const headers: Record<string, string> = {};
  const signedIn = await withDatabase('reading the session', () => deps.subscriberOf(request));
  if (signedIn === 'unreachable') return refuse(503, DATABASE_BUSY);
  const subscriber = signedIn.value;
  if (subscriber === null) {
    const ip = clientIp(request.headers);
    const ipHash = addressHash(secret, ip);
    if (!hasPass(secret, request.headers.get('cookie'), ipHash, now)) {
      try {
        await deps.verifyTurnstile(request.headers.get(TURNSTILE_HEADER), ip);
      } catch (error) {
        if (!(error instanceof TurnstileRefused)) throw error;
        // Cloudflare's reasons are for the owner's log, not the visitor.
        console.error(`playground: the bot check refused a visitor: ${error.message}`);
        return refuse(403, { kind: 'bot-check', message: 'Complete the bot check above the Run button, then run again.', retryAfterS: null });
      }
      headers['set-cookie'] = passCookie(secret, ipHash, now);
    }
    const limit = deps.settings.playground.runsPerMinute;
    const counted = await withDatabase('counting the visitor\'s runs', () => admitRun(deps.db, ipHash, limit, now));
    if (counted === 'unreachable') return refuse(503, DATABASE_BUSY, headers);
    const admitted = counted.value;
    if (!admitted.admitted) {
      return refuse(429, { kind: 'per-visitor', retryAfterS: admitted.retryAfterS,
        message: `You can run ${limit} a minute, and this minute's runs are used up.` }, headers);
    }
  }

  const example = { ...sample.example, input: input as typeof sample.example.input };
  const apiHeaders: Record<string, string> = { 'content-type': 'application/json',
    authorization: `Bearer ${deps.settings.playground.apiKey}` };
  if (subscriber !== null) apiHeaders['x-jeff-account'] = subscriber.id;
  const started = deps.clock();
  let answer: Response;
  let answerText: string;
  try {
    answer = await deps.fetchApi(`${deps.settings.playground.apiUrl}/v1/decide`, { method: 'POST', headers: apiHeaders,
      body: JSON.stringify(requestBody(example)), redirect: 'error', signal: AbortSignal.timeout(API_TIMEOUT_MS) });
    answerText = await answer.text();
  } catch (error) {
    // Logged for the owner; the visitor is told in plain words, with nothing of the API's address in them.
    console.error(`playground: the Jeff API could not be reached: ${(error as Error).name}: ${(error as Error).message}`);
    if ((error as Error).name === 'TimeoutError') {
      return refuse(504, { kind: 'api', message: `The API did not answer within ${API_TIMEOUT_MS / 1000} seconds; try again.`, retryAfterS: null }, headers);
    }
    return refuse(502, { kind: 'api', message: 'The API could not be reached; try again in a minute.', retryAfterS: null }, headers);
  }
  const latency = Math.round((deps.clock() - started) * 10) / 10;
  let parsed: unknown;
  try {
    parsed = JSON.parse(answerText);
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
    console.error(`playground: the Jeff API answered ${answer.status} without a JSON body`);
    return refuse(502, { kind: 'api', message: 'The API gave an answer the playground cannot read; try again later.', retryAfterS: null }, headers);
  }
  if (!answer.ok) {
    const error = (parsed as { error?: { type?: unknown; message?: unknown } } | null)?.error;
    if (error === undefined || error === null || typeof error.type !== 'string' || typeof error.message !== 'string') {
      console.error(`playground: the Jeff API answered ${answer.status} without a Jeff error body`);
      return refuse(502, { kind: 'api', message: 'The API gave an answer the playground cannot read; try again later.', retryAfterS: null }, headers);
    }
    if (error.type === 'Unauthorised' || error.type === 'NotLicensed') {
      console.error(`playground: the Jeff API refused the playground's key (${error.type}, ${subscriber === null ? 'a visitor' : 'a subscriber'})`);
    }
    const [status, refusal] = apiRefusal(answer.status, error.type, error.message, retryAfterOf(answer.headers), subscriber !== null);
    return refuse(status, refusal, headers);
  }
  const decisions = (parsed as { decisions?: unknown } | null)?.decisions;
  if (!Array.isArray(decisions) || decisions.length !== 1 || decisions[0] === null || typeof decisions[0] !== 'object') {
    console.error('playground: the Jeff API answered without exactly one decision');
    return refuse(502, { kind: 'api', message: 'The API gave an answer the playground cannot read; try again later.', retryAfterS: null }, headers);
  }
  const ran: PlaygroundAnswer = {
    ran_as: subscriber === null ? 'visitor' : 'account',
    decision: decisions[0] as Record<string, unknown>,
    latency_ms: latency,
    ...callCode(example, deps.apiOrigin, playgroundLanguages()),
    local: localCallOf(example, playgroundLanguages(), localAdaptersOf()),
  };
  return json(200, ran, headers);
}
