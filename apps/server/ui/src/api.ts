// The Jeff server's HTTP API, as the console uses it. Shapes follow contract/openapi.yaml and contract/schemas/*.json.
import { isRecord } from './json';

export type Json = string | number | boolean | null | Json[] | { [key: string]: Json };

/** A failed call: the server's standard error body ({"error": {type, message, details}}) and its x-request-id. */
export class ApiError extends Error {
  readonly status: number;
  readonly type: string;
  readonly details: Record<string, unknown>;
  readonly requestId: string | null;

  constructor({ status, type, message, details, requestId }: {
    status: number;
    type: string;
    message: string;
    details: Record<string, unknown>;
    requestId: string | null;
  }) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.type = type;
    this.details = details;
    this.requestId = requestId;
  }
}

/** A call that got no answer at all: the server could not be reached (it is down, the network is, or the browser
 * refused the request). Named after the server's address, so the visitor knows which server was unreachable. */
export class NetworkError extends Error {
  constructor(message: string, options: { cause: unknown }) {
    super(message, options);
    this.name = 'NetworkError';
  }
}

/** What the console shows for any failure: the error's type, its message and, when the server sent one, the request id. */
export interface ShownError {
  type: string;
  message: string;
  requestId: string | null;
}

export function shownError(error: unknown): ShownError {
  if (error instanceof ApiError) return { type: error.type, message: error.message, requestId: error.requestId };
  if (error instanceof Error) return { type: error.name, message: error.message, requestId: null };
  throw new Error(`Something other than an Error was thrown: ${String(error)}`);
}

/**
 * Reads a non-2xx answer into an ApiError. A body that is not the server's standard error shape (for example an HTML
 * page from a proxy in between) is reported as such, with the start of what came back, under the HTTP status.
 */
export async function errorFromResponse(response: Response): Promise<ApiError> {
  const requestId = response.headers.get('x-request-id');
  const text = await response.text();
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    body = undefined;
  }
  if (isRecord(body) && isRecord(body.error) && typeof body.error.type === 'string' && typeof body.error.message === 'string') {
    const details = isRecord(body.error.details) ? body.error.details : {};
    return new ApiError({ status: response.status, type: body.error.type, message: body.error.message, details, requestId });
  }
  return new ApiError({
    status: response.status,
    type: `HTTP ${response.status}`,
    message: `The server answered ${response.status} without a Jeff error body: ${text.slice(0, 300) || '(empty body)'}`,
    details: {},
    requestId,
  });
}

// ---- Shapes -------------------------------------------------------------------------------------------------------

export interface Session {
  user: string;
  token: string;
}

export interface Me {
  user: string;
  kind: 'api-key' | 'console' | 'hub' | 'open';
  admin: boolean;
}

export type HealthStatus = 'ready' | 'degraded' | 'broken';

export interface EngineHealth {
  socket: string;
  status: 'ready' | 'broken' | 'down';
  backend: string | null;
  adapters_version: number | null;
  adapters: string[] | null;
  cause?: string;
}

export interface Health {
  status: HealthStatus;
  engines?: EngineHealth[];
  model?: string;
  checkpoint?: string | null;
  max_options?: number | null;
  authentication?: boolean;
  modalities?: ('text' | 'image')[] | null;
  cause?: string;
}

export interface LabelSpec {
  source: 'fixed' | 'caller' | 'subset' | 'describe';
  fixed?: Record<string, string | null>;
  levels?: string[];
  fixed_first?: Record<string, string | null>;
  caller_keys?: string | null;
  max?: number;
  required?: string[];
  keys?: string[];
  // Fixed labels of a choice function only: true when a caller may offer a subset of them (labels as a list of keys).
  subset?: boolean;
}

// A context field as GET /v1/functions lists it: key is the field as the prompt shows it (it may contain spaces);
// code_name is the name a call passes it as.
export interface ContextField {
  key: string;
  code_name: string;
  description: string;
  required: boolean;
}

export interface Calibration {
  data: string;
  rows: number;
  target_precision: number;
  threshold: number;
  precision_at_threshold: number;
  coverage_at_threshold: number;
}

// A function's threshold on one INT4 base (an entry of calibration_by_format), and which calibration rows its fit left
// out.
export interface FormatCalibration extends Calibration {
  excluded: string;
}

// The weight format of the base the server's engines serve: {format: 'bf16'}, or for an INT4 base its format_id too.
export interface BaseFormat {
  format: string;
  format_id?: string;
}

export interface FunctionListing {
  id: string;
  interface: string;
  type: 'choice' | 'noul' | 'score';
  labels: LabelSpec;
  context: ContextField[];
  // form text: the function was trained on plain text alone, so its input is a string sent unwrapped, with no context
  input: { key: string; code_name: string; description: string; form: 'fields' | 'text' };
  calibration: Calibration | null;
  calibration_by_format?: Record<string, FormatCalibration>;
  adapter_version: string;
  licence: { kind: 'open' | 'proprietary'; adapter: string };
}

