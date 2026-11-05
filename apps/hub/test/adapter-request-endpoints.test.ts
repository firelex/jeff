// @vitest-environment node
// The adapter request endpoints (server/endpoints/adapter-requests.ts) against a real database (the migrations, in
// PGlite): sending (sign-in, the cross-site check, JSON only, the shared rules, the limits, the owner's email, and
// nothing stored when that email fails), the requester's own list, the owner-only admin endpoints, the export's
// shape, and a deleted account taking its requests with it.
import { getPayloadFromClientToken } from '@vercel/blob/client';
import { eq } from 'drizzle-orm';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MAX_OPEN_REQUESTS, MAX_SENDS_PER_DAY, MAX_UPLOADS_PER_DAY, type AdapterRequestForm } from '../app/lib/adapter-request';
import { CLAIMABLE_HOURS, manifestDraft, ORPHAN_ZIP_HOURS, trainingRows } from '../app/lib/adapter-requests.server';
import { deleteAccount } from '../app/lib/billing.server';
import type { DataSetStore, StoredBlob } from '../app/lib/data-set-store.server';
import type { Db } from '../app/lib/db.server';
import type { Mail } from '../app/lib/mail.server';
import { adapterRequest, dataSetUpload, user } from '../app/lib/schema';
import type { SessionUser } from '../app/lib/session.server';
import { serveEndpoint } from '../server/endpoint';
import { adapterRequestEndpoints } from '../server/endpoints/adapter-requests';
import { ApiError } from '../server/http';
import { endpointFor } from '../server/router';
import { aDataSetForm, aForm, aFormWith, anExample, DATA_SET_PATHNAME } from './helpers/adapter-request';
import { aUser, NOW } from './helpers/accounts';
import { testDb } from './helpers/db';
import { TEST_ENV, testSettings } from './helpers/settings';
import { fakeStripe } from './helpers/stripe';

const BASE = 'https://jeffhub.ai';
const ADA: SessionUser = { id: 'u1', email: 'u1@example.com', emailVerified: true, name: 'Ada', image: null };
const OWNER: SessionUser = { id: 'owner', email: 'Owner@Example.com', emailVerified: true, name: 'Owner', image: null };

beforeEach(() => { for (const [name, value] of Object.entries(TEST_ENV)) vi.stubEnv(name, value); });
afterEach(() => vi.unstubAllEnvs());

// The file store's token: a made-up one of Vercel Blob's form (vercel_blob_rw_<store id>_<secret>), which signs the
// client token here without any call to Vercel.
const BLOB_TOKEN = 'vercel_blob_rw_teststore_notarealsecret';
const NOTIFY = 'notify@example.com';

// A stand-in for the private file store: the blobs it has, and what was deleted and linked.
function fakeStore(blobs: StoredBlob[] = [], uploadedAt: Date = NOW) {
  const held = new Map(blobs.map((b) => [b.pathname, b]));
  const removed: string[] = [];
  const store: DataSetStore = {
    readWriteToken: () => BLOB_TOKEN,
    find: async (pathname) => held.get(pathname) ?? null,
    remove: async (pathnames) => { for (const p of pathnames) { held.delete(p); removed.push(p); } },
    list: async (prefix) => [...held.keys()].filter((p) => p.startsWith(prefix)).map((pathname) => ({ pathname, uploadedAt })),
    downloadUrl: async (pathname, validUntil) => `https://teststore.private.blob.vercel-storage.com/${pathname}?signed=1&until=${validUntil}`,
  };
  return { store, held, removed };
}
const privateBlob = (pathname = DATA_SET_PATHNAME, size = 2_500_000): StoredBlob =>
  ({ pathname, size, url: `https://teststore.private.blob.vercel-storage.com/${pathname}` });

function setUp(db: Db, signedIn: SessionUser | null = ADA,
  options: { failMail?: boolean; now?: () => Date; blobs?: ReturnType<typeof fakeStore> } = {}) {
  const mails: Mail[] = [];
  const who = { current: signedIn };
  const blobs = options.blobs ?? fakeStore();
  const endpoints = adapterRequestEndpoints({
    store: async () => ({ db, settings: testSettings() }),
    notifyEmail: () => NOTIFY,
    dataSets: blobs.store,
    mailer: () => async (mail) => {
      if (options.failMail === true) throw new Error('SendGrid did not send the email to owner@example.com: status 503: unavailable');
      mails.push(mail);
    },
    signedIn: async () => {
      if (who.current === null) throw new ApiError(401, 'SignInRequired', 'sign in first');
      return who.current;
    },
    now: options.now ?? (() => NOW),
  });
  return { endpoints, mails, who, blobs };
}

const send = (body: unknown, headers: Record<string, string> = { origin: BASE, 'content-type': 'application/json' }) =>
  new Request(`${BASE}/api/adapter-requests`, { method: 'POST', headers, body: typeof body === 'string' ? body : JSON.stringify(body) });
const get = (path: string) => new Request(`${BASE}${path}`);
const postJson = (path: string, body: unknown) => new Request(`${BASE}${path}`, { method: 'POST', headers: { origin: BASE, 'content-type': 'application/json' }, body: JSON.stringify(body) });

async function withUsers() {
  const db = await testDb();
  await aUser(db, 'u1');
  await aUser(db, 'owner', 'owner@example.com');
  return db;
}

// The users, and an upload token issued to u1 for DATA_SET_PATHNAME at `issuedAt` (as the upload endpoint records
// one), so a request may name that zip.
async function withAnUpload(issuedAt: Date = NOW) {
  const db = await withUsers();
  await db.insert(dataSetUpload).values({ id: 'upload-1', userId: 'u1', pathname: DATA_SET_PATHNAME, issuedAt });
  return db;
}

