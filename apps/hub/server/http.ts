// JSON responses and errors for the hub's endpoints (api/index.ts). Every error body has the API's own shape
// (contract/openapi.yaml's ErrorBody): {"error": {"type", "message", "details"}}, where the message says in plain
// words what was wrong, so a caller never has to guess.

export function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return Response.json(body, { status, headers: { 'cache-control': 'no-store', ...headers } });
}

// An error an endpoint answers with on purpose: a status, a type name (PascalCase, like contract/errors.yaml's), a
// message naming what is wrong, and details a program can read. Anything else an endpoint throws is a bug and is
// left to fail the request loudly (the platform answers 500 and logs it).
export class ApiError extends Error {
  readonly status: number;
  readonly type: string;
  readonly details: Record<string, unknown>;
  readonly headers: Record<string, string>;

  constructor(status: number, type: string, message: string, details: Record<string, unknown> = {}, headers: Record<string, string> = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.type = type;
    this.details = details;
    this.headers = headers;
  }
}

export function errorResponse(error: ApiError): Response {
  return json(error.status, { error: { type: error.type, message: error.message, details: error.details } }, error.headers);
}
