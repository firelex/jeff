// @vitest-environment node
// Adapter requests' examples are deleted 90 days after the request was closed (app/lib/adapter-requests.server.ts:
// clearExpiredExamples), by the daily job Vercel's cron calls (server/endpoints/cron.ts), against a real database (the
// migrations, in PGlite) and a stand-in for the file store: when a request counts as closed, what is deleted and what
// stays, a second run, a zip that cannot be deleted, the job's secret, the owner's export after the deletion, and the
// migration's closed_at for requests closed before it.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { PGlite } from '@electric-sql/pglite';
import { eq } from 'drizzle-orm';
import { drizzle } from 'drizzle-orm/pglite';
import { migrate } from 'drizzle-orm/pglite/migrator';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { RequestStatus } from '../app/lib/adapter-request';
import { clearExpiredExamples, setStatus } from '../app/lib/adapter-requests.server';
import type { DataSetStore, ListedBlob } from '../app/lib/data-set-store.server';
import type { Db } from '../app/lib/db.server';
import { adapterRequest } from '../app/lib/schema';
import * as schema from '../app/lib/schema';
import { serveEndpoint } from '../server/endpoint';
import { adapterRequestEndpoints } from '../server/endpoints/adapter-requests';
import { CLEAR_EXAMPLES_PATH, cronEndpoints } from '../server/endpoints/cron';
import { endpointFor } from '../server/router';
import { aDataSetForm, aForm } from './helpers/adapter-request';
import { aUser, NOW } from './helpers/accounts';
import { testDb } from './helpers/db';
import { TEST_ENV, testSettings } from './helpers/settings';

beforeEach(() => {
  for (const [name, value] of Object.entries(TEST_ENV)) vi.stubEnv(name, value);
  // The job needs the file store's token (the stand-in below never uses it).
  vi.stubEnv('BLOB_READ_WRITE_TOKEN', 'vercel_blob_rw_teststore_notarealsecret');
});
afterEach(() => { vi.unstubAllEnvs(); vi.restoreAllMocks(); });

const DAY_MS = 24 * 60 * 60 * 1000;
const daysBefore = (days: number, from = NOW) => new Date(from.getTime() - days * DAY_MS);
const BASE = 'https://jeffhub.ai';

// A stand-in for the private file store: the zips it holds, what was deleted, and pathnames whose deletion fails.
function fakeStore(failFor: string[] = [], listed: ListedBlob[] = []) {
  const removed: string[] = [];
  const store: DataSetStore = {
    list: async (prefix) => listed.filter((b) => b.pathname.startsWith(prefix) && !removed.includes(b.pathname)),
    readWriteToken: () => 'vercel_blob_rw_teststore_notarealsecret',
    find: async () => null,
    remove: async (pathnames) => {
      for (const p of pathnames) {
        if (failFor.includes(p)) throw new Error(`Vercel Blob: the store did not delete ${p}: status 503`);
        removed.push(p);
      }
    },
    downloadUrl: async (pathname) => `https://teststore.private.blob.vercel-storage.com/${pathname}?signed=1`,
  };
  return { store, removed };
}

// A request of u1's, sent long ago, with typed examples or a data set (stored at its own pathname).
async function aRequest(db: Db, id: string, source: 'generate' | 'data_set' = 'generate') {
  const form = source === 'generate' ? aForm() : aDataSetForm();
  const { examples, dataSet, rights, source: _source, ...stored } = form;
  const pathname = dataSet === null ? null : `adapter-requests/u1/data-set-${id}.zip`;
  const sent = daysBefore(400);
  await db.insert(adapterRequest).values({
    id, userId: 'u1', status: 'received', form: stored, exampleSource: source, examples,
    dataSetPathname: pathname, dataSetUrl: pathname === null ? null : `https://teststore.private.blob.vercel-storage.com/${pathname}`,
    dataSetBytes: dataSet?.bytes ?? null, dataSetRows: dataSet?.rows ?? null, dataSetCounts: dataSet?.counts ?? null,
    mayShare: rights.mayShare, noPersonalData: rights.noPersonalData, mayImproveBase: rights.mayImproveBase,
    createdAt: sent, updatedAt: sent, declineReason: null,
  });
  return pathname;
}