describe('POST /api/adapter-requests', () => {
  it('stores the request, emails the owner without the examples, and answers 201 with the request number', async () => {
    const db = await withUsers();
    const { endpoints, mails } = setUp(db);
    const response = await serveEndpoint(endpoints.mine, send(aForm()));
    expect(response.status).toBe(201);
    const { id } = await response.json() as { id: string };
    expect(id).toMatch(/^AR-[A-Z2-7]{8}$/);
    const [row] = await db.select().from(adapterRequest);
    expect(row).toMatchObject({ id, userId: 'u1', status: 'received', mayShare: true, noPersonalData: true, mayImproveBase: false,
      declineReason: null, createdAt: NOW, updatedAt: NOW });
    expect((row.examples as unknown[]).length).toBe(30);
    expect(row.form).not.toHaveProperty('examples');
    expect(mails).toHaveLength(1);
    expect(mails[0].to).toBe(NOTIFY);
    expect(mails[0].subject).toBe(`Adapter request ${id}`);
    expect(mails[0].text).toBe([
      'A new adapter request has arrived.', '', `Request number: ${id}`, 'From: u1@example.com',
      'Question: Which team should handle this support email?', 'Kind of answer: One option from a fixed list', 'Options: 3',
      'Examples: Generate the data for me', 'Typed examples: 30', '',
      `Open it: https://jeffhub.ai/admin/requests?id=${id}`].join('\n'));
    expect(mails[0].text + mails[0].html).not.toContain('Support email number');
  });

  it('a failed email fails the request, and nothing is stored', async () => {
    const db = await withUsers();
    const { endpoints } = setUp(db, ADA, { failMail: true });
    await expect(serveEndpoint(endpoints.mine, send(aForm()))).rejects.toThrow('SendGrid did not send the email');
    expect(await db.select().from(adapterRequest)).toEqual([]);
  });

  it('refuses a request that breaks the rules, naming the field, and stores nothing', async () => {
    const db = await withUsers();
    const { endpoints, mails } = setUp(db);
    const response = await serveEndpoint(endpoints.mine, send(aFormWith((f) => { f.examples = f.examples.slice(0, 10); f.decision.question = 'Short'; })));
    expect(response.status).toBe(400);
    const { error } = await response.json() as { error: { type: string; message: string; details: { problems: { field: string }[] } } };
    expect(error.type).toBe('InvalidRequest');
    expect(error.message).toBe('The question must be 10 to 300 characters; it has 5. (and 1 more problem)');
    expect(error.details.problems.map((p) => p.field)).toEqual(['decision.question', 'examples']);
    expect(await db.select().from(adapterRequest)).toEqual([]);
    expect(mails).toEqual([]);
  });

  it('needs a signed-in visitor, the hub\'s own origin and JSON', async () => {
    const db = await withUsers();
    expect((await serveEndpoint(setUp(db, null).endpoints.mine, send(aForm()))).status).toBe(401);
    const { endpoints } = setUp(db);
    const crossSite = await serveEndpoint(endpoints.mine, send(aForm(), { origin: 'https://evil.example', 'content-type': 'application/json' }));
    expect(crossSite.status).toBe(403);
    const notJson = await serveEndpoint(endpoints.mine, send(aForm(), { origin: BASE, 'content-type': 'text/plain' }));
    expect(notJson.status).toBe(415);
    const broken = await serveEndpoint(endpoints.mine, send('{not json', { origin: BASE, 'content-type': 'application/json' }));
    expect(broken.status).toBe(400);
    expect(await db.select().from(adapterRequest)).toEqual([]);
  });

  it('refuses a body over 4 MB with 413', async () => {
    const db = await withUsers();
    const big = aFormWith((f) => { f.decision.why = 'x'.repeat(4_000_001); });
    const response = await serveEndpoint(setUp(db).endpoints.mine, send(big));
    expect(response.status).toBe(413);
    expect((await response.json()).error.type).toBe('RequestTooLarge');
  });

  it(`allows at most ${MAX_OPEN_REQUESTS} open requests`, async () => {
    const db = await withUsers();
    const { endpoints } = setUp(db);
    for (let i = 0; i < MAX_OPEN_REQUESTS; i += 1) expect((await serveEndpoint(endpoints.mine, send(aForm()))).status).toBe(201);
    const refused = await serveEndpoint(endpoints.mine, send(aForm()));
    expect(refused.status).toBe(409);
    expect((await refused.json()).error).toEqual({ type: 'RequestRefused', details: {},
      message: 'You have 3 open adapter requests, the most an account may have at once; wait until one is delivered or declined.' });
    // A delivered one is no longer open.
    const [first] = await db.select().from(adapterRequest).limit(1);
    await db.update(adapterRequest).set({ status: 'delivered', closedAt: NOW }).where(eq(adapterRequest.id, first.id));
    expect((await serveEndpoint(endpoints.mine, send(aForm()))).status).toBe(201);
  });

  it(`allows at most ${MAX_SENDS_PER_DAY} sends in 24 hours`, async () => {
    const db = await withUsers();
    let now = NOW;
    const { endpoints } = setUp(db, ADA, { now: () => now });
    for (let i = 0; i < MAX_SENDS_PER_DAY; i += 1) {
      expect((await serveEndpoint(endpoints.mine, send(aForm()))).status).toBe(201);
      await db.update(adapterRequest).set({ status: 'declined', declineReason: 'test', closedAt: NOW });
    }
    const refused = await serveEndpoint(endpoints.mine, send(aForm()));
    expect(refused.status).toBe(409);
    expect((await refused.json()).error.message).toBe('You have sent 5 adapter requests in the last 24 hours, the most an account may; try again tomorrow.');
    now = new Date(NOW.getTime() + 24 * 60 * 60 * 1000 + 1);
    expect((await serveEndpoint(endpoints.mine, send(aForm()))).status).toBe(201);
  });

  it('two sends at once cannot pass the limit of open requests', async () => {
    const db = await withUsers();
    const { endpoints } = setUp(db);
    const statuses = await Promise.all(Array.from({ length: 5 }, () => serveEndpoint(endpoints.mine, send(aForm())).then((r) => r.status)));
    expect(statuses.filter((s) => s === 201)).toHaveLength(MAX_OPEN_REQUESTS);
    expect(await db.select().from(adapterRequest)).toHaveLength(MAX_OPEN_REQUESTS);
  });
});

