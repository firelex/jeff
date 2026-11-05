// The hub's scheduled job, which Vercel's cron runs once a day (vercel.json's "crons"):
// - GET /api/cron/clear-examples: deletes the typed examples and the data set's zip of every adapter request closed
//   (delivered or declined) 90 or more days ago, as the request form and the privacy policy promise
//   (app/lib/adapter-requests.server.ts: clearExpiredExamples). Answers 200 with {"cleared": [<request numbers>]}.
//   When a request's examples cannot be deleted (its zip could not be deleted from the file store), that request is
//   left as it was, the others are still done, the failure is logged naming the request, and the answer is 500 with
//   {"error": {"type": "ExamplesNotDeleted", "details": {"cleared", "failed"}}}, so Vercel's cron log shows it.
//   The same job then deletes every zip under adapter-requests/ that no request points to and that is 24 hours old or
//   more (an upload whose form was refused or never sent: sweepOrphanDataSets, final review I1); the answer's
//   "swept" counts them. A failure there fails the job.
// Vercel's cron sends `Authorization: Bearer <CRON_SECRET>`, the project's setting CRON_SECRET; it is compared in
// constant time, and any other request (no header, another secret, another method) gets the same 404 an unknown path
// gets, so the job looks like no endpoint to anyone else. Nothing here logs an example or the secret.
import { createHash, timingSafeEqual } from 'node:crypto';
import { ACCOUNTS_SETTING_NAMES } from '../../app/lib/accounts-settings.server.js';
import { clearExpiredExamples, sweepOrphanDataSets } from '../../app/lib/adapter-requests.server.js';
import { BLOB_TOKEN_SETTING, liveDataSetStore, type DataSetStore } from '../../app/lib/data-set-store.server.js';
import { getDb, type Db } from '../../app/lib/db.server.js';
import { defineEndpoint, type Handler } from '../endpoint.js';
import { ApiError, json } from '../http.js';
import { notFound } from '../owner.js';

export const CLEAR_EXAMPLES_PATH = '/api/cron/clear-examples';
export const CRON_SECRET_SETTING = 'CRON_SECRET';
// Vercel's advice for CRON_SECRET: a random string of at least 16 characters.
export const CRON_SECRET_MIN_LENGTH = 16;

// What the job runs against; tests pass a database and a file store of their own.
export interface CronContext {
  db(): Promise<Db>;
  dataSets: DataSetStore;
  now(): Date;
}

const live: CronContext = { db: getDb, dataSets: liveDataSetStore, now: () => new Date() };

// CRON_SECRET as the job reads it: at least CRON_SECRET_MIN_LENGTH characters (settings.server.ts checks it too, when
// the site is built for production).
export function checkCronSecret(secret: string): string {
  if (secret.length < CRON_SECRET_MIN_LENGTH) {
    throw new Error(`${CRON_SECRET_SETTING} must be at least ${CRON_SECRET_MIN_LENGTH} characters (make one with: openssl rand -hex 32)`);
  }
  return secret;
}

const digest = (text: string) => createHash('sha256').update(text, 'utf8').digest();

// Vercel's cron request, or the 404 of a path the hub does not have. Both sides are hashed first, so the comparison
// takes the same time whatever the header's length.
function requireCron(request: Request, secret: string): void {
  const given = request.headers.get('authorization');
  if (given === null || !timingSafeEqual(digest(given), digest(`Bearer ${checkCronSecret(secret)}`))) {
    throw notFound(new URL(request.url).pathname);
  }
}

const SETTINGS = [CRON_SECRET_SETTING, ...ACCOUNTS_SETTING_NAMES, BLOB_TOKEN_SETTING] as const;
type Name = (typeof SETTINGS)[number];

export function cronEndpoints(context: CronContext) {
  const elsewhere: Handler<Name> = (request) => { throw notFound(new URL(request.url).pathname); };
  return {
    clearExamples: defineEndpoint({
      settings: SETTINGS,
      hidesMissingSettings: true,
      methods: {
        GET: async (request, settings) => {
          requireCron(request, settings.CRON_SECRET);
          const db = await context.db();
          const { cleared, failed } = await clearExpiredExamples(db, (pathnames) => context.dataSets.remove(pathnames), context.now());
          const swept = await sweepOrphanDataSets(db, context.dataSets, context.now());
          for (const { id, message } of failed) console.error(`the examples of the adapter request ${id} were not deleted, and are kept: ${message}`);
          if (failed.length > 0) {
            throw new ApiError(500, 'ExamplesNotDeleted',
              `the examples of ${failed.length === 1 ? 'the adapter request' : 'the adapter requests'} ${failed.map((f) => f.id).join(', ')} were not deleted: ${failed.map((f) => `${f.id}: ${f.message}`).join('; ')}`,
              { cleared, failed });
          }
          return json(200, { cleared, swept: swept.length });
        },
        POST: elsewhere, PUT: elsewhere, PATCH: elsewhere, DELETE: elsewhere,
      },
    }),
  };
}

export const cronEndpoint = cronEndpoints(live);