const moveTo = (db: Db, id: string, status: RequestStatus, at: Date) =>
  setStatus(db, id, status, status === 'declined' ? 'Not something we can train.' : null, at);

async function rowOf(db: Db, id: string) {
  const [row] = await db.select().from(adapterRequest).where(eq(adapterRequest.id, id));
  return row;
}

async function withUser() {
  const db = await testDb();
  await aUser(db, 'u1');
  await aUser(db, 'owner', 'owner@example.com');
  return db;
}

describe('closing a request', () => {
  it('records when it was delivered or declined, keeps the first time while it stays closed, and clears it when it moves back', async () => {
    const db = await withUser();
    await aRequest(db, 'AR-1');
    expect((await rowOf(db, 'AR-1')).closedAt).toBeNull();
    await moveTo(db, 'AR-1', 'in_review', daysBefore(30));
    expect((await rowOf(db, 'AR-1')).closedAt).toBeNull();
    await moveTo(db, 'AR-1', 'delivered', daysBefore(20));
    expect((await rowOf(db, 'AR-1')).closedAt).toEqual(daysBefore(20));
    await moveTo(db, 'AR-1', 'declined', daysBefore(10));
    expect((await rowOf(db, 'AR-1')).closedAt).toEqual(daysBefore(20));
    await moveTo(db, 'AR-1', 'training', daysBefore(5));
    expect((await rowOf(db, 'AR-1')).closedAt).toBeNull();
    await moveTo(db, 'AR-1', 'delivered', daysBefore(1));
    expect((await rowOf(db, 'AR-1')).closedAt).toEqual(daysBefore(1));
  });

  it('the database refuses a closed request without the time, and an open one with it', async () => {
    const db = await withUser();
    await aRequest(db, 'AR-1');
    const refusal = (change: Partial<typeof adapterRequest.$inferInsert>) =>
      db.update(adapterRequest).set(change).where(eq(adapterRequest.id, 'AR-1')).then(() => null, (error: Error) => (error.cause as Error).message);
    expect(await refusal({ status: 'delivered' })).toContain('violates check constraint "adapter_request_closed"');
    expect(await refusal({ closedAt: NOW })).toContain('violates check constraint "adapter_request_closed"');
  });
});