describe('GET /api/adapter-requests', () => {
  it("lists the visitor's own requests, newest first, with a declined one's reason", async () => {
    const db = await withUsers();
    await aUser(db, 'u2');
    let now = NOW;
    const { endpoints, who } = setUp(db, ADA, { now: () => now });
    const first = (await (await serveEndpoint(endpoints.mine, send(aForm()))).json()).id as string;
    now = new Date(NOW.getTime() + 60_000);
    const second = (await (await serveEndpoint(endpoints.mine, send(aFormWith((f) => { f.decision.question = 'Is this email urgent enough to page someone?'; })))).json()).id as string;
    await db.update(adapterRequest).set({ status: 'declined', declineReason: 'Too few real examples.', closedAt: NOW }).where(eq(adapterRequest.id, first));
    who.current = OWNER;
    await serveEndpoint(endpoints.mine, send(aForm()));
    who.current = ADA;
    const response = await serveEndpoint(endpoints.mine, get('/api/adapter-requests'));
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ dataSetFolder: 'adapter-requests/u1/', requests: [
      { id: second, question: 'Is this email urgent enough to page someone?', status: 'received', createdAt: now.toISOString(), updatedAt: now.toISOString(), declineReason: null },
      { id: first, question: 'Which team should handle this support email?', status: 'declined', createdAt: NOW.toISOString(), updatedAt: NOW.toISOString(), declineReason: 'Too few real examples.' },
    ] });
  });

  it('needs a signed-in visitor', async () => {
    const db = await withUsers();
    expect((await serveEndpoint(setUp(db, null).endpoints.mine, get('/api/adapter-requests'))).status).toBe(401);
  });
});

