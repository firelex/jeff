// Adapter requests in the database (the adapter_request table) and the owner's view of them: sending one (within the
// per-account limits, and only once the owner has been emailed at ADAPTER_REQUEST_NOTIFY_EMAIL), the requester's own
// list, and the owner's list, detail, status, export, data set download and deletion
// (server/endpoints/adapter-requests.ts). The examples and a data set's rows are never logged and never put in the
// email. A data set itself is a zip in the private file store (data-set-store.server.ts); the row keeps where it is,
// and deleting the request deletes the zip. A request's typed examples and its data set's zip are deleted 90 days
// after it was closed (clearExpiredExamples, run once a day by server/endpoints/cron.ts), as the form promises
// (RETENTION_LINE); the request itself stays.
import { randomBytes, randomUUID } from 'node:crypto';
import { and, asc, count, desc, eq, gt, inArray, isNotNull, isNull, lt, lte, sql } from 'drizzle-orm';
import {
  ANSWER_KIND_LABELS, CLOSED_STATUSES, DATA_SET_ROOT, MAX_OPEN_REQUESTS, MAX_SENDS_PER_DAY, MAX_UPLOADS_PER_DAY, NONE_KEY, OPEN_STATUSES, optionCount, RETENTION_DAYS,
  type AdapterRequestForm, type AdminRequestDetail, type AdminRequestRow, type DataSetUpload, type Example, type ExampleSource,
  type OwnRequest, type RequestStatus, type StoredForm,
} from './adapter-request.js';
import { dataSetFolderOf, type ListedBlob, type StoredBlob } from './data-set-store.server.js';
import type { Db } from './db.server';
import { base32 } from './keys-format.js';
import { adapterRequestEmail, type SendMail } from './mail.server.js';
import { adapterRequest, dataSetUpload, user } from './schema.js';

const DAY_MS = 24 * 60 * 60 * 1000;

// A refusal the requester can act on (a limit reached), shown on the page as it is.
export class RequestRefused extends Error {}

// The request number: AR- and eight characters of base32 in capitals (40 random bits).
export function newRequestId(): string {
  return `AR-${base32(randomBytes(5)).toUpperCase()}`;
}

// The page of a request on /admin/requests, which the owner's email links to.
export function adminUrlOf(baseUrl: string, id: string): string {
  return `${baseUrl}/admin/requests?id=${encodeURIComponent(id)}`;
}

type Stored = StoredForm;

export interface SendDeps { db: Db; sendMail: SendMail; notifyEmail: string; baseUrl: string; now: Date }

type Tx = Parameters<Parameters<Db['transaction']>[0]>[0];

// The per-account limits (open requests, sends in 24 hours), refused in the requester's words. Run under the lock on
// the user's row when sending; the data set upload runs it too, so a zip is not uploaded for a request that would be
// refused.
export async function checkLimits(tx: Db | Tx, userId: string, now: Date): Promise<void> {
  const [{ open }] = await tx.select({ open: count() }).from(adapterRequest)
    .where(and(eq(adapterRequest.userId, userId), inArray(adapterRequest.status, [...OPEN_STATUSES])));
  if (open >= MAX_OPEN_REQUESTS) {
    throw new RequestRefused(`You have ${open} open adapter requests, the most an account may have at once; wait until one is delivered or declined.`);
  }
  const [{ sent }] = await tx.select({ sent: count() }).from(adapterRequest)
    .where(and(eq(adapterRequest.userId, userId), gt(adapterRequest.createdAt, new Date(now.getTime() - DAY_MS))));
  if (sent >= MAX_SENDS_PER_DAY) {
    throw new RequestRefused(`You have sent ${sent} adapter requests in the last 24 hours, the most an account may; try again tomorrow.`);
  }
}

