// The API reference's data: every public operation in contract/openapi.yaml, with its request/response schemas
// resolved and a ready-to-run curl example. The contract is the source of truth, but it
// carries no `operationId` and no `tags` anywhere (checked 2026-11-01: zero matches for either across the whole
// document) -- so the hub derives each operation's id from its method and path, and groups operations itself
// through the fixed GROUPS map below, instead of reading either from the document. An unresolvable $ref is still
// an error naming it, never a silent default.
import fs from 'node:fs';
import { parse } from 'yaml';

export interface Operation {
  id: string; method: string; path: string; group: string; summary: string; description: string;
  requestSchema: unknown | null; responses: { status: string; description: string; schema: unknown | null }[];
  curl: string;
}

const METHODS = ['get', 'post', 'put', 'patch', 'delete'] as const;

// Public: under /v1 (but not the owner-only factory) or the health check. Everything else (/ui/*, /metrics, the
// factory) is left out of the public reference.
// /v1/systemone, the older question API, is still answered by the server but no longer documented (owner,
// 4 November 2026), so the reference leaves it out with the factory's paths.
const UNDOCUMENTED = new Set(['/v1/systemone']);

export function isPublic(path: string): boolean {
  return (path.startsWith('/v1/') || path === '/health') && !path.startsWith('/v1/factory/') && !UNDOCUMENTED.has(path.toLowerCase());
}

// Every public path's group. A public path missing from here is an error naming it, so adding a new public
// endpoint forces this map to be updated rather than silently landing in the wrong group (or none).
const GROUPS: Record<string, string> = {
  '/v1/decide': 'Decisions',
  '/v1/functions': 'Functions and graphs',
  '/v1/graphs': 'Functions and graphs',
  '/v1/adapters': 'Adapters',
  '/v1/adapters/served': 'Adapters',
  '/v1/adapters/reload': 'Adapters',
  '/health': 'Server',
  '/v1/models': 'Server',
};

function groupFor(path: string): string {
  const group = GROUPS[path.toLowerCase()];
  if (group === undefined) throw new Error(`openapi.yaml: ${path} is public but is not in openapi.server.ts's GROUPS map`);
  return group;
}

// <method>-<path>, both lower-cased, the path's leading slash dropped and every run of characters other than
// a-z0-9 turned into one '-', trimmed of any leading/trailing '-'. E.g. POST /v1/decide -> post-v1-decide,
// PUT /v1/adapters/served -> put-v1-adapters-served, GET /health -> get-health.
function deriveId(method: string, path: string): string {
  const slug = path.toLowerCase().replace(/^\//, '').replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  return `${method.toLowerCase()}-${slug}`;
}

function resolver(doc: Record<string, unknown>) {
  // $refs still being resolved higher up the current chain (e.g. decide's graph condition schema, which nests
  // itself for "and"/"or"/"not"). Inlining one of those again would recurse forever, so once a $ref reappears
  // while it is still on the way down, resolution stops there and the raw {$ref} pointer is kept instead of being
  // expanded further -- the same $ref reached from a different, non-recursive branch is still fully inlined.
  const resolving = new Set<string>();
  const resolve = (node: unknown): unknown => {
    if (Array.isArray(node)) return node.map(resolve);
    if (node === null || typeof node !== 'object') return node;
    const ref = (node as { $ref?: unknown }).$ref;
    if (typeof ref === 'string') {
      if (!ref.startsWith('#/')) throw new Error(`openapi.yaml: only local $refs are supported, got ${ref}`);
      if (resolving.has(ref)) return { $ref: ref };
      let target: unknown = doc;
      for (const part of ref.slice(2).split('/')) {
        target = (target as Record<string, unknown> | undefined)?.[part];
        if (target === undefined) throw new Error(`openapi.yaml: $ref ${ref} does not resolve`);
      }
      resolving.add(ref);
      const resolved = resolve(target);
      resolving.delete(ref);
      return resolved;
    }
    return Object.fromEntries(Object.entries(node).map(([k, v]) => [k, resolve(v)]));
  };
  return resolve;
}

function curlFor(method: string, url: string, hasBody: boolean): string {
  const lines = [`curl -s -X ${method} ${url}`, `  -H "Authorization: Bearer $JEFF_API_KEY"`];
  if (hasBody) lines.push(`  -H "Content-Type: application/json"`, `  -d @request.json`);
  return lines.join(' \\\n');
}

export function loadOperations(file: string, apiOrigin: string): Operation[] {
  const doc = parse(fs.readFileSync(file, 'utf8')) as Record<string, unknown>;
  const resolve = resolver(doc);
  const paths = doc.paths as Record<string, Record<string, Record<string, unknown>>>;
  const out: Operation[] = [];
  const idSources = new Map<string, string>();
  for (const [path, item] of Object.entries(paths)) {
    if (!isPublic(path)) continue;
    for (const method of METHODS) {
      const op = item[method];
      if (op === undefined) continue;
      const where = `${method.toUpperCase()} ${path}`;
      const id = deriveId(method, path);
      const clash = idSources.get(id);
      if (clash !== undefined) throw new Error(`openapi.yaml: operation id ${id} is used by both ${clash} and ${where}`);
      idSources.set(id, where);
      const group = groupFor(path);
      const body = (op.requestBody as { content?: Record<string, { schema?: unknown }> } | undefined)
        ?.content?.['application/json']?.schema;
      // A summary (the page's title and its search entry), the responses and each response's description are
      // required; an operation's own description is optional in OpenAPI, and the page then shows none.
      if (typeof op.summary !== 'string' || op.summary === '') throw new Error(`openapi.yaml: ${where} has no summary`);
      if (op.responses === undefined) throw new Error(`openapi.yaml: ${where} has no responses`);
      const responses = Object.entries(op.responses as Record<string, Record<string, unknown>>).map(([status, r]) => {
        if (typeof r.description !== 'string') throw new Error(`openapi.yaml: ${where} response ${status} has no description`);
        const schema = (r.content as Record<string, { schema?: unknown }> | undefined)?.['application/json']?.schema;
        return { status, description: r.description, schema: resolve(schema ?? null) };
      });
      out.push({
        id, method: method.toUpperCase(), path, group,
        summary: op.summary, description: op.description === undefined ? '' : String(op.description),
        requestSchema: body === undefined ? null : resolve(body), responses,
        curl: curlFor(method.toUpperCase(), `${apiOrigin}${path}`, body !== undefined),
      });
    }
  }
  return out;
}