describe('the admin endpoints', () => {
  async function withOneRequest() {
    const db = await withUsers();
    const setup = setUp(db);
    const id = (await (await serveEndpoint(setup.endpoints.mine, send(aForm()))).json()).id as string;
    setup.who.current = OWNER;
    return { db, id, ...setup };
  }

  it('answer the owner only; anyone else, signed in or not, gets the 404 of a path that does not exist', async () => {
    const { db, id, endpoints, who } = await withOneRequest();
    for (const visitor of [ADA, null]) {
      who.current = visitor;
      for (const [endpoint, request] of [
        [endpoints.adminList, get('/api/admin/adapter-requests')],
        [endpoints.adminOne, get(`/api/admin/adapter-requests/${id}`)],
        [endpoints.adminOne, get(`/api/admin/adapter-requests/${id}/export?file=examples`)],
        [endpoints.adminOne, postJson(`/api/admin/adapter-requests/${id}/status`, { status: 'in_review' })],
      ] as const) {
        const response = await serveEndpoint(endpoint, request);
        expect(response.status).toBe(404);
        expect((await response.json()).error).toEqual({ type: 'NotFound', message: `the hub has no endpoint at ${new URL(request.url).pathname}`,
          details: { path: new URL(request.url).pathname } });
      }
    }
    const [row] = await db.select().from(adapterRequest);
    expect(row.status).toBe('received');
  });

  it('are routed: the list by its path, one request\'s paths by the prefix', () => {
    expect(endpointFor('/api/admin/adapter-requests')).toBeDefined();
    expect(endpointFor('/api/admin/adapter-requests/AR-ABCDEFGH/status')).toBe(endpointFor('/api/admin/adapter-requests/AR-ABCDEFGH'));
    expect(endpointFor('/api/adapter-requests')).toBeDefined();
  });

  it('list every request with its requester and counts, and show one in full', async () => {
    const { id, endpoints } = await withOneRequest();
    const list = await serveEndpoint(endpoints.adminList, get('/api/admin/adapter-requests'));
    expect(await list.json()).toEqual({ requests: [{ id, email: 'u1@example.com', question: 'Which team should handle this support email?',
      status: 'received', kind: 'one', source: 'generate', optionCount: 3, exampleCount: 30, dataSet: null, createdAt: NOW.toISOString(), examplesDeletedAt: null }] });
    const one = await serveEndpoint(endpoints.adminOne, get(`/api/admin/adapter-requests/${id}`));
    const { request } = await one.json();
    expect(request).toMatchObject({ id, email: 'u1@example.com', declineReason: null, consent: { mayShare: true, noPersonalData: true, mayImproveBase: false } });
    expect(request.examples).toHaveLength(30);
    expect(request.form.decision.why).toBe('Emails reach the right team faster.');
    const missing = await serveEndpoint(endpoints.adminOne, get('/api/admin/adapter-requests/AR-NOSUCH00'));
    expect(missing.status).toBe(404);
    expect((await missing.json()).error.message).toBe('there is no adapter request AR-NOSUCH00');
    // A malformed % escape is the 404 of a path the hub does not have, not a 500 (final review M4).
    for (const [endpoint, request] of [[endpoints.adminOne, get('/api/admin/adapter-requests/AR-%E0%A4%A')],
      [endpoints.adminOne, postJson('/api/admin/adapter-requests/AR-%ZZ/status', { status: 'training' })]] as const) {
      const bad = await serveEndpoint(endpoint, request);
      expect(bad.status).toBe(404);
      expect((await bad.json()).error.type).toBe('NotFound');
    }
  });

  it('set the status; declining needs a reason, which the requester then sees', async () => {
    const { db, id, endpoints, who } = await withOneRequest();
    const path = `/api/admin/adapter-requests/${id}/status`;
    expect((await serveEndpoint(endpoints.adminOne, postJson(path, { status: 'training' }))).status).toBe(200);
    expect((await db.select().from(adapterRequest))[0].status).toBe('training');
    const noReason = await serveEndpoint(endpoints.adminOne, postJson(path, { status: 'declined' }));
    expect(noReason.status).toBe(400);
    expect((await noReason.json()).error.message).toBe('say why the request is declined, in 1 to 600 characters');
    const stray = await serveEndpoint(endpoints.adminOne, postJson(path, { status: 'in_review', declineReason: 'x' }));
    expect(stray.status).toBe(400);
    const unknown = await serveEndpoint(endpoints.adminOne, postJson(path, { status: 'lost' }));
    expect((await unknown.json()).error.message).toBe('the status must be one of received, in_review, training, delivered, declined');
    expect((await serveEndpoint(endpoints.adminOne, postJson(path, { status: 'declined', declineReason: ' Too few examples. ' }))).status).toBe(200);
    who.current = ADA;
    const mine = await (await serveEndpoint(endpoints.mine, get('/api/adapter-requests'))).json();
    expect(mine.requests[0]).toMatchObject({ status: 'declined', declineReason: 'Too few examples.' });
    who.current = OWNER;
    // Moving it on again clears the reason.
    expect((await serveEndpoint(endpoints.adminOne, postJson(path, { status: 'in_review' }))).status).toBe(200);
    expect((await db.select().from(adapterRequest))[0].declineReason).toBeNull();
  });

  it('a status change is refused from another site', async () => {
    const { id, endpoints } = await withOneRequest();
    const request = new Request(`${BASE}/api/admin/adapter-requests/${id}/status`, { method: 'POST',
      headers: { origin: 'https://evil.example', 'content-type': 'application/json' }, body: '{"status":"delivered"}' });
    expect((await serveEndpoint(endpoints.adminOne, request)).status).toBe(403);
  });

  it('export the examples as training rows (JSON Lines) and a manifest draft, as downloads', async () => {
    const { id, endpoints } = await withOneRequest();
    const rows = await serveEndpoint(endpoints.adminOne, get(`/api/admin/adapter-requests/${id}/export?file=examples`));
    expect(rows.status).toBe(200);
    expect(rows.headers.get('content-type')).toBe('application/x-ndjson; charset=utf-8');
    expect(rows.headers.get('content-disposition')).toBe(`attachment; filename="${id}-examples.jsonl"`);
    expect(rows.headers.get('cache-control')).toBe('no-store');
    const lines = (await rows.text()).trimEnd().split('\n').map((line) => JSON.parse(line));
    expect(lines).toHaveLength(30);
    expect(lines[0]).toEqual({
      id: `${id}-01`,
      state: { product: 'Widget', input: 'Support email number 0: something about billing.' },
      question: { type: 'choice', instructions: 'Which team should handle this support email?',
        criteria: { billing: 'Payments, invoices and refunds.', tech: 'Bugs and errors in the product.', none: 'Neither team.' } },
      label: 'billing', target: 'billing',
    });
    expect(Object.keys(lines[0].state)).toEqual(['product', 'input']);
    const manifest = await serveEndpoint(endpoints.adminOne, get(`/api/admin/adapter-requests/${id}/export?file=manifest`));
    expect(manifest.headers.get('content-disposition')).toBe(`attachment; filename="${id}-manifest-draft.json"`);
    const draft = await manifest.json();
    expect(draft).toMatchObject({ request: id, id: null, to_fill: ['id', 'functions.decide.interface', 'base'],
      context: [{ key: 'product', description: 'The product the email is about.', required: true, example: 'Widget' }],
      input: { key: 'input', form: 'text' },
      functions: { decide: { interface: null, type: 'choice', labels: { source: 'fixed', subset: false,
        fixed: { billing: 'Payments, invoices and refunds.', tech: 'Bugs and errors in the product.', none: 'Neither team.' } }, when_none_fits: 'the option "none"' } } });
    const noFile = await serveEndpoint(endpoints.adminOne, get(`/api/admin/adapter-requests/${id}/export`));
    expect(noFile.status).toBe(400);
  });
});

