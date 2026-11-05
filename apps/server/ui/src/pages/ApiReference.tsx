// The API reference: every HTTP operation (read live from GET /openapi.json), with its request and response
// fields, the statuses it can answer with and which error types answer each one, and curl/Python code samples --
// plus the low-latency channel (/v1/channel) and MCP (/mcp), which the OpenAPI document describes in prose because
// neither is an HTTP operation it lists under "paths".
import { useEffect, useState } from 'react';
import { Badge, type BadgeTone, Spinner } from '@jeff/ui';
import { shownError, type Api, type ShownError } from '../api';
import { ErrorAlert } from '../components/ErrorAlert';
import { isRecord } from '../json';
import { Card, Page } from '../components/Page';

// --- The slice of the OpenAPI document this page reads -----------------------------------------------------------

interface Schema {
  $ref?: string;
  title?: string;
  type?: string | string[];
  anyOf?: Schema[];
  oneOf?: Schema[];
  items?: Schema;
  additionalProperties?: Schema | boolean;
  properties?: Record<string, Schema>;
  required?: string[];
  enum?: unknown[];
  const?: unknown;
  default?: unknown;
  description?: string;
  minimum?: number;
  maximum?: number;
  minItems?: number;
  maxItems?: number;
  pattern?: string;
}

interface MediaType {
  schema: Schema;
}

interface ResponseObject {
  description: string;
  content?: Record<string, MediaType>;
}

interface Operation {
  summary?: string;
  description?: string;
  requestBody?: { required?: boolean; content: Record<string, MediaType> };
  responses: Record<string, ResponseObject>;
}

interface HeaderObject {
  description: string;
}

interface OpenApiDoc {
  info: { title: string; version: string; description: string };
  paths: Record<string, Record<string, Operation>>;
  components?: { schemas?: Record<string, Schema>; headers?: Record<string, HeaderObject> };
}

function asOpenApiDoc(document: Record<string, unknown>): OpenApiDoc {
  const info = document.info;
  if (!isRecord(info) || typeof info.title !== 'string' || typeof info.version !== 'string' || typeof info.description !== 'string') {
    throw new Error('GET /openapi.json answered without a usable info.title, info.version and info.description');
  }
  if (!isRecord(document.paths)) throw new Error('GET /openapi.json answered without a usable "paths"');
  return document as unknown as OpenApiDoc;
}

// --- $ref resolution and a short type name for a schema ------------------------------------------------------

/**
 * A local JSON pointer ($ref starting with "#/"), walked against the whole document field by field -- not guessed
 * from the ref's last path segment, since several schemas here nest named pieces under their own $defs (for
 * example decide_request/$defs/call).
 */
function resolveRef(root: OpenApiDoc, ref: string): Schema {
  if (!ref.startsWith('#/')) throw new Error(`Only local $ref pointers are supported here: ${ref}`);
  let node: unknown = root;
  for (const rawPart of ref.slice(2).split('/')) {
    const part = rawPart.replace(/~1/g, '/').replace(/~0/g, '~');
    if (!isRecord(node) || !(part in node)) throw new Error(`Broken $ref in the OpenAPI document: ${ref}`);
    node = node[part];
  }
  return node as Schema;
}

function refName(ref: string): string {
  const name = ref.split('/').pop();
  if (!name) throw new Error(`Empty $ref: ${ref}`);
  return name;
}

function follow(schema: Schema, root: OpenApiDoc): Schema {
  return schema.$ref ? follow(resolveRef(root, schema.$ref), root) : schema;
}

/**
 * A oneOf/anyOf member's own name, in order: its $ref's component name (DecideCallsRequest); else its own
 * `title`; else, for a plain object member with no name of its own (decide_response's three reply shapes have
 * none -- only their parent "decide_response" has a title), the first key it `required`s (so "decisions" /
 * "decision" / "graph_result" tell the three replies to POST /v1/decide apart); else whatever typeName would
 * otherwise show.
 */
