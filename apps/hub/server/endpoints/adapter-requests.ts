// Adapter requests (app/lib/adapter-requests.server.ts), for the /request-adapter, /account and /admin/requests pages:
// - POST /api/adapter-requests with the form as JSON: checked with the form's own rules (app/lib/adapter-request.ts),
//   within the per-account limits (3 open, 5 sent in 24 hours), stored, and emailed to ADAPTER_REQUEST_NOTIFY_EMAIL;
//   answers 201 with {"id"}. A failed email fails the request, and nothing is stored. With a data set, the zip the
//   browser uploaded is checked again first, without unzipping it: it is in the store, private, in this account's
//   folder, and as large as the request says (at most 500 MB); a request that then fails deletes the zip.
// - POST /api/adapter-requests/upload: @vercel/blob's client upload asks here for the token that lets the browser
//   upload a data set's zip straight to the private file store (handleUpload's "blob.generate-client-token" event,
//   nothing else). Only into this account's folder, only application/zip, at most 500 MB, within the limits, and at
//   most 5 tokens per account in 24 hours (429 after that; counted in the database, final review I1). Each token may
//   create one file, at a pathname the server picks and answers ({"type", "clientToken", "pathname"}), for 30 minutes
//   (re-review R1); a request may name only a zip issued so to its account in the last 23 hours (R2).
// - GET  /api/adapter-requests: the signed-in visitor's own requests, newest first, and their data set folder.
// - GET  /api/admin/adapter-requests: every request, newest first.
// - GET  /api/admin/adapter-requests/<id>: one request in full.
// - POST /api/admin/adapter-requests/<id>/status with {"status", "declineReason"}: moves it on.
// - POST /api/admin/adapter-requests/<id>/delete with {"confirm": "delete"}: deletes it, and its data set's zip.
// - GET  /api/admin/adapter-requests/<id>/export?file=examples (the typed examples as training rows, JSON Lines) or
//   ?file=manifest (the manifest skeleton, JSON), as a download.
// - GET  /api/admin/adapter-requests/<id>/data-set: the data set's zip, through a link of the file store that works
//   for five minutes (a redirect to it).
// Once a request's examples were deleted (90 days after it was closed, server/endpoints/cron.ts), the examples export
// and the data set download answer 410, saying when.
// Every one needs a signed-in visitor (401 otherwise); the admin ones answer only the owner (the session's email is
// OWNER_EMAIL), and anyone else, signed in or not, gets the same 404 an unknown path gets. Every POST must come from
// the hub's own pages (403 otherwise, checked before the session is read) and be JSON (415 otherwise).
import { handleUpload, type HandleUploadBody } from '@vercel/blob/client';
import {
  ACCOUNTS_SETTING_NAMES, accountsSettings, REQUEST_MAIL_SETTING_NAMES, requestMailSettings, requestNotifyEmail, type AccountsSettings,
} from '../../app/lib/accounts-settings.server.js';
import {
  checkAdapterRequest, DATA_SET_CONTENT_TYPE, DATA_SET_NAME, LIMITS, megabytes, REQUEST_STATUSES,
  type AdminRequestDetail, type AdminRequests, type CreatedRequest, type DataSetUpload, type UploadToken, type OwnRequests, type RequestStatus,
} from '../../app/lib/adapter-request.js';
import {
  allRequests, checkLimits, claimUpload, dataSetPathnameOf, unclaimableReason, deleteRequest, manifestDraft, ownRequests, RequestRefused, requestDetail, sendRequest, setStatus,
  trainingJsonl, UploadsRefused,
} from '../../app/lib/adapter-requests.server.js';
import {
  dataSetFolderOf, DOWNLOAD_LINK_SECONDS, isPrivateBlobUrl, liveDataSetStore, type DataSetStore, type StoredBlob,
} from '../../app/lib/data-set-store.server.js';
import { getDb, type Db } from '../../app/lib/db.server.js';
import { mailerFor, type SendMail } from '../../app/lib/mail.server.js';
import { requireUser, type SessionUser } from '../../app/lib/session.server.js';
import { defineEndpoint } from '../endpoint.js';
import { ApiError, json } from '../http.js';
import { notFound, requireOwner } from '../owner.js';
import { requireSameOrigin } from '../same-origin.js';
import { isJson } from './account.js';