describe('clearExpiredExamples', () => {
  it('keeps a request closed 89 days ago, and clears one closed 90 days ago: its examples, its zip, and when', async () => {
    const db = await withUser();
    await aRequest(db, 'AR-KEPT');
    await aRequest(db, 'AR-TYPED');
    const zip = await aRequest(db, 'AR-ZIP', 'data_set');
    await moveTo(db, 'AR-KEPT', 'delivered', daysBefore(89));
    await moveTo(db, 'AR-TYPED', 'delivered', daysBefore(90));
    await moveTo(db, 'AR-ZIP', 'declined', daysBefore(120));
    const { store, removed } = fakeStore();

    const result = await clearExpiredExamples(db, store.remove, NOW);

    expect(result).toEqual({ cleared: ['AR-ZIP', 'AR-TYPED'], failed: [] });
    expect(removed).toEqual([zip]);
    const kept = await rowOf(db, 'AR-KEPT');
    expect((kept.examples as unknown[]).length).toBe(30);
    expect(kept.examplesDeletedAt).toBeNull();
    const typed = await rowOf(db, 'AR-TYPED');
    expect(typed).toMatchObject({ examples: [], examplesDeletedAt: NOW, status: 'delivered', closedAt: daysBefore(90), updatedAt: daysBefore(90) });
    expect(typed.form).toEqual((({ examples: _e, dataSet: _d, rights: _r, source: _s, ...stored }) => stored)(aForm()));
    expect(await rowOf(db, 'AR-ZIP')).toMatchObject({
      examples: [], examplesDeletedAt: NOW, status: 'declined', declineReason: 'Not something we can train.',
      dataSetPathname: null, dataSetUrl: null, dataSetBytes: 2_500_000, dataSetRows: 330,
    });
  });

  it('never clears a request that is received, in review or training, however old', async () => {
    const db = await withUser();
    await aRequest(db, 'AR-RECEIVED');
    await aRequest(db, 'AR-REVIEW', 'data_set');
    await aRequest(db, 'AR-TRAINING');
    await moveTo(db, 'AR-REVIEW', 'in_review', daysBefore(300));
    await moveTo(db, 'AR-TRAINING', 'training', daysBefore(300));
    const { store, removed } = fakeStore();
    expect(await clearExpiredExamples(db, store.remove, NOW)).toEqual({ cleared: [], failed: [] });
    expect(removed).toEqual([]);
    for (const id of ['AR-RECEIVED', 'AR-REVIEW', 'AR-TRAINING']) expect((await rowOf(db, id)).examplesDeletedAt).toBeNull();
  });

  it('a request moved back to an open status is not cleared, though it was closed long ago', async () => {
    const db = await withUser();
    await aRequest(db, 'AR-1');
    await moveTo(db, 'AR-1', 'delivered', daysBefore(200));
    await moveTo(db, 'AR-1', 'training', daysBefore(150));
    const { store } = fakeStore();
    expect(await clearExpiredExamples(db, store.remove, NOW)).toEqual({ cleared: [], failed: [] });
    expect((await rowOf(db, 'AR-1')).closedAt).toBeNull();
  });

  it('a second run does nothing', async () => {
    const db = await withUser();
    await aRequest(db, 'AR-ZIP', 'data_set');
    await moveTo(db, 'AR-ZIP', 'delivered', daysBefore(95));
    const { store, removed } = fakeStore();
    expect((await clearExpiredExamples(db, store.remove, NOW)).cleared).toEqual(['AR-ZIP']);
    const later = new Date(NOW.getTime() + DAY_MS);
    expect(await clearExpiredExamples(db, store.remove, later)).toEqual({ cleared: [], failed: [] });
    expect(removed).toHaveLength(1);
    expect((await rowOf(db, 'AR-ZIP')).examplesDeletedAt).toEqual(NOW);
  });

  it('a zip that cannot be deleted leaves that request as it was, names it, and the others are still cleared', async () => {
    const db = await withUser();
    const stuck = await aRequest(db, 'AR-STUCK', 'data_set');
    const fine = await aRequest(db, 'AR-FINE', 'data_set');
    await moveTo(db, 'AR-STUCK', 'delivered', daysBefore(100));
    await moveTo(db, 'AR-FINE', 'delivered', daysBefore(95));
    const { store, removed } = fakeStore([stuck!]);
    const result = await clearExpiredExamples(db, store.remove, NOW);
    expect(result.cleared).toEqual(['AR-FINE']);
    expect(result.failed).toEqual([{ id: 'AR-STUCK', message: `Vercel Blob: the store did not delete ${stuck}: status 503` }]);
    expect(removed).toEqual([fine]);
    expect(await rowOf(db, 'AR-STUCK')).toMatchObject({ examplesDeletedAt: null, dataSetPathname: stuck });
  });
});