function variantName(schema: Schema, root: OpenApiDoc): string {
  if (schema.$ref) return refName(schema.$ref);
  if (schema.title) return schema.title;
  if (schema.required && schema.required.length > 0) return schema.required[0];
  return typeName(schema, root);
}

/** A short, readable type for a schema: "string", "list of string", "DecideCallsRequest | DecideDispatchRequest". */
function typeName(schema: Schema | undefined, root: OpenApiDoc): string {
  if (!schema) return 'any';
  if (schema.$ref) return refName(schema.$ref);
  if (schema.const !== undefined) return JSON.stringify(schema.const);
  if (schema.enum) return schema.enum.map((value) => JSON.stringify(value)).join(' | ');
  const union = schema.anyOf ?? schema.oneOf;
  if (union) return union.map((variant) => variantName(variant, root)).join(' | ');
  if (schema.type === 'array') return `list of ${typeName(schema.items, root)}`;
  if (schema.type === 'object' && typeof schema.additionalProperties === 'object') return `map of ${typeName(schema.additionalProperties, root)}`;
  if (Array.isArray(schema.type)) return schema.type.join(' | ');
  return schema.type ?? 'any';
}

function limits(schema: Schema): string {
  const parts: string[] = [];
  if (schema.minimum !== undefined) parts.push(`>= ${schema.minimum}`);
  if (schema.maximum !== undefined) parts.push(`<= ${schema.maximum}`);
  if (schema.minItems !== undefined || schema.maxItems !== undefined) parts.push(`${schema.minItems ?? 0}-${schema.maxItems ?? 'unbounded'} items`);
  if (schema.pattern !== undefined) parts.push(`matches ${schema.pattern}`);
  if (schema.default !== undefined) parts.push(`default ${JSON.stringify(schema.default)}`);
  return parts.join(', ');
}

