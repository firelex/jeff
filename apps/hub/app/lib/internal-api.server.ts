// The hub's internal API, which jeff-server (apps/server, jeff_server/accounts.py) calls once a minute in accounts
// mode (spec Section 6): GET /api/internal/v1/keys, the list of keys that may be used now, and POST
// /api/internal/v1/usage, its usage totals. server/endpoints/internal.ts serves them. No key, no key hash and never the
// shared secret goes into a log line or an error message here.
import { createHash, timingSafeEqual } from 'node:crypto';
import { Ajv2020 } from 'ajv/dist/2020.js';
import { and, asc, eq, inArray, isNull, sql } from 'drizzle-orm';
import { ApiError } from '../../server/http.js';
import { PLAYGROUND_ACCOUNT } from './account-page.server.js';
import type { InternalSettings } from './accounts-settings.server.js';
import type { Db } from './db.server.js';
import { ACCOUNTS_KEYS_SCHEMA, ACCOUNTS_USAGE_SCHEMA } from './internal-schemas.js';
import { hashKey } from './keys.server.js';
import { apiKey, subscription, usage, user } from './schema.js';
import { USABLE_STATUSES } from './subscription.server.js';
import { isoSeconds, monthStart, nextMonthStart } from './time.js';

const ajv = new Ajv2020({ strict: false, allErrors: false });
const validKeys = ajv.compile(ACCOUNTS_KEYS_SCHEMA);
const validUsage = ajv.compile(ACCOUNTS_USAGE_SCHEMA);

// The hosts a test run serves on, the only ones the internal API answers over plain http, and only outside
// production (the rule of env.server.ts's addressSetting). URL writes ::1 as [::1].
const LOOPBACK_HOSTS = ['127.0.0.1', '[::1]', 'localhost'];

// Rows per statement: well under Postgres's 65,535 parameters per statement (4 per usage row, 2 per key).
const CHUNK = 1000;

function digest(text: string): Buffer {
  return createHash('sha256').update(text, 'utf8').digest();
}

// jeff-server's door into the hub: https only, then the shared secret as a bearer token, compared in constant time
// (both sides hashed first, so their lengths match and the time tells nothing about the secret's length). The 401 is
// the same whatever was wrong: no header, another scheme or another secret.
//
// "https" is X-Forwarded-Proto: https, which Vercel sets itself to the scheme the client used, replacing any value
// the client sent (and it redirects plain http at its edge). request.url cannot say: Vercel's Node adapter builds it
// from the Host header, as http:// for a host without a port (the review of task 15, C1; not measured on a preview,
// task-15-report.md fix round 1). The only other way in is the test rule: plain http to a loopback host (127.0.0.1,
// ::1, localhost) outside production, as scripts/local-server.ts serves the e2e run.
export function checkInternal(request: Request, token: string, production: boolean): void {
  const url = new URL(request.url);
  const overHttps = request.headers.get('x-forwarded-proto') === 'https';
  const loopbackTest = !production && url.protocol === 'http:' && LOOPBACK_HOSTS.includes(url.hostname);
  if (!overHttps && !loopbackTest) {
    throw new ApiError(403, 'HttpsRequired', 'the internal API is served over https only');
  }
  const given = request.headers.get('authorization');
  if (given === null || !timingSafeEqual(digest(given), digest(`Bearer ${token}`))) {
    throw new ApiError(401, 'Unauthorised', 'the internal API needs its shared secret as a bearer token');
  }
}

export interface KeyListBody {
  accounts: { account: string; allowance: number; period_start: string; period_end: string }[];
  keys: { hash: string; account: string }[];
  playground: { hash: string; runs_per_second: number; period_start: string; period_end: string };
}

// GET /api/internal/v1/keys (contract/schemas/accounts_keys.schema.json): every account whose subscription may use
// keys, with its allowance and current Stripe billing period; every live key of those accounts by hash; and the
// playground's key with its total rate. The playground's period is the current UTC calendar month, for counting only.
// The list is checked against its schema before it is answered: jeff-server refuses a list that breaks it, so a bad
// one fails here, at the hub, naming the problem.
export async function keyList(db: Db, settings: Pick<InternalSettings, 'plan' | 'playground'>, now: Date): Promise<KeyListBody> {
  const paying = await db.select().from(subscription).where(inArray(subscription.status, [...USABLE_STATUSES]))
    .orderBy(subscription.userId);
  const keys = await db.select({ hash: apiKey.hash, account: apiKey.userId }).from(apiKey)
    .innerJoin(subscription, eq(subscription.userId, apiKey.userId))
    .where(and(inArray(subscription.status, [...USABLE_STATUSES]), isNull(apiKey.revokedAt))).orderBy(apiKey.hash);
  const body: KeyListBody = {
    accounts: paying.map((row) => ({ account: row.userId, allowance: settings.plan.allowanceInputTokens,
      period_start: isoSeconds(row.currentPeriodStart), period_end: isoSeconds(row.currentPeriodEnd) })),
    keys,
    playground: { hash: hashKey(settings.playground.apiKey), runs_per_second: settings.playground.runsPerSecond,
      period_start: isoSeconds(monthStart(now)), period_end: isoSeconds(nextMonthStart(now)) },
  };
  if (!validKeys(body)) {
    const first = validKeys.errors?.[0];
    throw new Error(`the hub's key list does not match accounts_keys at ${first?.instancePath ?? '(no place given)'}: `
      + `${first?.message ?? 'no detail'}`);
  }
  return body;
}

