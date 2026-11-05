// The hub's Postgres tables (spec Section 9). The first five are Better Auth's own (user, session, account,
// verification, rate_limit), written as its CLI generates them for Postgres with the Drizzle adapter: the property names are the
// field names Better Auth uses, so do not rename them. The rest are the accounts work's.
import { sql } from 'drizzle-orm';
import { bigint, boolean, check, index, integer, jsonb, pgTable, primaryKey, text, timestamp } from 'drizzle-orm/pg-core';

const at = (name: string) => timestamp(name, { withTimezone: true, mode: 'date' });

export const user = pgTable('user', {
  id: text('id').primaryKey(),
  name: text('name').notNull(),
  email: text('email').notNull().unique(),
  emailVerified: boolean('email_verified').notNull(),
  image: text('image'),
  createdAt: at('created_at').notNull(),
  updatedAt: at('updated_at').notNull(),
});

export const session = pgTable('session', {
  id: text('id').primaryKey(),
  expiresAt: at('expires_at').notNull(),
  token: text('token').notNull().unique(),
  createdAt: at('created_at').notNull(),
  updatedAt: at('updated_at').notNull(),
  ipAddress: text('ip_address'),
  userAgent: text('user_agent'),
  userId: text('user_id').notNull().references(() => user.id, { onDelete: 'cascade' }),
}, (t) => [index('session_user').on(t.userId)]);

export const account = pgTable('account', {
  id: text('id').primaryKey(),
  accountId: text('account_id').notNull(),
  providerId: text('provider_id').notNull(),
  userId: text('user_id').notNull().references(() => user.id, { onDelete: 'cascade' }),
  accessToken: text('access_token'),
  refreshToken: text('refresh_token'),
  idToken: text('id_token'),
  accessTokenExpiresAt: at('access_token_expires_at'),
  refreshTokenExpiresAt: at('refresh_token_expires_at'),
  scope: text('scope'),
  password: text('password'),
  createdAt: at('created_at').notNull(),
  updatedAt: at('updated_at').notNull(),
}, (t) => [index('account_user').on(t.userId)]);

export const verification = pgTable('verification', {
  id: text('id').primaryKey(),
  identifier: text('identifier').notNull(),
  value: text('value').notNull(),
  expiresAt: at('expires_at').notNull(),
  createdAt: at('created_at').notNull(),
  updatedAt: at('updated_at').notNull(),
});

// Better Auth's rate-limit counts (rateLimit.storage 'database'), one row per visitor address and path, so every
// function instance shares them. The property must be named rateLimit (the Drizzle adapter finds the model by it) and
// the fields are Better Auth's: key unique (on a race to create a row, the loser's insert fails and it reads the
// row again), lastRequest in milliseconds since 1970 (too large for integer). Better Auth deletes rows whose
// lastRequest is older than its longest window, hence the index.
export const rateLimit = pgTable('rate_limit', {
  id: text('id').primaryKey(),
  key: text('key').notNull().unique(),
  count: integer('count').notNull(),
  lastRequest: bigint('last_request', { mode: 'number' }).notNull(),
}, (t) => [index('rate_limit_last_request').on(t.lastRequest)]);

// The sign-in emails sent per email address in the last hour (email-limit.server.ts: at most 5 an hour), one row per
// email. The address is kept only as the SHA-256 of its trimmed, lower-cased form; rows older than an hour are deleted.
export const signInEmail = pgTable('sign_in_email', {
  id: text('id').primaryKey(),
  addressHash: text('address_hash').notNull(),
  sentAt: bigint('sent_at', { mode: 'number' }).notNull(),   // milliseconds since 1970
}, (t) => [
  index('sign_in_email_address_sent').on(t.addressHash, t.sentAt),
  index('sign_in_email_sent').on(t.sentAt),
  check('sign_in_email_hash_form', sql`${t.addressHash} ~ '^[0-9a-f]{64}$'`),
]);

// The user's one Stripe customer, made before their first Checkout (billing.server.ts, under a lock on the user, so
// two tabs never make two) and used for every Checkout, the Customer Portal and deletion after that.
export const stripeCustomer = pgTable('stripe_customer', {
  userId: text('user_id').primaryKey().references(() => user.id, { onDelete: 'cascade' }),
  customerId: text('customer_id').notNull().unique(),
  createdAt: at('created_at').notNull(),
});

// One row per user who has started paying: the Stripe subscription as Stripe last reported it (synced from Stripe on
// every webhook, stripe-webhook.server.ts). A user who subscribes again after cancelling gets the new subscription
// in the same row.
export const subscription = pgTable('subscription', {
  userId: text('user_id').primaryKey().references(() => user.id, { onDelete: 'cascade' }),
  stripeCustomerId: text('stripe_customer_id').notNull(),
  stripeSubscriptionId: text('stripe_subscription_id').notNull().unique(),
  status: text('status').notNull(),
  currentPeriodStart: at('current_period_start').notNull(),
  currentPeriodEnd: at('current_period_end').notNull(),
  cancelAtPeriodEnd: boolean('cancel_at_period_end').notNull(),
  updatedAt: at('updated_at').notNull(),
});

export const apiKey = pgTable('api_key', {
  id: text('id').primaryKey(),
  userId: text('user_id').notNull().references(() => user.id, { onDelete: 'cascade' }),
  name: text('name').notNull(),
  hash: text('hash').notNull().unique(),     // SHA-256 of the whole key, lowercase hex; the key itself is never stored
  prefix: text('prefix').notNull(),          // the 6 characters after jeff_live_
  createdAt: at('created_at').notNull(),
  revokedAt: at('revoked_at'),
  lastUsedAt: at('last_used_at'),            // from jeff-server's usage reports
}, (t) => [
  index('api_key_user').on(t.userId),
  // The database itself refuses a malformed hash or prefix (keys-format.ts: base32 in lower case, 6 shown).
  check('api_key_hash_form', sql`${t.hash} ~ '^[0-9a-f]{64}$'`),
  check('api_key_prefix_form', sql`${t.prefix} ~ '^[a-z2-7]{6}$'`),
]);