// GET /v1/functions.
export interface FunctionsResponse {
  base_format: BaseFormat;
  functions: FunctionListing[];
}

/**
 * Whether a function decides with a threshold on a base of `base`: on bfloat16 its calibration block; on an INT4 base
 * its calibration_by_format entry for that base's format_id. 'missing' is a function calibrated on bfloat16 with no
 * entry for the served INT4 base, which the server refuses to call.
 */
export function calibrationState(listing: FunctionListing, base: BaseFormat): 'calibrated' | 'not calibrated' | 'missing' {
  if (!listing.calibration) return 'not calibrated';
  if (base.format_id === undefined) return 'calibrated';
  return listing.calibration_by_format?.[base.format_id] ? 'calibrated' : 'missing';
}

export type Labels = Record<string, string | null> | string[] | null;

export interface DecideCall {
  function: string;
  input: Json;
  context?: Record<string, Json> | null;
  labels?: Labels;
}

export type DecideRequest = Record<string, Json>;

export type Value = string | boolean | number;

export interface Decision {
  value: Value;
  confidence: number | null;
  abstained: boolean;
  calibrated: boolean;
  interface: string;
  function: string;
  version: string;
  probabilities: Record<string, number>;
  score: number | null;
  route: { router: string; chose: string; confidence: number }[];
  decided_by: string;
  escalations: { decided_by: string; value: Value; confidence: number | null; reason: string }[];
  timing: Record<string, unknown>;
  evidence_refs: string[];
}

export type GraphNodeResult = { decision: Decision } | { skipped: true; reason: string } | { code: unknown };

export type DecideResponse =
  | { decisions: Decision[] }
  | { decision: Decision }
  | { graph_result: { nodes: Record<string, GraphNodeResult>; outputs: Record<string, GraphNodeResult>; stages: number } };

export interface Example {
  name: string;
  call: DecideCall;
  // the test set's answer, for an example taken from the adapter's test set; absent for a golden case
  expected?: Value;
}

export type Question =
  | { type: 'choice'; instructions?: Json; criteria: Record<string, Json> }
  | { type: 'score'; instructions?: Json; criteria: Json[] }
  | { type: 'noul'; instructions?: Json; criteria?: { true?: Json; false?: Json } | null };

export interface SystemOneRequest {
  model: string;
  state: Json;
  questions: Record<string, Question>;
  images?: string[];
  orders?: 1 | 2;
}

export type SystemOneAnswer =
  | { type: 'choice'; choice: string; probabilities: Record<string, number>; confidence: number }
  | { type: 'score'; score: number; probabilities: Record<string, number>; legend: Record<string, Json>; confidence: number }
  | { type: 'noul'; noul: number };

export interface SystemOneResponse {
  model: string;
  answers: Record<string, SystemOneAnswer>;
  usage: { input_tokens: number; output_tokens: 0; orders: 1 | 2 };
  timing: { server_ms: number } & Record<string, unknown>;
}

export interface ModelEntry {
  name: string;
  description: string;
  release_date: string;
}

export interface AdapterFunctionEntry {
  id: string;
  interface: string;
  /** Whether the function has a threshold for the base the engines serve. */
  calibrated: boolean;
}

/** One entry of GET /v1/adapters: an adapter trained for the base the engines serve. */
export interface AdapterEntry {
  id: string;
  aliases: string[];
  version: string;
  /** The adapter's one-line description, as its manifest writes it. */
  description: string;
  functions: AdapterFunctionEntry[];
  served: boolean;
}

export interface AdapterList {
  adapters: AdapterEntry[];
  adapters_version: number;
}

export interface AccessLogReport {
  entries: Record<string, unknown>[];
  total: number;
  people: Record<string, unknown>[];
  anonymous_visits: number;
  anonymous_ips: string[];
  locations_on: boolean;
}

/** A timed call's answer: the body, the server's request id, and the round trip measured in this browser. */
export interface Timed<T> {
  body: T;
  requestId: string;
  roundTripMs: number;
}

// ---- Where the server is -------------------------------------------------------------------------------------------

/**
 * The Jeff server's root, as seen by this browser: the address this page was loaded from, without its final `ui/`.
 * The server always serves the console at `<root>/ui/`, and the root may carry a path prefix when a proxy serves the
 * server under one (the gateway serves it at `/jeff/`, so the console is at `/jeff/ui/` and the root is `/jeff`).
 * `origin` is the full address without a trailing slash (for code samples), `path` the path part alone, '' at the
 * top (for this console's own calls). A page whose path does not end in `/ui/` is an error: the console would not
 * know where the server is.
 */
export function serverRoot(href: string = window.location.href): { origin: string; path: string } {
  const page = new URL(href);
  if (!page.pathname.endsWith('/ui/')) {
    throw new Error(`The console must be opened at an address ending in /ui/ to find its server; it was opened at ${page.pathname}`);
  }
  const path = page.pathname.slice(0, -'/ui/'.length);
  return { origin: page.origin + path, path };
}