describe('GET /api/cron/clear-examples', () => {
  const SECRET = TEST_ENV.CRON_SECRET;
  const cron = (headers: Record<string, string> = { authorization: `Bearer ${SECRET}` }, method = 'GET') =>
    new Request(`${BASE}${CLEAR_EXAMPLES_PATH}`, { method, headers });
  const endpointWith = (db: Db, store: DataSetStore) => cronEndpoints({ db: async () => db, dataSets: store, now: () => NOW }).clearExamples;

  it('is routed to the cron endpoint', () => {
    expect(endpointFor(CLEAR_EXAMPLES_PATH)).toBeDefined();
  });

  it("runs the job for Vercel's cron request and answers with the requests it cleared", async () => {
    const db = await withUser();
    await aRequest(db, 'AR-1');
    await moveTo(db, 'AR-1', 'declined', daysBefore(90));
    const response = await serveEndpoint(endpointWith(db, fakeStore().store), cron());
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ cleared: ['AR-1'], swept: 0 });
  });

  it('deletes the zips no request points to once they are 24 hours old, and keeps the rest (final review I1)', async () => {
    const db = await withUser();
    const used = await aRequest(db, 'AR-ZIP', 'data_set');
    const hoursAgo = (hours: number) => new Date(NOW.getTime() - hours * 60 * 60 * 1000);
    const listed: ListedBlob[] = [
      { pathname: used!, uploadedAt: hoursAgo(48) },
      { pathname: 'adapter-requests/u1/data-set-Old.zip', uploadedAt: hoursAgo(24) },
      { pathname: 'adapter-requests/gone-user/data-set-Old.zip', uploadedAt: hoursAgo(300) },
      { pathname: 'adapter-requests/u1/data-set-New.zip', uploadedAt: hoursAgo(23) },
    ];
    const { store, removed } = fakeStore([], listed);
    const response = await serveEndpoint(endpointWith(db, store), cron());
    expect(await response.json()).toEqual({ cleared: [], swept: 2 });
    expect(removed).toEqual(['adapter-requests/u1/data-set-Old.zip', 'adapter-requests/gone-user/data-set-Old.zip']);
  });

  it('answers 404 to a wrong secret, a missing one, another scheme and another method, and does nothing', async () => {
    const db = await withUser();
    await aRequest(db, 'AR-1');
    await moveTo(db, 'AR-1', 'delivered', daysBefore(90));
    const endpoint = endpointWith(db, fakeStore().store);
    for (const request of [cron({ authorization: `Bearer ${SECRET}x` }), cron({}), cron({ authorization: SECRET }),
      cron({ authorization: `Basic ${SECRET}` }), cron(undefined, 'POST')]) {
      const response = await serveEndpoint(endpoint, request);
      expect(response.status).toBe(404);
      expect((await response.json()).error).toMatchObject({ type: 'NotFound', message: `the hub has no endpoint at ${CLEAR_EXAMPLES_PATH}` });
    }
    expect((await rowOf(db, 'AR-1')).examplesDeletedAt).toBeNull();
  });

  it('without CRON_SECRET set, answers the 404 of an unknown path (the log names the setting) and runs nothing (re-review R8)', async () => {
    const logged = vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.stubEnv('CRON_SECRET', '');
    const db = await withUser();
    const response = await serveEndpoint(endpointWith(db, fakeStore().store), cron({ authorization: 'Bearer ' }));
    expect(response.status).toBe(404);
    const { error } = await response.json();
    expect(error).toEqual({ type: 'NotFound', message: `the hub has no endpoint at ${CLEAR_EXAMPLES_PATH}`, details: { path: CLEAR_EXAMPLES_PATH } });
    expect(String(logged.mock.calls[0][0])).toContain('CRON_SECRET');
  });

  it('refuses a CRON_SECRET shorter than 16 characters, naming it', async () => {
    vi.stubEnv('CRON_SECRET', 'short');
    const db = await withUser();
    await expect(serveEndpoint(endpointWith(db, fakeStore().store), cron({ authorization: 'Bearer short' })))
      .rejects.toThrow('CRON_SECRET must be at least 16 characters');
  });

  it('a zip that cannot be deleted: answers 500 naming the request, logs it without the examples, and clears the others', async () => {
    const logged = vi.spyOn(console, 'error').mockImplementation(() => {});
    const db = await withUser();
    const stuck = await aRequest(db, 'AR-STUCK', 'data_set');
    await aRequest(db, 'AR-FINE');
    await moveTo(db, 'AR-STUCK', 'delivered', daysBefore(100));
    await moveTo(db, 'AR-FINE', 'delivered', daysBefore(95));
    const response = await serveEndpoint(endpointWith(db, fakeStore([stuck!]).store), cron());
    expect(response.status).toBe(500);
    const { error } = await response.json();
    expect(error.type).toBe('ExamplesNotDeleted');
    expect(error.message).toContain('AR-STUCK');
    expect(error.details).toEqual({ cleared: ['AR-FINE'], failed: [{ id: 'AR-STUCK', message: `Vercel Blob: the store did not delete ${stuck}: status 503` }] });
    expect(logged).toHaveBeenCalledTimes(1);
    expect(String(logged.mock.calls[0][0])).toContain('AR-STUCK');
    const everything = JSON.stringify(logged.mock.calls) + JSON.stringify(error);
    expect(everything).not.toContain('Support email number');
    expect(everything).not.toContain(TEST_ENV.CRON_SECRET);
    expect((await rowOf(db, 'AR-FINE')).examplesDeletedAt).toEqual(NOW);
  });
});