export interface RequestContext {
  store(): Promise<{ db: Db; settings: AccountsSettings }>;
  // The mailer the new-request email goes through (SendGrid, or the test mailbox in tests), and where it goes.
  mailer(): SendMail;
  notifyEmail(): string;
  // The private file store of the data sets (Vercel Blob; a stand-in in tests).
  dataSets: DataSetStore;
  signedIn(request: Request): Promise<SessionUser>;
  now(): Date;
}

const live: RequestContext = {
  store: async () => ({ db: await getDb(), settings: accountsSettings() }),
  mailer: () => mailerFor(requestMailSettings(), fetch),
  notifyEmail: requestNotifyEmail,
  dataSets: liveDataSetStore,
  signedIn: requireUser,
  now: () => new Date(),
};

const SETTINGS = [...ACCOUNTS_SETTING_NAMES, ...REQUEST_MAIL_SETTING_NAMES] as const;
export const ADMIN_PREFIX = '/api/admin/adapter-requests/';
// How long the browser's upload token works (re-review R1). @vercel/blob checks it on every request of an upload,
// each part of a multipart one included, so it must outlast the whole upload: 30 minutes carries the largest zip,
// 500 MB, at about 2.3 Mbit/s or faster. With one pathname per token and no overwrite, a longer life cannot buy
// more files, only a slower upload.
export const UPLOAD_TOKEN_MS = 30 * 60 * 1000;

// The answer for a request whose examples were deleted, 90 days after it was closed.
const examplesDeleted = (id: string, at: string) => new ApiError(410, 'ExamplesDeleted',
  `the examples of the adapter request ${id} were deleted on ${at.slice(0, 10)}, 90 days after it was closed`, { id, examplesDeletedAt: at });

// A request number from the admin path: its segment decoded, or the 404 of a path the hub does not have when the
// segment's % escapes are not valid (final review M4).
function requestIdOf(segment: string, pathname: string): string {
  try {
    return decodeURIComponent(segment);
  } catch (error) {
    if (error instanceof URIError) throw notFound(pathname);
    throw error;
  }
}

const isObject = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value);

// The data set's zip as the store has it, checked against the request without unzipping it: in this account's
// folder, in the store, private, and of the size the request says (at most 500 MB). A zip of this account's folder
// that is refused (public, too large, or not the size the request says) is deleted first, as no request will use it.
async function storedDataSet(db: Db, store: DataSetStore, userId: string, dataSet: DataSetUpload, now: Date): Promise<StoredBlob> {
  const refuse = (message: string) => new ApiError(400, 'InvalidRequest', message, { field: 'dataSet' });
  if (!dataSet.pathname.startsWith(dataSetFolderOf(userId))) throw refuse('The data set was not uploaded by this account; upload it again from this page.');
  // Only a pathname the upload endpoint issued to this account, and not so old the daily job may delete it.
  const unclaimable = await unclaimableReason(db, userId, dataSet.pathname, now);
  if (unclaimable !== null) throw refuse(unclaimable);
  const blob = await store.find(dataSet.pathname);
  if (blob === null || blob.pathname !== dataSet.pathname) throw refuse('The uploaded data set is not in the store; upload it again.');
  const refused = async (error: ApiError): Promise<never> => {
    await store.remove([blob.pathname]);
    throw error;
  };
  if (!isPrivateBlobUrl(blob.url)) await refused(refuse('The data set was not uploaded as a private file; upload it again from this page.'));
  if (blob.size > LIMITS.dataSetBytesMax) {
    await refused(new ApiError(413, 'RequestTooLarge', `The data set is ${megabytes(blob.size)}; it may be at most ${megabytes(LIMITS.dataSetBytesMax)}.`, { field: 'dataSet' }));
  }
  if (blob.size !== dataSet.bytes) await refused(refuse(`The stored data set is ${blob.size.toLocaleString('en-US')} bytes, but the request says ${dataSet.bytes.toLocaleString('en-US')}; upload it again.`));
  return blob;
}