describe('the training rows of each kind', () => {
  const stored = (form: AdapterRequestForm) => { const { examples: _e, rights: _r, ...rest } = form; return rest; };

  it('yes or no is a probability question; several options a list of 1 or 0 per option', () => {
    const yesNo = aFormWith((f) => {
      f.answer = { kind: 'yes_no', options: [{ key: 'yes', definition: 'Urgent' }, { key: 'no', definition: 'Can wait' }], noneFits: 'abstain', exampleSets: [] };
    });
    const [yes, no] = trainingRows('AR-X', stored(yesNo), [anExample(0, 'yes'), anExample(1, 'no')]);
    expect(yes).toMatchObject({ question: { type: 'noul' }, label: 'yes', target: 1 });
    expect(no).toMatchObject({ label: 'no', target: 0 });
    const several = aFormWith((f) => { f.answer.kind = 'several'; });
    const [row] = trainingRows('AR-X', stored(several), [{ ...anExample(0), answer: ['tech', 'billing'] }]);
    expect(row).toMatchObject({ question: { type: 'choice', subset: true }, label: ['tech', 'billing'], target: [1, 1, 0] });
  });

  it('options given by the caller use each example\'s own options; named fields put the input last as an object', () => {
    const caller = aFormWith((f) => {
      f.answer = { kind: 'caller', options: [], noneFits: 'abstain', exampleSets: [[{ key: 'a', definition: 'A' }, { key: 'b', definition: 'B' }], [], []] };
      f.input = { form: 'fields', fields: [{ name: 'subject', description: 's' }], typicalLength: 1, maxLength: 9, languages: 'en' };
    });
    const [row] = trainingRows('AR-X', stored(caller), [{ input: { subject: 'Hi' }, context: { product: 'W' }, answer: 'y', reason: '', tricky: false,
      options: [{ key: 'x', definition: 'X' }, { key: 'y', definition: 'Y' }] }]);
    expect(row).toEqual({ id: 'AR-X-1', state: { product: 'W', input: { subject: 'Hi' } },
      question: { type: 'choice', instructions: 'Which team should handle this support email?', criteria: { x: 'X', y: 'Y' } }, label: 'y', target: 'y' });
    expect(manifestDraft('AR-X', stored(caller), { mayShare: true, noPersonalData: true, mayImproveBase: false }).functions.decide.labels)
      .toMatchObject({ source: 'caller', max: 2 });
  });
});

it("deleting an account deletes the account's requests", async () => {
  const db = await withUsers();
  const { endpoints } = setUp(db);
  expect((await serveEndpoint(endpoints.mine, send(aForm()))).status).toBe(201);
  await deleteAccount({ db, stripe: fakeStripe({}).api, settings: testSettings() }, 'u1', { list: async () => [], remove: async (zips) => { expect(zips).toEqual([]); } });
  expect(await db.select().from(user).where(eq(user.id, 'u1'))).toEqual([]);
  expect(await db.select().from(adapterRequest)).toEqual([]);
});

describe('a request with a data set', () => {
  it('is stored with the zip\'s place, size and rows once the store has the zip, private, in the account\'s folder', async () => {
    const db = await withAnUpload();
    const { endpoints, mails } = setUp(db, ADA, { blobs: fakeStore([privateBlob()]) });
    const response = await serveEndpoint(endpoints.mine, send(aDataSetForm()));
    expect(response.status).toBe(201);
    const { id } = await response.json() as { id: string };
    const [row] = await db.select().from(adapterRequest);
    expect(row).toMatchObject({ id, exampleSource: 'data_set', examples: [], dataSetPathname: DATA_SET_PATHNAME,
      dataSetUrl: `https://teststore.private.blob.vercel-storage.com/${DATA_SET_PATHNAME}`, dataSetBytes: 2_500_000, dataSetRows: 330,
      dataSetCounts: [{ key: 'billing', count: 110 }, { key: 'tech', count: 110 }, { key: 'none', count: 110 }] });
    expect(mails[0].to).toBe(NOTIFY);
    expect(mails[0].text).toContain('Options: 3\nExamples: I have a data set\nData set: 2.5 MB\nRows: 330\nRows per option: billing 110, tech 110, none 110\n');
  });

  it('is checked again on the server, without unzipping: refused when the zip is missing, public, another account\'s, of another size or too large', async () => {
    const db = await withAnUpload();
    const cases: [string, StoredBlob[], AdapterRequestForm, number, string][] = [
      ['missing', [], aDataSetForm(), 400, 'The uploaded data set is not in the store; upload it again.'],
      ['public', [{ ...privateBlob(), url: `https://teststore.public.blob.vercel-storage.com/${DATA_SET_PATHNAME}` }], aDataSetForm(), 400,
        'The data set was not uploaded as a private file; upload it again from this page.'],
      ['another account\'s', [privateBlob('adapter-requests/u2/data-set-Abc123.zip')],
        { ...aDataSetForm(), dataSet: { ...aDataSetForm().dataSet!, pathname: 'adapter-requests/u2/data-set-Abc123.zip' } }, 400,
        'The data set was not uploaded by this account; upload it again from this page.'],
      ['another size', [privateBlob(DATA_SET_PATHNAME, 2_400_000)], aDataSetForm(), 400,
        'The stored data set is 2,400,000 bytes, but the request says 2,500,000; upload it again.'],
      ['too large', [privateBlob(DATA_SET_PATHNAME, 500_000_001)], aDataSetForm(), 413, 'The data set is 500.0 MB; it may be at most 500.0 MB.'],
    ];
    for (const [what, blobs, form, status, message] of cases) {
      const store = fakeStore(blobs);
      const response = await serveEndpoint(setUp(db, ADA, { blobs: store }).endpoints.mine, send(form));
      expect(response.status, what).toBe(status);
      expect((await response.json()).error.message, what).toBe(message);
      // A refused zip of this account's folder is deleted; another account's, or a missing one, is left alone.
      expect(store.removed, what).toEqual(['missing', 'another account\'s'].includes(what) ? [] : [DATA_SET_PATHNAME]);
    }
    expect(await db.select().from(adapterRequest)).toEqual([]);
  });

  it('meets the rules of a data set: at least 300 rows and 20 for every option, no typed examples', async () => {
    const db = await withAnUpload();
    const few = aDataSetForm();
    few.dataSet = { ...few.dataSet!, rows: 299, counts: [{ key: 'billing', count: 19 }, { key: 'tech', count: 140 }, { key: 'none', count: 140 }] };
    const response = await serveEndpoint(setUp(db, ADA, { blobs: fakeStore([privateBlob()]) }).endpoints.mine, send(few));
    expect(response.status).toBe(400);
    expect((await response.json()).error.details.problems.map((p: { message: string }) => p.message)).toEqual([
      'The data set has 299 rows; it needs at least 300.',
      'The option "billing" has 19 rows in the data set; every option needs at least 20.',
    ]);
    const both = { ...aDataSetForm(), examples: aForm().examples };
    expect((await (await serveEndpoint(setUp(db).endpoints.mine, send(both))).json()).error.message)
      .toBe('With a data set the examples are in the zip; remove the typed examples, or choose "Generate the data for me".');
  });

  it('refuses a zip of the account\'s folder whose pathname was not issued to it by the upload endpoint (re-review R1)', async () => {
    const db = await withUsers();
    const blobs = fakeStore([privateBlob()]);
    const response = await serveEndpoint(setUp(db, ADA, { blobs }).endpoints.mine, send(aDataSetForm()));
    expect(response.status).toBe(400);
    expect((await response.json()).error.message).toBe("The data set was not uploaded from this account's request page; upload it again from this page.");
    expect(await db.select().from(adapterRequest)).toEqual([]);
  });

  it('refuses a zip whose token was issued 23 hours ago or more, so the daily job can never delete a zip being claimed (re-review R2)', async () => {
    const hoursAgo = (hours: number) => new Date(NOW.getTime() - hours * 60 * 60 * 1000);
    const late = await withAnUpload(hoursAgo(23));
    const refused = await serveEndpoint(setUp(late, ADA, { blobs: fakeStore([privateBlob()]) }).endpoints.mine, send(aDataSetForm()));
    expect(refused.status).toBe(400);
    expect((await refused.json()).error.message).toBe('The data set was uploaded more than 23 hours ago, and is deleted soon; upload it again.');
    const inTime = await withAnUpload(new Date(hoursAgo(23).getTime() + 1000));
    expect((await serveEndpoint(setUp(inTime, ADA, { blobs: fakeStore([privateBlob()]) }).endpoints.mine, send(aDataSetForm()))).status).toBe(201);
    expect(CLAIMABLE_HOURS).toBeLessThan(ORPHAN_ZIP_HOURS);
  });

  it('a failed email stores nothing and deletes the zip', async () => {
    const db = await withAnUpload();
    const blobs = fakeStore([privateBlob()]);
    await expect(serveEndpoint(setUp(db, ADA, { failMail: true, blobs }).endpoints.mine, send(aDataSetForm()))).rejects.toThrow('SendGrid did not send');
    expect(await db.select().from(adapterRequest)).toEqual([]);
    expect(blobs.removed).toEqual([DATA_SET_PATHNAME]);
  });
});