// ---- Calls ---------------------------------------------------------------------------------------------------------

/** `path` is the server's own path (`/v1/decide`); it is sent under the server root (`/jeff/v1/decide` behind the gateway). */
async function send(path: string, init: RequestInit, token: string | null): Promise<Response> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (init.body !== undefined) headers['Content-Type'] = 'application/json';
  if (token !== null) headers.Authorization = `Bearer ${token}`;
  const root = serverRoot();
  try {
    // credentials 'same-origin': the browser sends this site's own cookies, which the Jeff server ignores (it reads
    // only Authorization), but a proxy in front of it may need: the gateway that serves Jeff under /jeff/ lets a call
    // without Authorization (the sign-in itself) through only with its own sign-in cookie.
    return await fetch(root.path + path, { ...init, headers, credentials: 'same-origin' });
  } catch (cause) {
    const reason = cause instanceof Error ? cause.message : String(cause);
    throw new NetworkError(
      `Could not reach the Jeff server at ${root.origin} (${init.method ?? 'GET'} ${path}): ${reason}`, { cause });
  }
}

/** POST /ui/login: checks a console sign-in against the server's JEFF_UI_USERS file. Needs no session. */
export async function signIn(email: string, password: string): Promise<Session> {
  const response = await send('/ui/login', { method: 'POST', body: JSON.stringify({ email, password }) }, null);
  if (!response.ok) throw await errorFromResponse(response);
  const body: unknown = await response.json();
  if (!isRecord(body) || typeof body.user !== 'string' || typeof body.token !== 'string') {
    throw new Error(`POST /ui/login answered without a string user and token: ${JSON.stringify(body).slice(0, 200)}`);
  }
  return { user: body.user, token: body.token };
}

export interface ApiOptions {
  token: string;
  /** Called whenever a call answers 401, with the error and the token that call sent: that token is no longer
   * accepted. The token lets the caller ignore a late refusal for a session it has already replaced. */
  onUnauthorised: (error: ApiError, token: string) => void;
}

export type Api = ReturnType<typeof createApi>;

/** The calls a signed-in console makes. Every one sends the session token; any non-2xx answer throws ApiError. */
export function createApi({ token, onUnauthorised }: ApiOptions) {
  async function call(path: string, init: RequestInit = {}): Promise<Response> {
    const response = await send(path, init, token);
    if (!response.ok) {
      const error = await errorFromResponse(response);
      if (response.status === 401) onUnauthorised(error, token);
      throw error;
    }
    return response;
  }

  async function json<T>(path: string, init: RequestInit = {}): Promise<T> {
    return (await (await call(path, init)).json()) as T;
  }

  async function timed<T>(path: string, body: unknown): Promise<Timed<T>> {
    const started = performance.now();
    const response = await call(path, { method: 'POST', body: JSON.stringify(body) });
    const answer = (await response.json()) as T;
    const roundTripMs = performance.now() - started;
    const requestId = response.headers.get('x-request-id');
    if (requestId === null) throw new Error(`${path} answered without an x-request-id header`);
    return { body: answer, requestId, roundTripMs };
  }

  return {
    me: () => json<Me>('/ui/me'),
    reportView: (view: string) => json<{ logged: boolean }>('/ui/events', { method: 'POST', body: JSON.stringify({ view }) }),
    examples: () => json<{ examples: Example[] }>('/ui/examples'),
    accessLog: (limit: number) => json<AccessLogReport>(`/ui/access-log?limit=${encodeURIComponent(String(limit))}`),
    health: () => json<Health>('/health'),
    functions: () => json<FunctionsResponse>('/v1/functions'),
    decide: (request: unknown) => timed<DecideResponse>('/v1/decide', request),
    systemOne: (request: SystemOneRequest) => timed<SystemOneResponse>('/v1/systemone', request),
    models: () => json<{ models: ModelEntry[] }>('/v1/models'),
    adapters: () => json<AdapterList>('/v1/adapters'),
    setServed: (adapters: string[]) =>
      json<AdapterList>('/v1/adapters/served', { method: 'PUT', body: JSON.stringify({ adapters }) }),
    openapi: () => json<Record<string, unknown>>('/openapi.json'),
    /** A JSON call to the adapter factory (/v1/factory<path>); the typed calls are in pages/factory/api.ts. */
    factory: <T>(method: 'GET' | 'POST' | 'PUT', path: string, body?: unknown) =>
      json<T>(`/v1/factory${path}`, body === undefined ? { method } : { method, body: JSON.stringify(body) }),
    /** A factory file as text (a file of an importable question set). */
    factoryText: async (path: string) => (await call(`/v1/factory${path}`)).text(),
    /** A factory event stream (server-sent events), open until `signal` aborts it. */
    factoryStream: (path: string, signal: AbortSignal) => call(`/v1/factory${path}`, { signal }),
  };
}