// jeff-server's reported totals: one row per account and billing period. account_id is a user id or 'playground',
// so it has no foreign key; a deleted user's rows are deleted with the account (billing.server.ts).
export const usage = pgTable('usage', {
  accountId: text('account_id').notNull(),
  periodStart: at('period_start').notNull(),
  inputTokens: bigint('input_tokens', { mode: 'number' }).notNull(),
  updatedAt: at('updated_at').notNull(),
}, (t) => [primaryKey({ columns: [t.accountId, t.periodStart] })]);

// Every Stripe event processed, by id: a repeated delivery changes nothing.
export const stripeEvent = pgTable('stripe_event', {
  id: text('id').primaryKey(),
  type: text('type').notNull(),
  processedAt: at('processed_at').notNull(),
});

// The playground's runs per visitor address (a SHA-256 of it), kept for one minute, for the per-minute limit.
export const playgroundRun = pgTable('playground_run', {
  ipHash: text('ip_hash').notNull(),
  at: at('at').notNull(),
}, (t) => [index('playground_run_ip_at').on(t.ipHash, t.at), index('playground_run_at').on(t.at)]);

// A request for an adapter (adapter-requests.server.ts), sent from /request-adapter: the whole form but its examples
// as JSON, the examples as JSON, and the consent ticks as columns. The owner moves it through the statuses on
// /admin/requests; a declined one says why. It goes with its user.
export const adapterRequest = pgTable('adapter_request', {
  id: text('id').primaryKey(),
  userId: text('user_id').notNull().references(() => user.id, { onDelete: 'cascade' }),
  status: text('status').notNull(),
  form: jsonb('form').notNull(),
  // How the examples were given: typed examples to generate the data from (in `examples`), or a data set (a zip in
  // the private file store, whose pathname, URL, size and the rows the browser counted are kept here; `examples` is
  // then empty).
  exampleSource: text('example_source').notNull(),
  examples: jsonb('examples').notNull(),
  dataSetPathname: text('data_set_pathname').unique(),
  dataSetUrl: text('data_set_url'),
  dataSetBytes: integer('data_set_bytes'),
  dataSetRows: integer('data_set_rows'),
  dataSetCounts: jsonb('data_set_counts'),
  mayShare: boolean('may_share').notNull(),
  noPersonalData: boolean('no_personal_data').notNull(),
  mayImproveBase: boolean('may_improve_base').notNull(),
  createdAt: at('created_at').notNull(),
  updatedAt: at('updated_at').notNull(),
  declineReason: text('decline_reason'),
  // When the owner closed the request (moved it to delivered or declined); null while it is open, and cleared when
  // its status moves back. Its examples are deleted 90 days after this (adapter-requests.server.ts: clearExpiredExamples).
  closedAt: at('closed_at'),
  // When its typed examples and its data set's zip were deleted, 90 days after it was closed; null while it holds them.
  // The request itself (its form, status and the data set's size and rows) stays for the owner's history.
  examplesDeletedAt: at('examples_deleted_at'),
}, (t) => [
  index('adapter_request_user_created').on(t.userId, t.createdAt),
  index('adapter_request_created').on(t.createdAt),
  check('adapter_request_status', sql`${t.status} in ('received', 'in_review', 'training', 'delivered', 'declined')`),
  // A declined request says why; no other has a decline reason.
  check('adapter_request_decline_reason', sql`(${t.status} = 'declined') = (${t.declineReason} is not null)`),
  check('adapter_request_example_source', sql`${t.exampleSource} in ('data_set', 'generate')`),
  // A data set has all five of its columns, and only a request with a data set has any; once its examples are
  // deleted, the zip's pathname and URL are gone and its size, rows and rows per option stay.
  check('adapter_request_data_set', sql`(${t.exampleSource} = 'data_set' and ${t.examplesDeletedAt} is null) = (${t.dataSetPathname} is not null)
    and (${t.dataSetPathname} is null) = (${t.dataSetUrl} is null) and (${t.exampleSource} = 'data_set') = (${t.dataSetBytes} is not null)
    and (${t.exampleSource} = 'data_set') = (${t.dataSetRows} is not null) and (${t.exampleSource} = 'data_set') = (${t.dataSetCounts} is not null)`),
  // A delivered or declined request has the time it was closed; an open one has none.
  check('adapter_request_closed', sql`(${t.status} in ('delivered', 'declined')) = (${t.closedAt} is not null)`),
  // A request whose examples were deleted holds none.
  check('adapter_request_examples_deleted', sql`${t.examplesDeletedAt} is null or ${t.examples} = '[]'::jsonb`),
]);

// The data set upload tokens issued to each account (server/endpoints/adapter-requests.ts: at most
// MAX_UPLOADS_PER_DAY in 24 hours, so an account cannot fill the file store with zips that never become a request:
// final review I1), one row per token, with the one pathname the token may create (re-review R1): a request may name
// only a zip whose pathname was issued to its account. Rows older than a day are deleted when the next token is issued.
export const dataSetUpload = pgTable('data_set_upload', {
  id: text('id').primaryKey(),
  userId: text('user_id').notNull().references(() => user.id, { onDelete: 'cascade' }),
  pathname: text('pathname').notNull().unique(),
  issuedAt: at('issued_at').notNull(),
}, (t) => [index('data_set_upload_user_issued').on(t.userId, t.issuedAt), index('data_set_upload_issued').on(t.issuedAt)]);