// Stores a checked request and emails the notification address, in one transaction under a lock on the user's row,
// so two sends at once cannot pass the limits, and a failed email leaves nothing stored (the error goes to the
// caller). `blob` is the data set's zip as the store has it (checked by the endpoint), null for typed examples.
export async function sendRequest(deps: SendDeps, requester: { id: string; email: string }, form: AdapterRequestForm, blob: StoredBlob | null): Promise<string> {
  const { db, now } = deps;
  if ((form.source === 'data_set') !== (blob !== null)) throw new Error(`a ${form.source} request was sent ${blob === null ? 'without' : 'with'} a stored data set`);
  return db.transaction(async (tx) => {
    const [locked] = await tx.select({ id: user.id }).from(user).where(eq(user.id, requester.id)).for('update');
    if (locked === undefined) throw new Error(`the signed-in user ${requester.id} has no user row`);
    await checkLimits(tx, requester.id, now);
    const id = newRequestId();
    const { examples, dataSet, rights, source: _source, ...stored } = form;
    await tx.insert(adapterRequest).values({
      id, userId: requester.id, status: 'received', form: stored, exampleSource: form.source, examples,
      dataSetPathname: dataSet?.pathname ?? null, dataSetUrl: blob?.url ?? null, dataSetBytes: dataSet?.bytes ?? null,
      dataSetRows: dataSet?.rows ?? null, dataSetCounts: dataSet?.counts ?? null,
      mayShare: rights.mayShare, noPersonalData: rights.noPersonalData, mayImproveBase: rights.mayImproveBase,
      createdAt: now, updatedAt: now, declineReason: null,
    });
    await deps.sendMail(adapterRequestEmail(deps.notifyEmail, {
      id, requester: requester.email, question: form.decision.question, kind: form.answer.kind,
      optionCount: optionCount(form.answer), adminUrl: adminUrlOf(deps.baseUrl, id),
      examples: dataSet === null ? { source: 'generate', count: examples.length } : { source: 'data_set', bytes: dataSet.bytes, rows: dataSet.rows, counts: dataSet.counts },
    }));
    return id;
  });
}

export async function ownRequests(db: Db, userId: string): Promise<OwnRequest[]> {
  const rows = await db.select().from(adapterRequest).where(eq(adapterRequest.userId, userId)).orderBy(desc(adapterRequest.createdAt));
  return rows.map((row) => ({
    id: row.id, question: (row.form as Stored).decision.question, status: row.status as RequestStatus,
    createdAt: row.createdAt.toISOString(), updatedAt: row.updatedAt.toISOString(), declineReason: row.declineReason,
  }));
}

type Row = typeof adapterRequest.$inferSelect & { email: string };

function adminRow(row: Row): AdminRequestRow {
  const form = row.form as Stored;
  return {
    id: row.id, email: row.email, question: form.decision.question, status: row.status as RequestStatus, kind: form.answer.kind,
    source: row.exampleSource as ExampleSource, optionCount: optionCount(form.answer), exampleCount: (row.examples as Example[]).length,
    dataSet: row.dataSetBytes === null || row.dataSetRows === null ? null : { bytes: row.dataSetBytes, rows: row.dataSetRows },
    createdAt: row.createdAt.toISOString(), examplesDeletedAt: row.examplesDeletedAt === null ? null : row.examplesDeletedAt.toISOString(),
  };
}

const withEmail = (db: Db) => db.select({ request: adapterRequest, email: user.email }).from(adapterRequest)
  .innerJoin(user, eq(user.id, adapterRequest.userId));

export async function allRequests(db: Db): Promise<AdminRequestRow[]> {
  const rows = await withEmail(db).orderBy(desc(adapterRequest.createdAt));
  return rows.map(({ request, email }) => adminRow({ ...request, email }));
}

async function oneRow(db: Db, id: string): Promise<Row | null> {
  const [row] = await withEmail(db).where(eq(adapterRequest.id, id));
  return row === undefined ? null : { ...row.request, email: row.email };
}

export async function requestDetail(db: Db, id: string): Promise<AdminRequestDetail['request'] | null> {
  const row = await oneRow(db, id);
  if (row === null) return null;
  return {
    ...adminRow(row), updatedAt: row.updatedAt.toISOString(), declineReason: row.declineReason,
    closedAt: row.closedAt === null ? null : row.closedAt.toISOString(),
    form: row.form as Stored, examples: row.examples as Example[], dataSetCounts: row.dataSetCounts as DataSetUpload['counts'] | null,
    consent: { mayShare: row.mayShare, noPersonalData: row.noPersonalData, mayImproveBase: row.mayImproveBase },
  };
}

// Sets a request's status; a declined one needs its reason, and any other status has none. Moving it to delivered or
// declined closes it: closed_at is set to now, unless it was already closed (delivered and then declined keeps the
// first time, so the 90 days are not restarted); moving it back to an open status clears closed_at. Returns false
// when there is no such request.
export async function setStatus(db: Db, id: string, status: RequestStatus, declineReason: string | null, now: Date): Promise<boolean> {
  if ((status === 'declined') !== (declineReason !== null)) {
    throw new Error(status === 'declined' ? 'a declined request needs its reason' : `a ${status} request has no decline reason`);
  }
  const closedAt = CLOSED_STATUSES.includes(status) ? sql`coalesce(${adapterRequest.closedAt}, ${now.toISOString()}::timestamptz)` : null;
  const updated = await db.update(adapterRequest).set({ status, declineReason, updatedAt: now, closedAt })
    .where(eq(adapterRequest.id, id)).returning({ id: adapterRequest.id });
  return updated.length > 0;
}