interface UsageReport {
  accounts: { account: string; period_start: string; input_tokens: number }[];
  keys: { hash: string; last_used: string }[];
}

// What recordUsage did: how many account totals it stored, and how many it left out because their account does not
// exist (deleted, or never made); a total for an unknown key needs no count, it changes nothing.
export interface UsageRecorded { recorded: number; left_out: number }

function refused(message: string): ApiError {
  return new ApiError(422, 'InvalidRequest', `the usage report does not match accounts_usage: ${message}`);
}

// A time the schema's pattern lets through but that is no real time (2026-13-45T99:00:00Z) is refused.
function timeAt(text: string, place: string): Date {
  const when = new Date(text);
  if (Number.isNaN(when.getTime()) || isoSeconds(when) !== text) throw refused(`${place} is not a real UTC time`);
  return when;
}

function chunks<T>(rows: T[]): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < rows.length; i += CHUNK) out.push(rows.slice(i, i + CHUNK));
  return out;
}

// POST /api/internal/v1/usage (contract/schemas/accounts_usage.schema.json). Totals, never deltas: each row keeps the
// greater of what it has and what is reported, so a repeated, late or reordered report does no harm, and a total
// never goes backwards; a report naming the same account and period twice is the same rule applied within it.
//
// A total is stored only for an account that exists (a user, or the playground): jeff-server keeps reporting a
// deleted account's totals, and deleting an account removed its rows (billing.server.ts's deleteAccount), so they
// are left out and counted in the answer. The users' rows are locked (FOR SHARE) while the totals are stored, and
// deleteAccount deletes the user before its usage, so a deletion at the same moment either waits for this report and
// then deletes its rows too, or finishes first and this report finds no user. A key hash the hub does not know (a
// deleted key) is skipped by the update's join.
export async function recordUsage(db: Db, body: unknown, now: Date): Promise<UsageRecorded> {
  if (!validUsage(body)) {
    const first = validUsage.errors?.[0];
    throw refused(first === undefined ? 'no detail' : `${first.instancePath === '' ? 'the body' : first.instancePath} ${first.message}`);
  }
  const report = body as unknown as UsageReport;
  const totals = new Map<string, { account: string; periodStart: Date; tokens: number }>();
  report.accounts.forEach((entry, place) => {
    if (!Number.isSafeInteger(entry.input_tokens)) {
      throw refused(`/accounts/${place}/input_tokens is larger than the hub stores exactly (${Number.MAX_SAFE_INTEGER})`);
    }
    const periodStart = timeAt(entry.period_start, `/accounts/${place}/period_start`);
    const id = `${entry.account}\n${entry.period_start}`;
    const known = totals.get(id);
    if (known === undefined || known.tokens < entry.input_tokens) totals.set(id, { account: entry.account, periodStart, tokens: entry.input_tokens });
  });
  const lastUsed = new Map<string, Date>();
  report.keys.forEach((entry, place) => {
    const when = timeAt(entry.last_used, `/keys/${place}/last_used`);
    const known = lastUsed.get(entry.hash);
    if (known === undefined || known < when) lastUsed.set(entry.hash, when);
  });
  // Sorted, so two reports at once take the rows' locks in the same order (usage rows by the upsert, users and keys
  // by their ordered locking reads).
  const rows = [...totals.entries()].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)).map(([, row]) => row);
  const keys = [...lastUsed.entries()].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return db.transaction(async (tx) => {
    const userIds = [...new Set(rows.map((row) => row.account))].filter((id) => id !== PLAYGROUND_ACCOUNT).sort();
    const existing = new Set<string>([PLAYGROUND_ACCOUNT]);
    for (const ids of chunks(userIds)) {
      const found = await tx.select({ id: user.id }).from(user).where(inArray(user.id, ids)).orderBy(user.id).for('share');
      for (const { id } of found) existing.add(id);
    }
    const kept = rows.filter((row) => existing.has(row.account));
    for (const part of chunks(kept)) {
      await tx.insert(usage).values(part.map((row) => ({ accountId: row.account, periodStart: row.periodStart,
        inputTokens: row.tokens, updatedAt: now })))
        .onConflictDoUpdate({ target: [usage.accountId, usage.periodStart],
          set: { inputTokens: sql`greatest(${usage.inputTokens}, excluded.input_tokens)`, updatedAt: now } });
    }
    // The key rows are locked first, in hash order, by an ordered SELECT ... FOR UPDATE: the UPDATE ... FROM below
    // takes its row locks in whatever order its join gives, so without this two reports at once could deadlock on
    // the same keys. coalesce is SQL's "the stored time, or the reported one when none is stored yet": a key never
    // used has none.
    for (const part of chunks(keys)) {
      await tx.select({ hash: apiKey.hash }).from(apiKey).where(inArray(apiKey.hash, part.map(([hash]) => hash)))
        .orderBy(asc(apiKey.hash)).for('update');
      const values = sql.join(part.map(([hash, when]) => sql`(${hash}, ${when.toISOString()}::timestamptz)`), sql`, `);
      await tx.execute(sql`update ${apiKey} set last_used_at = greatest(coalesce(${apiKey.lastUsedAt}, reported.at), reported.at)
        from (values ${values}) as reported(hash, at) where ${apiKey.hash} = reported.hash`);
    }
    return { recorded: kept.length, left_out: rows.length - kept.length };
  });
}