describe("the owner's page after the examples were deleted", () => {
  const owner = (db: Db) => adapterRequestEndpoints({
    store: async () => ({ db, settings: testSettings() }),
    notifyEmail: () => 'notify@example.com',
    dataSets: fakeStore().store,
    mailer: () => async () => {},
    signedIn: async () => ({ id: 'owner', email: 'owner@example.com', emailVerified: true, name: 'Owner', image: null }),
    now: () => NOW,
  }).adminOne;
  const get = (p: string) => new Request(`${BASE}/api/admin/adapter-requests/${p}`);

  it('the request says when, and the examples export and the data set download answer 410; the manifest draft still downloads', async () => {
    const db = await withUser();
    await aRequest(db, 'AR-TYPED');
    await aRequest(db, 'AR-ZIP', 'data_set');
    await moveTo(db, 'AR-TYPED', 'delivered', daysBefore(90));
    await moveTo(db, 'AR-ZIP', 'delivered', daysBefore(90));
    await clearExpiredExamples(db, fakeStore().store.remove, NOW);
    const endpoint = owner(db);
    const detail = await (await serveEndpoint(endpoint, get('AR-ZIP'))).json();
    expect(detail.request).toMatchObject({ examplesDeletedAt: NOW.toISOString(), closedAt: daysBefore(90).toISOString(), examples: [],
      dataSet: { bytes: 2_500_000, rows: 330 } });
    for (const p of ['AR-TYPED/export?file=examples', 'AR-ZIP/data-set']) {
      const response = await serveEndpoint(endpoint, get(p));
      expect(response.status, p).toBe(410);
      expect((await response.json()).error.message).toContain(`were deleted on ${NOW.toISOString().slice(0, 10)}`);
    }
    expect((await serveEndpoint(endpoint, get('AR-TYPED/export?file=manifest'))).status).toBe(200);
  });
});

describe('the migration', () => {
  it('gives requests already delivered or declined their updated_at as the time they were closed', async () => {
    const folder = path.join(import.meta.dirname, '../drizzle');
    const before = fs.mkdtempSync(path.join(os.tmpdir(), 'hub-migrations-'));
    try {
      fs.cpSync(folder, before, { recursive: true });
      const journalFile = path.join(before, 'meta/_journal.json');
      const journal = JSON.parse(fs.readFileSync(journalFile, 'utf8')) as { entries: { tag: string }[] };
      const last = journal.entries.findIndex((entry) => entry.tag === '0006_examples_retention');
      expect(last).toBeGreaterThan(0);
      journal.entries = journal.entries.slice(0, last);
      fs.writeFileSync(journalFile, JSON.stringify(journal));
      const client = new PGlite();
      const db = drizzle(client, { schema });
      await migrate(db, { migrationsFolder: before });
      await client.exec(`insert into "user" (id, name, email, email_verified, created_at, updated_at) values ('u1', 'u1', 'u1@example.com', true, now(), now())`);
      const insert = (id: string, status: string, updated: string) => client.query(
        `insert into adapter_request (id, user_id, status, form, example_source, examples, may_share, no_personal_data, may_improve_base, created_at, updated_at, decline_reason)
         values ($1, 'u1', $2, '{}', 'generate', '[]', true, true, false, '2026-01-01T00:00:00Z', $3, $4)`,
        [id, status, updated, status === 'declined' ? 'No.' : null]);
      await insert('AR-D', 'delivered', '2026-02-01T00:00:00Z');
      await insert('AR-X', 'declined', '2026-03-01T00:00:00Z');
      await insert('AR-O', 'training', '2026-04-01T00:00:00Z');
      await migrate(db, { migrationsFolder: folder });
      const rows = await client.query<{ id: string; closed_at: Date | null }>('select id, closed_at from adapter_request order by id');
      expect(rows.rows).toEqual([
        { id: 'AR-D', closed_at: new Date('2026-02-01T00:00:00Z') },
        { id: 'AR-O', closed_at: null },
        { id: 'AR-X', closed_at: new Date('2026-03-01T00:00:00Z') },
      ]);
    } finally {
      fs.rmSync(before, { recursive: true, force: true });
    }
  });
});