// Where a request's data set is in the store: its pathname, or null when the request has none (it has typed
// examples), or the time its examples were deleted when they were. Undefined when there is no such request.
export async function dataSetPathnameOf(db: Db, id: string): Promise<string | null | { examplesDeletedAt: Date } | undefined> {
  const [row] = await db.select({ pathname: adapterRequest.dataSetPathname, examplesDeletedAt: adapterRequest.examplesDeletedAt })
    .from(adapterRequest).where(eq(adapterRequest.id, id));
  if (row === undefined) return undefined;
  if (row.examplesDeletedAt !== null) return { examplesDeletedAt: row.examplesDeletedAt };
  return row.pathname;
}

// --- Deleting the examples 90 days after a request was closed. ---

export interface ClearedExamples {
  // The requests whose examples (and data set's zip) were deleted by this run.
  cleared: string[];
  // The requests whose examples could not be deleted, each with why; their examples are left as they were.
  failed: { id: string; message: string }[];
}

// For every request closed RETENTION_DAYS or more days before `now` that still holds its examples: deletes its typed
// examples and its data set's zip, and records when. The request's row, form, status and the data set's size and
// rows stay. Each request is done in a transaction of its own, under a lock on its row and checked again under it: the
// row is changed first and the zip deleted last, so a zip that cannot be deleted leaves that request as it was; it is
// listed in `failed` and the others are still done. A second run finds nothing left to do.
export async function clearExpiredExamples(db: Db, removeBlobs: (pathnames: string[]) => Promise<void>, now: Date): Promise<ClearedExamples> {
  const cutoff = new Date(now.getTime() - RETENTION_DAYS * DAY_MS);
  const due = await db.select({ id: adapterRequest.id }).from(adapterRequest)
    .where(and(lte(adapterRequest.closedAt, cutoff), isNull(adapterRequest.examplesDeletedAt))).orderBy(asc(adapterRequest.closedAt));
  const result: ClearedExamples = { cleared: [], failed: [] };
  for (const { id } of due) {
    try {
      const done = await db.transaction(async (tx) => {
        const [row] = await tx.select({ pathname: adapterRequest.dataSetPathname, closedAt: adapterRequest.closedAt, examplesDeletedAt: adapterRequest.examplesDeletedAt })
          .from(adapterRequest).where(eq(adapterRequest.id, id)).for('update');
        // Deleted, reopened or already cleared since the list was read (another run at the same time).
        if (row === undefined || row.examplesDeletedAt !== null || row.closedAt === null || row.closedAt > cutoff) return false;
        await tx.update(adapterRequest).set({ examples: [], dataSetPathname: null, dataSetUrl: null, examplesDeletedAt: now })
          .where(eq(adapterRequest.id, id));
        if (row.pathname !== null) await removeBlobs([row.pathname]);
        return true;
      });
      if (done) result.cleared.push(id);
    } catch (error) {
      // The caller asked for this: one request's failure is reported, by its number, and the others are still done.
      result.failed.push({ id, message: error instanceof Error ? error.message : String(error) });
    }
  }
  return result;
}

// Records one data set upload token for the user and returns the one pathname it may create, in the account's folder
// (adapter-requests/<user id>/data-set-<16 random base32 characters>.zip; re-review R1), or refuses with UploadsRefused
// when the account has had MAX_UPLOADS_PER_DAY in the last 24 hours (final review I1). The count and the new row are
// one transaction under the lock on the user's row, so two tabs at once cannot both pass; rows older than a day, of
// any account, go in it too.
export class UploadsRefused extends Error {}