describe('POST /api/adapter-requests/upload', () => {
  const tokenRequest = (payload: Record<string, unknown> = {}, headers: Record<string, string> = { origin: BASE, 'content-type': 'application/json' }) =>
    new Request(`${BASE}/api/adapter-requests/upload`, { method: 'POST', headers, body: JSON.stringify({ type: 'blob.generate-client-token',
      payload: { pathname: 'adapter-requests/u1/data-set.zip', multipart: true, clientPayload: JSON.stringify({ contentType: 'application/zip', bytes: 2_500_000 }), ...payload } }) });

  it("issues a token that can create exactly one zip, at a pathname of the account's folder the server picked, for 30 minutes (re-review R1)", async () => {
    const db = await withUsers();
    const { endpoints } = setUp(db);
    const first = await (await serveEndpoint(endpoints.upload, tokenRequest())).json() as { type: string; clientToken: string; pathname: string };
    expect(first.type).toBe('blob.generate-client-token');
    expect(first.pathname).toMatch(/^adapter-requests\/u1\/data-set-[a-z2-7]{16}\.zip$/);
    // The token is signed for that one pathname, with no random suffix and no overwrite: the store refuses a second
    // upload with it, which would be at the same pathname.
    expect(getPayloadFromClientToken(first.clientToken)).toMatchObject({ pathname: first.pathname, allowedContentTypes: ['application/zip'],
      maximumSizeInBytes: 500_000_000, addRandomSuffix: false, allowOverwrite: false, validUntil: NOW.getTime() + 30 * 60 * 1000 });
    expect(getPayloadFromClientToken(first.clientToken)).not.toHaveProperty('onUploadCompleted');
    const second = await (await serveEndpoint(endpoints.upload, tokenRequest())).json() as { pathname: string };
    expect(second.pathname).not.toBe(first.pathname);
    expect((await db.select().from(dataSetUpload)).map((row) => row.pathname).sort()).toEqual([first.pathname, second.pathname].sort());
  });

  it('a second upload with the same token is refused: a store that keeps the token\'s rules refuses the second file', async () => {
    const db = await withUsers();
    const { clientToken } = await (await serveEndpoint(setUp(db).endpoints.upload, tokenRequest())).json() as { clientToken: string };
    // What the Blob store does with a client token (its rules are signed into it): a pathname other than the one
    // signed is refused, and so is a second file at it, since there is no random suffix and no overwrite.
    const stored = new Set<string>();
    const putWith = (token: string, pathname: string): 'stored' | 'refused' => {
      const rules = getPayloadFromClientToken(token);
      if (rules.pathname !== pathname || (stored.has(pathname) && rules.allowOverwrite !== true)) return 'refused';
      stored.add(rules.addRandomSuffix === true ? `${pathname}-${stored.size}` : pathname);
      return 'stored';
    };
    const { pathname } = getPayloadFromClientToken(clientToken);
    expect(putWith(clientToken, pathname)).toBe('stored');
    expect(putWith(clientToken, pathname)).toBe('refused');
    expect(putWith(clientToken, 'adapter-requests/u1/data-set-other.zip')).toBe('refused');
    expect(stored.size).toBe(1);
  });

  it('refuses a visitor who is not signed in, another site, another type of file and a file over 500 MB', async () => {
    const db = await withUsers();
    expect((await serveEndpoint(setUp(db, null).endpoints.upload, tokenRequest())).status).toBe(401);
    const { endpoints } = setUp(db);
    expect((await serveEndpoint(endpoints.upload, tokenRequest({}, { origin: 'https://evil.example', 'content-type': 'application/json' }))).status).toBe(403);
    const wrongType = await serveEndpoint(endpoints.upload, tokenRequest({ clientPayload: JSON.stringify({ contentType: 'text/csv', bytes: 10 }) }));
    expect(wrongType.status).toBe(415);
    expect((await wrongType.json()).error.message).toBe('A data set must be a zip file (application/zip); this one is text/csv.');
    const tooLarge = await serveEndpoint(endpoints.upload, tokenRequest({ clientPayload: JSON.stringify({ contentType: 'application/zip', bytes: 500_000_001 }) }));
    expect(tooLarge.status).toBe(413);
    expect((await tooLarge.json()).error.message).toBe('The zip is 500.0 MB; a data set may be at most 500.0 MB.');
  });

  it("refuses another folder, another event and a request without the file's type and size", async () => {
    const db = await withUsers();
    const { endpoints } = setUp(db);
    const elsewhere = await serveEndpoint(endpoints.upload, tokenRequest({ pathname: 'adapter-requests/u2/data-set.zip' }));
    expect(elsewhere.status).toBe(400);
    expect((await elsewhere.json()).error.message).toBe("upload the data set as adapter-requests/u1/data-set.zip, this account's folder");
    const completed = new Request(`${BASE}/api/adapter-requests/upload`, { method: 'POST', headers: { origin: BASE, 'content-type': 'application/json' },
      body: JSON.stringify({ type: 'blob.upload-completed', payload: { blob: {} } }) });
    expect((await serveEndpoint(endpoints.upload, completed)).status).toBe(400);
    expect((await serveEndpoint(endpoints.upload, tokenRequest({ clientPayload: null }))).status).toBe(400);
  });

  it('refuses an upload for a request the limits would refuse', async () => {
    const db = await withUsers();
    const { endpoints } = setUp(db);
    for (let i = 0; i < MAX_OPEN_REQUESTS; i += 1) expect((await serveEndpoint(endpoints.mine, send(aForm()))).status).toBe(201);
    const refused = await serveEndpoint(endpoints.upload, tokenRequest());
    expect(refused.status).toBe(409);
    expect((await refused.json()).error.type).toBe('RequestRefused');
  });

  it('issues at most 5 tokens per account in 24 hours, then answers 429 saying so (final review I1)', async () => {
    const db = await withUsers();
    await aUser(db, 'u2');
    let now = NOW;
    const { endpoints, who } = setUp(db, ADA, { now: () => now });
    for (let i = 0; i < MAX_UPLOADS_PER_DAY; i += 1) expect((await serveEndpoint(endpoints.upload, tokenRequest())).status).toBe(200);
    const refused = await serveEndpoint(endpoints.upload, tokenRequest());
    expect(refused.status).toBe(429);
    expect((await refused.json()).error).toMatchObject({ type: 'TooManyUploads',
      message: 'You have started 5 data set uploads in the last 24 hours, the most an account may; try again tomorrow.' });
    who.current = { ...ADA, id: 'u2', email: 'u2@example.com' };
    expect((await serveEndpoint(endpoints.upload, tokenRequest({ pathname: 'adapter-requests/u2/data-set.zip' }))).status).toBe(200);
    who.current = ADA;
    now = new Date(NOW.getTime() + 24 * 60 * 60 * 1000 + 1);
    expect((await serveEndpoint(endpoints.upload, tokenRequest())).status).toBe(200);
  });

  it('is routed by its path', () => {
    expect(endpointFor('/api/adapter-requests/upload')).toBeDefined();
    expect(endpointFor('/api/adapter-requests/upload')).not.toBe(endpointFor('/api/adapter-requests'));
  });
});