/** The fields of an object schema (following $ref), or one block per variant for a oneOf/anyOf request or response. */
function Fields({ schema, root }: { schema: Schema; root: OpenApiDoc }) {
  const target = follow(schema, root);
  const union = target.oneOf ?? target.anyOf;
  if (union) {
    return (
      <div className="flex flex-col gap-3">
        {union.map((variant, index) => (
          <div key={index} className="flex flex-col gap-1.5">
            <p className="m-0 font-mono text-xs font-semibold text-text">{variantName(variant, root)}</p>
            <Fields schema={variant} root={root} />
          </div>
        ))}
      </div>
    );
  }
  if (!target.properties) return <p className="m-0 text-sm text-mute">{typeName(schema, root)}</p>;
  const required = new Set(target.required ?? []);
  return (
    <div className="overflow-hidden rounded-card border border-line">
      {Object.entries(target.properties).map(([name, field]) => (
        <div key={name} className="grid grid-cols-[9rem_1fr] gap-4 border-b border-line px-4 py-2.5 text-sm last:border-b-0">
          <div className="min-w-0">
            <span className="font-mono text-text">{name}</span>
            {required.has(name) && <span className="ml-2 text-xs text-danger">required</span>}
          </div>
          <div className="min-w-0">
            <p className="m-0 break-words font-mono text-xs text-primary-dark">{typeName(field, root)}</p>
            {(field.description || limits(field)) && (
              <p className="m-0 text-xs text-mute">{[field.description, limits(field)].filter(Boolean).join(' -- ')}</p>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

// --- A plausible example value for a schema, to fill a code sample's request body --------------------------------

function exampleValue(schema: Schema, root: OpenApiDoc, depth = 0): unknown {
  if (depth > 6) return '...';
  if (schema.$ref) return exampleValue(resolveRef(root, schema.$ref), root, depth + 1);
  if (schema.const !== undefined) return schema.const;
  if (schema.default !== undefined) return schema.default;
  if (schema.enum && schema.enum.length > 0) return schema.enum[0];
  const union = schema.anyOf ?? schema.oneOf;
  if (union && union.length > 0) return exampleValue(union[0], root, depth + 1);
  if (schema.properties) {
    const keys = schema.required ?? Object.keys(schema.properties);
    const out: Record<string, unknown> = {};
    for (const key of keys) {
      const field = schema.properties[key];
      if (field) out[key] = exampleValue(field, root, depth + 1);
    }
    return out;
  }
  if (schema.type === 'array') return schema.items ? [exampleValue(schema.items, root, depth + 1)] : [];
  const kind = Array.isArray(schema.type) ? schema.type[0] : schema.type;
  if (kind === 'string') return 'string';
  if (kind === 'integer' || kind === 'number') return schema.minimum ?? 0;
  if (kind === 'boolean') return true;
  if (kind === 'object') return {};
  return null;
}

// --- Code samples --------------------------------------------------------------------------------------------

function curlSample(origin: string, method: string, path: string, body: unknown | null): string {
  const lines = [`curl -X ${method.toUpperCase()} ${origin}${path}`, '-H "Authorization: Bearer <token>"'];
  if (body !== null) {
    lines.push('-H "Content-Type: application/json"');
    lines.push(`-d '${JSON.stringify(body).replace(/'/g, "'\\''")}'`);
  }
  return lines.join(' \\\n  ');
}

function pythonLiteral(json: string): string {
  return json.replace(/\bnull\b/g, 'None').replace(/\btrue\b/g, 'True').replace(/\bfalse\b/g, 'False');
}

/**
 * Jeff.remote (README "Remote mode") gives Python code the planner's own surface -- binding, labels, policies and
 * escalation -- and is how POST /v1/decide is called from Python. It has no method for the operations and console
 * endpoints (adapters, health, the access log, ...), so those get a plain `requests` call instead, with the same
 * bearer placeholder as the curl sample.
 */
function pythonSample(origin: string, method: string, path: string, body: unknown | null): string {
  if (path === '/v1/decide' && method.toLowerCase() === 'post') {
    return [
      'import jeff',
      '',
      `remote = jeff.Jeff.remote("${origin}", token="<token>", timeout_s=30.0)`,
      'decision = remote.function("<function id>", context={"...": "..."}, labels=None)("<input>")',
      'remote.close()',
    ].join('\n');
  }
  const lines = ['import requests', '', `response = requests.request(`, `    "${method.toUpperCase()}", "${origin}${path}",`, '    headers={"Authorization": "Bearer <token>"},'];
  if (body !== null) lines.push(`    json=${pythonLiteral(JSON.stringify(body, null, 4)).split('\n').join('\n    ')},`);
  lines.push(')', 'response.raise_for_status()', 'print(response.json())');
  return lines.join('\n');
}

function CodeBlock({ label, code }: { label: string; code: string }) {
  return (
    <div className="overflow-hidden rounded-card border border-line">
      <p className="m-0 border-b border-line bg-background px-3 py-1.5 text-xs font-medium text-mute">{label}</p>
      <pre className="m-0 max-h-80 overflow-auto p-3 font-mono text-xs leading-relaxed text-text">{code}</pre>
    </div>
  );
}

// --- Errors: contract/errors.yaml's status column, bundled here since that file is not served over HTTP ----------

/**
 * contract/errors.yaml's own status column, grouped by status: every JeffError subclass that can answer with
 * that HTTP status. That file is never served over HTTP, so a page built only from GET /openapi.json cannot read
 * it live; this is a maintained copy (apps/server/ui/src/pages/ApiReference.contract.test.tsx checks it against
 * the real file). Exported so that test can import it rather than keep its own second copy.
 */
export const ERROR_TYPES_BY_STATUS: Record<string, string[]> = {
  '401': ['Unauthorised'],
  '403': ['NotLicensed', 'FactoryOff'],
  '404': ['UnknownFunction'],
  '409': ['LowConfidence', 'Conflict'],
  '422': [
    'InvalidRequest', 'InvalidReference', 'MissingContext', 'UnknownContext', 'MissingLabels', 'InvalidLabels',
    'TooManyLabels', 'TooManyHandles', 'InterfaceMismatch', 'InvalidPolicy', 'GraphError', 'InputTooLong',
  ],
  '424': ['NotInstalled'],
  '429': ['RateLimited'],
  '500': ['JeffError', 'ContractError', 'ManifestError', 'EngineError'],
  '502': ['EscalationFailed'],
  '503': ['BackendUnavailable', 'NotReady'],
  '504': ['EscalationBudgetExceeded', 'DeadlineExceeded'],
  '529': ['Busy'],
};

/** The error type(s) that answer a status; throws naming the status rather than silently showing nothing or a
 * vague pointer, so a status this table has not been kept in step with (contract/errors.yaml gained one) is
 * loud, not a blank cell nobody notices. */
function errorTypesForStatus(status: string): string[] {
  const types = ERROR_TYPES_BY_STATUS[status];
  if (!types) throw new Error(`No error type is bundled for HTTP status ${status}; add it to ERROR_TYPES_BY_STATUS (mirroring contract/errors.yaml)`);
  return types;
}

const METHOD_TONE: Record<string, BadgeTone> = { get: 'info', post: 'ok', put: 'brand', delete: 'empty' };

/** components.headers.XRequestId's own description: every response, success or error, carries this header
 * (jeff_server.app._Application sets it even for an exception no route handler caught). Read from the document
 * rather than stated here a second time, so a changed description shows up without touching this page. */
function requestIdNote(spec: OpenApiDoc): string {
  const header = spec.components?.headers?.XRequestId;
  if (!header) throw new Error('GET /openapi.json has no components.headers.XRequestId to describe the x-request-id header');
  return header.description;
}

// --- The channel and MCP, carved out of info.description --------------------------------------------------------

const CHANNEL_MARKER = 'The low-latency channel';

function splitDescription(description: string): { overview: string; channelAndMcp: string } {
  const index = description.indexOf(CHANNEL_MARKER);
  if (index === -1) throw new Error('info.description no longer mentions the channel; the API reference cannot find that section');
  return { overview: description.slice(0, index).trim(), channelAndMcp: description.slice(index).trim() };
}

// --- One route -------------------------------------------------------------------------------------------------

/** The element id for a route's card, so the path list at the top of the page can scroll straight to it. */
function routeAnchor(method: string, path: string): string {
  return `route-${method}-${path.replace(/\//g, '_')}`;
}

/** Scrolls a route's card into view. The console routes by location.hash, so the path list must not set it. */
function scrollToRoute(method: string, path: string): void {
  const card = document.getElementById(routeAnchor(method, path));
  if (!card) throw new Error(`No route card for ${method.toUpperCase()} ${path} on this page`);
  card.scrollIntoView({ behavior: 'smooth' });
}

function Route({ path, method, operation, origin, root }: {
  path: string; method: string; operation: Operation; origin: string; root: OpenApiDoc;
}) {
  const requestSchema = operation.requestBody?.content['application/json']?.schema;
  const okStatus = '200' in operation.responses ? '200' : 'default';
  const okSchema = operation.responses[okStatus]?.content?.['application/json']?.schema;
  const errorRows = Object.entries(operation.responses).filter(([status]) => status !== okStatus);
  const exampleBody = requestSchema ? exampleValue(requestSchema, root) : null;

  return (
    <Card id={routeAnchor(method, path)} className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <Badge tone={METHOD_TONE[method] ?? 'neutral'} className="w-14 justify-center text-center uppercase">{method}</Badge>
        <span className="min-w-0 flex-1 truncate font-mono text-sm text-text">{path}</span>
      </div>
      {operation.summary && <p className="m-0 text-sm font-medium text-text">{operation.summary}</p>}
      {operation.description && <p className="m-0 text-sm text-soft">{operation.description}</p>}
      {requestSchema && (
        <div className="flex flex-col gap-1.5">
          <h4 className="m-0 text-xs font-semibold uppercase tracking-wide text-mute">Request</h4>
          <Fields schema={requestSchema} root={root} />
        </div>
      )}
      {okSchema && (
        <div className="flex flex-col gap-1.5">
          <h4 className="m-0 text-xs font-semibold uppercase tracking-wide text-mute">Response ({okStatus})</h4>
          <Fields schema={okSchema} root={root} />
        </div>
      )}
      {errorRows.length > 0 && (
        <div className="flex flex-col gap-1.5">
          <h4 className="m-0 text-xs font-semibold uppercase tracking-wide text-mute">Errors</h4>
          <div className="overflow-hidden rounded-card border border-line">
            {errorRows.map(([status, response]) => (
              <div key={status} className="grid grid-cols-[4.5rem_1fr] gap-4 border-b border-line px-4 py-2.5 text-sm last:border-b-0">
                <span className="font-mono text-text">{status}</span>
                <div className="min-w-0">
                  <p className="m-0 text-xs text-mute">{response.description}</p>
                  <p className="m-0 font-mono text-xs text-primary-dark">{errorTypesForStatus(status).join(', ')}</p>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
      <div className="flex flex-col gap-1.5">
        <h4 className="m-0 text-xs font-semibold uppercase tracking-wide text-mute">Call it</h4>
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          <CodeBlock label="curl" code={curlSample(origin, method, path, exampleBody)} />
          <CodeBlock label="Python" code={pythonSample(origin, method, path, exampleBody)} />
        </div>
      </div>
    </Card>
  );
}

export function ApiReference({ api }: { api: Api }) {
  const [spec, setSpec] = useState<OpenApiDoc | null>(null);
  const [error, setError] = useState<ShownError | null>(null);

  useEffect(() => {
    let current = true;
    void (async () => {
      try {
        const document = asOpenApiDoc(await api.openapi());
        if (current) setSpec(document);
      } catch (failure) {
        if (current) setError(shownError(failure));
      }
    })();
    return () => {
      current = false;
    };
  }, [api]);

  const origin = window.location.origin;

  return (
    <Page eyebrow="Learn" title="API reference" lead="Every operation this server answers, read live from its own GET /openapi.json.">
      {error && <ErrorAlert error={error} />}
      {!spec && !error && (
        <div className="flex items-center gap-2 text-sm text-mute">
          <Spinner /> Loading /openapi.json
        </div>
      )}
      {spec && (
        <ApiReferenceBody spec={spec} origin={origin} />
      )}
    </Page>
  );
}

function ApiReferenceBody({ spec, origin }: { spec: OpenApiDoc; origin: string }) {
  const { overview, channelAndMcp } = splitDescription(spec.info.description);
  const paths = Object.entries(spec.paths).sort((a, b) => a[0].localeCompare(b[0]));
  const routes = paths.flatMap(([path, methods]) => Object.entries(methods).map(([method, operation]) => ({ path, method, operation })));
  return (
    <>
      <Card className="flex flex-col gap-3">
        <div>
          <p className="m-0 text-xs font-semibold uppercase tracking-wide text-mute">{spec.info.title} -- version {spec.info.version}</p>
          <p className="mt-2 mb-0 text-sm text-soft">{overview}</p>
        </div>
        <p className="m-0 text-xs text-mute">
          Every response, success or error, carries an <code className="font-mono text-primary-dark">x-request-id</code> header: {requestIdNote(spec)}
        </p>
        <nav aria-label="Every path" className="flex flex-wrap gap-2 border-t border-line pt-3">
          {routes.map(({ path, method }) => (
            <button
              key={`${method} ${path}`}
              type="button"
              onClick={() => scrollToRoute(method, path)}
              className="inline-flex items-center gap-1.5 rounded-pill border border-line bg-transparent px-2.5 py-1 font-mono text-xs text-soft hover:border-primary-border hover:text-primary-dark"
            >
              <span className="uppercase text-mute">{method}</span> {path}
            </button>
          ))}
        </nav>
      </Card>
      <div className="flex flex-col gap-3">
        {routes.map(({ path, method, operation }) => (
          <Route key={`${method} ${path}`} path={path} method={method} operation={operation} origin={origin} root={spec} />
        ))}
      </div>
      <Card className="flex flex-col gap-2">
        <h2 className="m-0 text-sm font-semibold text-text">The channel and MCP</h2>
        <p className="m-0 text-sm text-soft">{channelAndMcp}</p>
      </Card>
    </>
  );
}