export async function claimUpload(db: Db, userId: string, now: Date): Promise<string> {
  const dayAgo = new Date(now.getTime() - DAY_MS);
  return db.transaction(async (tx) => {
    const locked = await tx.select({ id: user.id }).from(user).where(eq(user.id, userId)).for('update');
    if (locked.length === 0) throw new Error(`there is no user ${userId} to issue an upload token to`);
    await tx.delete(dataSetUpload).where(lt(dataSetUpload.issuedAt, dayAgo));
    const [{ issued }] = await tx.select({ issued: count() }).from(dataSetUpload)
      .where(and(eq(dataSetUpload.userId, userId), gt(dataSetUpload.issuedAt, dayAgo)));
    if (issued >= MAX_UPLOADS_PER_DAY) {
      throw new UploadsRefused(`You have started ${issued} data set uploads in the last 24 hours, the most an account may; try again tomorrow.`);
    }
    const pathname = `${dataSetFolderOf(userId)}data-set-${base32(randomBytes(10))}.zip`;
    await tx.insert(dataSetUpload).values({ id: randomUUID(), userId, pathname, issuedAt: now });
    return pathname;
  });
}

// How long after its token was issued a zip may still be named by a request: an hour less than ORPHAN_ZIP_HOURS, so
// a zip a request claims was uploaded (after its token was issued) less than a day before, and the daily job, which
// deletes only zips no request points to and 24 hours old, can never take it while the request is being stored, even
// with the file store's clock an hour from the function's (re-review R2).
export const CLAIMABLE_HOURS = 23;

// Why a request may not name the zip at `pathname`, or null when it may: the pathname must have been issued to this
// account (claimUpload) less than CLAIMABLE_HOURS ago.
export async function unclaimableReason(db: Db, userId: string, pathname: string, now: Date): Promise<string | null> {
  const [row] = await db.select({ issuedAt: dataSetUpload.issuedAt }).from(dataSetUpload)
    .where(and(eq(dataSetUpload.userId, userId), eq(dataSetUpload.pathname, pathname)));
  if (row === undefined) return 'The data set was not uploaded from this account\'s request page; upload it again from this page.';
  if (row.issuedAt.getTime() <= now.getTime() - CLAIMABLE_HOURS * 60 * 60 * 1000) {
    return `The data set was uploaded more than ${CLAIMABLE_HOURS} hours ago, and is deleted soon; upload it again.`;
  }
  return null;
}

// How old a zip that no request points to must be before the daily job deletes it: well past the upload token's
// life, and an hour past CLAIMABLE_HOURS, so a zip a request may still claim is never taken.
export const ORPHAN_ZIP_HOURS = 24;

// The zips under adapter-requests/ that no request points to and that were uploaded ORPHAN_ZIP_HOURS or more ago
// (an upload whose form was refused or never sent), deleted from the file store; returns their pathnames (final
// review I1). Run once a day by server/endpoints/cron.ts.
export async function sweepOrphanDataSets(db: Db, store: { list(prefix: string): Promise<ListedBlob[]>; remove(pathnames: string[]): Promise<void> },
  now: Date): Promise<string[]> {
  const cutoff = now.getTime() - ORPHAN_ZIP_HOURS * 60 * 60 * 1000;
  const stored = await store.list(`${DATA_SET_ROOT}/`);
  const rows = await db.select({ pathname: adapterRequest.dataSetPathname }).from(adapterRequest).where(isNotNull(adapterRequest.dataSetPathname));
  const used = new Set(rows.map((row) => row.pathname));
  const orphans = stored.filter((blob) => !used.has(blob.pathname) && blob.uploadedAt.getTime() <= cutoff).map((blob) => blob.pathname);
  await store.remove(orphans);
  return orphans;
}

// Every zip of a user: the ones their requests point to and everything else in their folder of the file store
// (uploads that never became a request). Account deletion deletes them all (billing.server.ts; final review I1).
export async function dataSetPathnamesOfUserFolder(db: Db | Tx, store: { list(prefix: string): Promise<ListedBlob[]> }, userId: string): Promise<string[]> {
  const inFolder = (await store.list(dataSetFolderOf(userId))).map((blob) => blob.pathname);
  return [...new Set([...(await dataSetPathnamesOfUser(db, userId)), ...inFolder])];
}

// Deletes a request and its data set's zip, in one transaction: the row first, then the zip, so a failure to delete
// the zip leaves the request as it was (the error goes to the caller). Returns false when there is no such request.
export async function deleteRequest(db: Db, removeBlobs: (pathnames: string[]) => Promise<void>, id: string): Promise<boolean> {
  return db.transaction(async (tx) => {
    const deleted = await tx.delete(adapterRequest).where(eq(adapterRequest.id, id)).returning({ pathname: adapterRequest.dataSetPathname });
    if (deleted.length === 0) return false;
    const pathnames = deleted.flatMap((row) => (row.pathname === null ? [] : [row.pathname]));
    await removeBlobs(pathnames);
    return true;
  });
}