describe("the owner's data set download and request deletion", () => {
  async function withADataSet() {
    const db = await withAnUpload();
    const setup = setUp(db, ADA, { blobs: fakeStore([privateBlob()]) });
    const id = (await (await serveEndpoint(setup.endpoints.mine, send(aDataSetForm()))).json()).id as string;
    setup.who.current = OWNER;
    return { db, id, ...setup };
  }
  const del = (id: string, body: unknown = { confirm: 'delete' }) => postJson(`/api/admin/adapter-requests/${id}/delete`, body);

  it('the owner gets a link of the store that works for five minutes; anyone else the 404 of a path that does not exist', async () => {
    const { id, endpoints, who } = await withADataSet();
    const path = `/api/admin/adapter-requests/${id}/data-set`;
    const response = await serveEndpoint(endpoints.adminOne, get(path));
    expect(response.status).toBe(302);
    expect(response.headers.get('location')).toBe(`https://teststore.private.blob.vercel-storage.com/${DATA_SET_PATHNAME}?signed=1&until=${NOW.getTime() + 5 * 60 * 1000}`);
    expect(response.headers.get('cache-control')).toBe('no-store');
    for (const visitor of [ADA, null]) {
      who.current = visitor;
      const refused = await serveEndpoint(endpoints.adminOne, get(path));
      expect(refused.status).toBe(404);
      expect((await refused.json()).error.message).toBe(`the hub has no endpoint at ${path}`);
    }
  });

  it('the list and the detail show the data set; a request with typed examples has none to download, and one with a data set no rows to export', async () => {
    const { id, endpoints, who } = await withADataSet();
    const { requests } = await (await serveEndpoint(endpoints.adminList, get('/api/admin/adapter-requests'))).json();
    expect(requests[0]).toMatchObject({ id, source: 'data_set', exampleCount: 0, dataSet: { bytes: 2_500_000, rows: 330 } });
    const { request } = await (await serveEndpoint(endpoints.adminOne, get(`/api/admin/adapter-requests/${id}`))).json();
    expect(request.dataSetCounts).toEqual([{ key: 'billing', count: 110 }, { key: 'tech', count: 110 }, { key: 'none', count: 110 }]);
    const noRows = await serveEndpoint(endpoints.adminOne, get(`/api/admin/adapter-requests/${id}/export?file=examples`));
    expect(noRows.status).toBe(400);
    who.current = ADA;
    const typed = (await (await serveEndpoint(endpoints.mine, send(aForm()))).json()).id as string;
    who.current = OWNER;
    const none = await serveEndpoint(endpoints.adminOne, get(`/api/admin/adapter-requests/${typed}/data-set`));
    expect(none.status).toBe(404);
    expect((await none.json()).error.message).toBe(`the adapter request ${typed} has no data set; it has typed examples`);
  });

  it('deleting a request deletes its zip from the store', async () => {
    const { db, id, endpoints, blobs } = await withADataSet();
    const unconfirmed = await serveEndpoint(endpoints.adminOne, del(id, {}));
    expect(unconfirmed.status).toBe(400);
    expect(blobs.removed).toEqual([]);
    const response = await serveEndpoint(endpoints.adminOne, del(id));
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ deleted: id });
    expect(await db.select().from(adapterRequest)).toEqual([]);
    expect(blobs.removed).toEqual([DATA_SET_PATHNAME]);
    expect((await serveEndpoint(endpoints.adminOne, del(id))).status).toBe(404);
  });

  it('a zip that cannot be deleted keeps the request', async () => {
    const { db, id, endpoints, blobs } = await withADataSet();
    blobs.store.remove = async () => { throw new Error('Vercel Blob: the store is not available'); };
    await expect(serveEndpoint(endpoints.adminOne, del(id))).rejects.toThrow('the store is not available');
    expect(await db.select().from(adapterRequest)).toHaveLength(1);
  });

  it('only the owner may delete a request, from the hub\'s own pages', async () => {
    const { db, id, endpoints, who } = await withADataSet();
    const crossSite = new Request(`${BASE}/api/admin/adapter-requests/${id}/delete`, { method: 'POST',
      headers: { origin: 'https://evil.example', 'content-type': 'application/json' }, body: '{"confirm":"delete"}' });
    expect((await serveEndpoint(endpoints.adminOne, crossSite)).status).toBe(403);
    who.current = ADA;
    expect((await serveEndpoint(endpoints.adminOne, del(id))).status).toBe(404);
    expect(await db.select().from(adapterRequest)).toHaveLength(1);
  });

  it("deleting the account deletes its requests' zips", async () => {
    const { db, blobs } = await withADataSet();
    await deleteAccount({ db, stripe: fakeStripe({}).api, settings: testSettings() }, 'u1', blobs.store);
    expect(await db.select().from(adapterRequest)).toEqual([]);
    expect(blobs.removed).toEqual([DATA_SET_PATHNAME]);
  });

  it('deleting the account deletes every zip in its folder, also the ones no request points to (final review I1)', async () => {
    const db = await withUsers();
    const blobs = fakeStore([privateBlob('adapter-requests/u1/data-set-Never.zip'), privateBlob('adapter-requests/u2/data-set-Other.zip')]);
    await deleteAccount({ db, stripe: fakeStripe({}).api, settings: testSettings() }, 'u1', blobs.store);
    expect(blobs.removed).toEqual(['adapter-requests/u1/data-set-Never.zip']);
    expect([...blobs.held.keys()]).toEqual(['adapter-requests/u2/data-set-Other.zip']);
  });

  it('a zip that cannot be deleted keeps the account', async () => {
    const { db } = await withADataSet();
    await expect(deleteAccount({ db, stripe: fakeStripe({}).api, settings: testSettings() }, 'u1',
      { list: async () => [], remove: async () => { throw new Error('Vercel Blob: the store is not available'); } })).rejects.toThrow('the store is not available');
    expect(await db.select().from(user).where(eq(user.id, 'u1'))).toHaveLength(1);
    expect(await db.select().from(adapterRequest)).toHaveLength(1);
  });
});

describe('the notification address', () => {
  it('is required: without ADAPTER_REQUEST_NOTIFY_EMAIL the endpoints answer 500 naming it', async () => {
    const db = await withUsers();
    vi.stubEnv('ADAPTER_REQUEST_NOTIFY_EMAIL', '');
    const response = await serveEndpoint(setUp(db).endpoints.mine, send(aForm()));
    expect(response.status).toBe(500);
    expect((await response.json()).error).toMatchObject({ type: 'MissingSetting', details: { missing: ['ADAPTER_REQUEST_NOTIFY_EMAIL'] } });
    expect(await db.select().from(adapterRequest)).toEqual([]);
  });
});