// The body of a POST as JSON, refused when it is not JSON or is larger than a request may be.
async function bodyOf(request: Request, example: string): Promise<unknown> {
  if (!isJson(request.headers.get('content-type'))) {
    throw new ApiError(415, 'UnsupportedMediaType', `send the request as JSON: ${example} with content-type application/json`);
  }
  const bytes = await request.arrayBuffer();
  if (bytes.byteLength > LIMITS.bodyBytesMax) {
    throw new ApiError(413, 'RequestTooLarge', `The request is ${(bytes.byteLength / 1_000_000).toFixed(1)} MB; it may be at most ${LIMITS.bodyBytesMax / 1_000_000} MB. Send fewer or shorter examples.`,
      { bytes: bytes.byteLength, limit: LIMITS.bodyBytesMax });
  }
  try {
    return JSON.parse(new TextDecoder().decode(bytes));
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
    throw new ApiError(400, 'InvalidRequest', `the body is not JSON: ${error.message}`);
  }
}

export function adapterRequestEndpoints(context: RequestContext) {
  // The owner, or the 404: a visitor who is not signed in gets it too.
  const owner = (request: Request, settings: AccountsSettings): Promise<SessionUser> => requireOwner(request, settings.ownerEmail, context.signedIn);

  return {
    mine: defineEndpoint({
      settings: SETTINGS,
      methods: {
        GET: async (request) => {
          const user = await context.signedIn(request);
          const { db } = await context.store();
          const body: OwnRequests = { requests: await ownRequests(db, user.id), dataSetFolder: dataSetFolderOf(user.id) };
          return json(200, body);
        },
        POST: async (request) => {
          const { db, settings } = await context.store();
          requireSameOrigin(request, settings.baseUrl);
          const user = await context.signedIn(request);
          const checked = checkAdapterRequest(await bodyOf(request, 'the request form'));
          if (!checked.ok) {
            const [first] = checked.problems;
            const more = checked.problems.length - 1;
            throw new ApiError(400, 'InvalidRequest', more === 0 ? first.message : `${first.message} (and ${more} more ${more === 1 ? 'problem' : 'problems'})`,
              { problems: checked.problems });
          }
          const blob = checked.form.dataSet === null ? null : await storedDataSet(db, context.dataSets, user.id, checked.form.dataSet, context.now());
          let id: string;
          try {
            id = await sendRequest({ db, sendMail: context.mailer(), notifyEmail: context.notifyEmail(), baseUrl: settings.baseUrl, now: context.now() },
              { id: user.id, email: user.email }, checked.form, blob);
          } catch (error) {
            // Nothing was stored, so the zip belongs to no request: it goes too, and the error is the request's.
            if (blob !== null) await context.dataSets.remove([blob.pathname]);
            if (error instanceof RequestRefused) throw new ApiError(409, 'RequestRefused', error.message);
            throw error;
          }
          const body: CreatedRequest = { id };
          return json(201, body);
        },
      },
    }),
    upload: defineEndpoint({
      settings: SETTINGS,
      methods: {
        POST: async (request) => {
          const { db, settings } = await context.store();
          requireSameOrigin(request, settings.baseUrl);
          const user = await context.signedIn(request);
          const body = await bodyOf(request, 'the upload library\'s token request');
          if (!isObject(body) || body.type !== 'blob.generate-client-token' || !isObject(body.payload)) {
            throw new ApiError(400, 'InvalidRequest', 'this endpoint only issues tokens to upload a data set (the event "blob.generate-client-token")');
          }
          const expected = `${dataSetFolderOf(user.id)}${DATA_SET_NAME}`;
          if (body.payload.pathname !== expected) {
            throw new ApiError(400, 'InvalidRequest', `upload the data set as ${expected}, this account's folder`, { field: 'pathname', expected });
          }
          const payloadShape = 'say the file\'s type and size: clientPayload must be {"contentType": "application/zip", "bytes": <size>}';
          let file: unknown;
          try {
            file = typeof body.payload.clientPayload === 'string' ? JSON.parse(body.payload.clientPayload) : null;
          } catch (error) {
            if (!(error instanceof SyntaxError)) throw error;
            throw new ApiError(400, 'InvalidRequest', `${payloadShape}; it is not JSON: ${error.message}`, { field: 'clientPayload' });
          }
          if (!isObject(file) || typeof file.contentType !== 'string' || typeof file.bytes !== 'number' || !Number.isInteger(file.bytes) || file.bytes < 1) {
            throw new ApiError(400, 'InvalidRequest', payloadShape, { field: 'clientPayload' });
          }
          if (file.contentType !== DATA_SET_CONTENT_TYPE) {
            throw new ApiError(415, 'UnsupportedMediaType', `A data set must be a zip file (${DATA_SET_CONTENT_TYPE}); this one is ${file.contentType}.`, { field: 'contentType' });
          }
          if (file.bytes > LIMITS.dataSetBytesMax) {
            throw new ApiError(413, 'RequestTooLarge', `The zip is ${megabytes(file.bytes)}; a data set may be at most ${megabytes(LIMITS.dataSetBytesMax)}.`,
              { bytes: file.bytes, limit: LIMITS.dataSetBytesMax });
          }
          const now = context.now();
          try {
            await checkLimits(db, user.id, now);
          } catch (error) {
            if (error instanceof RequestRefused) throw new ApiError(409, 'RequestRefused', error.message);
            throw error;
          }
          let pathname: string;
          try {
            pathname = await claimUpload(db, user.id, now);
          } catch (error) {
            if (error instanceof UploadsRefused) throw new ApiError(429, 'TooManyUploads', error.message);
            throw error;
          }
          // The token is signed for the pathname the server chose, with no random suffix and no overwrite, so it can
          // create exactly one file (re-review R1); the page uploads to the pathname in the answer.
          const answer = await handleUpload({
            token: context.dataSets.readWriteToken(),
            request,
            body: { ...body, payload: { ...body.payload, pathname } } as unknown as HandleUploadBody,
            onBeforeGenerateToken: async () => ({
              allowedContentTypes: [DATA_SET_CONTENT_TYPE],
              maximumSizeInBytes: LIMITS.dataSetBytesMax,
              addRandomSuffix: false,
              allowOverwrite: false,
              validUntil: now.getTime() + UPLOAD_TOKEN_MS,
            }),
          });
          const issued: UploadToken = { ...(answer as { type: 'blob.generate-client-token'; clientToken: string }), pathname };
          return json(200, issued);
        },
      },
    }),
    adminList: defineEndpoint({
      settings: SETTINGS,
      methods: {
        GET: async (request) => {
          const { db, settings } = await context.store();
          await owner(request, settings);
          const body: AdminRequests = { requests: await allRequests(db) };
          return json(200, body);
        },
      },
    }),
    // /api/admin/adapter-requests/<id>, /<id>/status and /<id>/export.
    adminOne: defineEndpoint({
      settings: SETTINGS,
      methods: {
        GET: async (request) => {
          const { db, settings } = await context.store();
          await owner(request, settings);
          const url = new URL(request.url);
          const [id, action, ...rest] = url.pathname.slice(ADMIN_PREFIX.length).split('/');
          if (id === '' || rest.length > 0 || (action !== undefined && action !== 'export' && action !== 'data-set')) throw notFound(url.pathname);
          const decodedId = requestIdOf(id, url.pathname);
          if (action === 'data-set') {
            const pathname = await dataSetPathnameOf(db, decodedId);
            if (pathname === undefined) throw new ApiError(404, 'NotFound', `there is no adapter request ${decodedId}`, { id: decodedId });
            if (typeof pathname === 'object' && pathname !== null) throw examplesDeleted(decodedId, pathname.examplesDeletedAt.toISOString());
            if (pathname === null) throw new ApiError(404, 'NotFound', `the adapter request ${decodedId} has no data set; it has typed examples`, { id: decodedId });
            const link = await context.dataSets.downloadUrl(pathname, context.now().getTime() + DOWNLOAD_LINK_SECONDS * 1000);
            return new Response(null, { status: 302, headers: { location: link, 'cache-control': 'no-store' } });
          }
          const detail = await requestDetail(db, decodedId);
          if (detail === null) throw new ApiError(404, 'NotFound', `there is no adapter request ${decodedId}`, { id: decodedId });
          if (action === undefined) {
            const body: AdminRequestDetail = { request: detail };
            return json(200, body);
          }
          const file = url.searchParams.get('file');
          if (file === 'examples') {
            if (detail.examplesDeletedAt !== null) throw examplesDeleted(detail.id, detail.examplesDeletedAt);
            if (detail.source === 'data_set') {
              throw new ApiError(400, 'InvalidRequest', `${detail.id} has a data set instead of typed examples; download it at ${ADMIN_PREFIX}${encodeURIComponent(detail.id)}/data-set`, { field: 'file' });
            }
            return new Response(trainingJsonl(detail.id, detail.form, detail.examples), { status: 200, headers: {
              'content-type': 'application/x-ndjson; charset=utf-8', 'cache-control': 'no-store',
              'content-disposition': `attachment; filename="${detail.id}-examples.jsonl"` } });
          }
          if (file === 'manifest') {
            return new Response(`${JSON.stringify(manifestDraft(detail.id, detail.form, detail.consent), null, 2)}\n`, { status: 200, headers: {
              'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store',
              'content-disposition': `attachment; filename="${detail.id}-manifest-draft.json"` } });
          }
          throw new ApiError(400, 'InvalidRequest', `say which file to export: ?file=examples or ?file=manifest; got ${JSON.stringify(file)}`, { field: 'file' });
        },
        POST: async (request) => {
          const { db, settings } = await context.store();
          requireSameOrigin(request, settings.baseUrl);
          await owner(request, settings);
          const pathname = new URL(request.url).pathname;
          const [id, action, ...rest] = pathname.slice(ADMIN_PREFIX.length).split('/');
          if (id === '' || (action !== 'status' && action !== 'delete') || rest.length > 0) throw notFound(pathname);
          const decodedId = requestIdOf(id, pathname);
          if (action === 'delete') {
            const confirm = await bodyOf(request, '{"confirm": "delete"}');
            if (!isObject(confirm) || confirm.confirm !== 'delete') {
              throw new ApiError(400, 'ConfirmationRequired', 'send {"confirm": "delete"} to delete the request and its data set', { field: 'confirm' });
            }
            if (!(await deleteRequest(db, (pathnames) => context.dataSets.remove(pathnames), decodedId))) {
              throw new ApiError(404, 'NotFound', `there is no adapter request ${decodedId}`, { id: decodedId });
            }
            return json(200, { deleted: decodedId });
          }
          const body = await bodyOf(request, '{"status": "in_review"}');
          const fields = typeof body === 'object' && body !== null && !Array.isArray(body) ? body as Record<string, unknown> : null;
          if (fields === null) throw new ApiError(400, 'InvalidRequest', 'send {"status": ..., "declineReason": ...} as a JSON object');
          const status = fields.status;
          if (typeof status !== 'string' || !(REQUEST_STATUSES as readonly string[]).includes(status)) {
            throw new ApiError(400, 'InvalidRequest', `the status must be one of ${REQUEST_STATUSES.join(', ')}`, { field: 'status' });
          }
          const raw = fields.declineReason;
          let declineReason: string | null = null;
          if (status === 'declined') {
            if (typeof raw !== 'string' || raw.trim() === '' || raw.trim().length > LIMITS.declineReasonMax) {
              throw new ApiError(400, 'InvalidRequest', `say why the request is declined, in 1 to ${LIMITS.declineReasonMax} characters`, { field: 'declineReason' });
            }
            declineReason = raw.trim();
          } else if (raw !== undefined && raw !== null) {
            throw new ApiError(400, 'InvalidRequest', `only a declined request has a decline reason; this one is being set to ${status}`, { field: 'declineReason' });
          }
          if (!(await setStatus(db, decodedId, status as RequestStatus, declineReason, context.now()))) {
            throw new ApiError(404, 'NotFound', `there is no adapter request ${decodedId}`, { id: decodedId });
          }
          return json(200, { id: decodedId, status });
        },
      },
    }),
  };
}

export const adapterRequestEndpoint = adapterRequestEndpoints(live);