// The data sets' zips of every request of a user (account deletion deletes them, billing.server.ts).
export async function dataSetPathnamesOfUser(db: Db | Tx, userId: string): Promise<string[]> {
  const rows = await db.select({ pathname: adapterRequest.dataSetPathname }).from(adapterRequest).where(eq(adapterRequest.userId, userId));
  return rows.flatMap((row) => (row.pathname === null ? [] : [row.pathname]));
}

// --- The export: the examples as training rows, and a manifest skeleton. ---

// The training data sets' row (one per example): the id, the state (the context fields first, the input last, as
// the shared part of a request comes before the part that changes), the question, the label and the target.
// "Yes or no" with only yes and no is a probability question (noul: the target is 1 for yes, 0 for no); every other
// kind is a choice (the target is the key, or for several options one 1-or-0 per option in the criteria's order).
export interface TrainingRow {
  id: string;
  state: Record<string, unknown>;
  question: { type: 'choice' | 'noul'; instructions: string; criteria: Record<string, string>; subset?: true };
  label: string | string[];
  target: string | number | number[];
}

export function trainingRows(id: string, form: Stored, examples: Example[]): TrainingRow[] {
  const width = String(examples.length).length;
  return examples.map((example, i) => {
    const options = form.answer.kind === 'caller' ? example.options! : form.answer.options;
    const criteria = Object.fromEntries(options.map((o) => [o.key, o.definition]));
    const keys = options.map((o) => o.key);
    const state = { ...example.context, input: example.input };
    const rowId = `${id}-${String(i + 1).padStart(width, '0')}`;
    const instructions = form.decision.question;
    if (form.answer.kind === 'several') {
      const chosen = example.answer as string[];
      return { id: rowId, state, question: { type: 'choice', instructions, criteria, subset: true }, label: chosen,
        target: keys.map((key) => (chosen.includes(key) ? 1 : 0)) };
    }
    const answer = example.answer as string;
    if (form.answer.kind === 'yes_no' && keys.length === 2) {
      return { id: rowId, state, question: { type: 'noul', instructions, criteria }, label: answer, target: answer === 'yes' ? 1 : 0 };
    }
    return { id: rowId, state, question: { type: 'choice', instructions, criteria }, label: answer, target: answer };
  });
}

export function trainingJsonl(id: string, form: Stored, examples: Example[]): string {
  return trainingRows(id, form, examples).map((row) => JSON.stringify(row)).join('\n') + '\n';
}

// The manifest skeleton the catalogue's manifests start from: the context, the input and the one function's
// options. What only the owner can decide (the adapter's id, its interface and the base it is trained on) is null,
// and listed under to_fill.
export function manifestDraft(id: string, form: Stored, consent: AdminRequestDetail['request']['consent']) {
  const inputDescription = form.input.form === 'text'
    ? `One text, typically ${form.input.typicalLength} characters and at most ${form.input.maxLength}, in ${form.input.languages}.`
    : `Named fields, typically ${form.input.typicalLength} characters together and at most ${form.input.maxLength}, in ${form.input.languages}.`;
  const fixed = form.answer.kind !== 'caller';
  return {
    request: id,
    id: null,
    version: '0.1.0',
    licence: { kind: 'proprietary' },
    to_fill: ['id', 'functions.decide.interface', 'base'],
    base: null,
    context: form.context.map((field) => ({ key: field.name, description: field.description, required: true, example: field.example })),
    input: {
      key: 'input', description: inputDescription, form: form.input.form,
      ...(form.input.form === 'fields' ? { fields: form.input.fields.map((f) => ({ key: f.name, description: f.description })) } : {}),
    },
    functions: {
      decide: {
        interface: null,
        type: form.answer.kind === 'yes_no' && form.answer.options.length === 2 ? 'noul' : 'choice',
        instructions: form.decision.question,
        why: form.decision.why,
        answer_kind: ANSWER_KIND_LABELS[form.answer.kind],
        labels: fixed
          ? { source: 'fixed', fixed: Object.fromEntries(form.answer.options.map((o) => [o.key, o.definition])), subset: form.answer.kind === 'several' }
          : { source: 'caller', fixed_first: {}, caller_keys: null, max: Math.max(...form.answer.exampleSets.map((s) => s.length)), example_sets: form.answer.exampleSets },
        when_none_fits: form.answer.noneFits === 'none_option' ? `the option "${NONE_KEY}"` : 'abstain',
        abstain_labels: [],
      },
    },
    use: form.use,
    consent,
  };
}
