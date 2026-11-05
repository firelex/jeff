// The hub as Vercel serves it, on this machine: for the browser checks (e2e/, via scripts/serve-local.ts) and the
// unit tests (test/local-server.test.ts). It follows vercel.json in Vercel's order: its redirects, then
// trailingSlash false (an address ending in / is redirected to the same address without it), then the filesystem
// (a file of the static build, or a folder's index.html, or the function api/index.ts at /api), then its rewrites
// (every other /api/* path goes to that function), and last 404.html with status 404. It supports only what
// vercel.json uses and throws on anything else, so a change there that this server cannot mirror is noticed at once.
//
// What the function receives was measured on a real Vercel preview (task-8-report.md, "Fix round 1"): the request's
// own path and query, unchanged, for a rewrite source with an unnamed group (/api/(.*)); a source with a named
// parameter (/api/:path*) also appends that parameter to the query (/api/a/b arrives as /api/a/b?path=a%2Fb), which
// this server reproduces so a test can tell the two apart. Vercel refuses a request over its function limit with 413
// FUNCTION_PAYLOAD_TOO_LARGE before the function runs; so does this server (MAX_REQUEST_BYTES). Like Vercel, it sets
// X-Forwarded-For to the client's own address, replacing what the client sent, and X-Forwarded-Proto to the scheme
// the client used (here always http).
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';

export interface VercelConfig {
  outputDirectory: string;
  trailingSlash: boolean;
  redirects: { source: string; destination: string; permanent: boolean }[];
  rewrites: { source: string; destination: string }[];
  headers: { source: string; headers: { key: string; value: string }[] }[];
}

// A vercel.json `source` as a regular expression over the whole path: literal path characters and unnamed groups,
// whose contents are regular expressions themselves (path-to-regexp's form, e.g. /(.*) or /((?!api(?:/|$)).*)). A
// named parameter (:name) is not modelled and throws.
export function sourcePattern(source: string): RegExp {
  let pattern = '^';
  let depth = 0;
  for (const char of source) {
    if (char === '(') depth += 1;
    if (depth === 0) {
      if (char === ':') throw new Error(`local-server.ts does not model named parameters in a source: ${source}`);
      pattern += char.replace(/[.*+?^${}|[\]\\]/g, '\\$&');
    } else {
      pattern += char;
    }
    if (char === ')') depth -= 1;
  }
  if (depth !== 0) throw new Error(`unbalanced parentheses in the source ${source}`);
  return new RegExp(`${pattern}$`);
}

// The headers vercel.json gives a path: every entry whose source matches, in order.
export function configHeaders(config: VercelConfig, pathname: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const entry of config.headers) {
    if (!sourcePattern(entry.source).test(pathname)) continue;
    for (const { key, value } of entry.headers) {
      const name = key.toLowerCase();
      if (out[name] !== undefined) throw new Error(`vercel.json sets ${key} twice for ${pathname}; local-server.ts does not model which one wins`);
      out[name] = value;
    }
  }
  return out;
}

export interface LocalServerOptions {
  clientDir: string; // the static build (vercel.json's outputDirectory)
  config: VercelConfig;
  api: (request: Request) => Promise<Response>; // api/index.ts's fetch handler
}

// The one function: api/index.ts, which Vercel serves at /api.
const FUNCTION_PATH = '/api';

// Vercel's limit on a request to a function: 4.5 MB (4,500,000 bytes), headers included. Measured on the preview: with
// the CLI's request headers (about 6 KB) the largest body it passed was 4,493,948 bytes. This server counts the
// header lines and the body against the same limit.
export const MAX_REQUEST_BYTES = 4_500_000;

function headerBytes(req: http.IncomingMessage): number {
  let size = 0;
  for (let i = 0; i < req.rawHeaders.length; i += 2) size += Buffer.byteLength(`${req.rawHeaders[i]}: ${req.rawHeaders[i + 1]}\r\n`);
  return size;
}

const TYPES: Record<string, string> = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.json': 'application/json', '.data': 'text/x-script', '.svg': 'image/svg+xml', '.woff2': 'font/woff2',
  '.png': 'image/png', '.txt': 'text/plain; charset=utf-8',
};

function typeOf(file: string): string {
  const type = TYPES[path.extname(file)];
  if (type === undefined) throw new Error(`local-server.ts has no content type for ${file}`);
  return type;
}

function isFile(file: string): boolean {
  return fs.existsSync(file) && fs.statSync(file).isFile();
}

// A rewrite of everything under one prefix, in either of the two forms Vercel takes: /prefix/(.*) (the request
// reaches the function unchanged) or /prefix/:name* (Vercel also adds name=<the rest of the path> to the query).
// Returns, for a path, null when it does not match, else the query parameters Vercel adds.
export function rewriteMatcher(source: string): (pathname: string) => [string, string][] | null {
  const unnamed = /^(\/[\w-]+(?:\/[\w-]+)*)\/\(\.\*\)$/.exec(source);
  const named = /^(\/[\w-]+(?:\/[\w-]+)*)\/:(\w+)\*$/.exec(source);
  const match = unnamed ?? named;
  if (match === null) throw new Error(`local-server.ts supports only rewrite sources like /api/(.*) or /api/:path*, not ${source}`);
  const prefix = match[1];
  return (pathname) => {
    if (pathname !== prefix && !pathname.startsWith(`${prefix}/`)) return null;
    const rest = pathname.slice(prefix.length + 1);
    return named !== null && rest !== '' ? [[named[2], rest]] : [];
  };
}

// The request body, or null when the request is larger than Vercel lets through (the rest is still read, so the
// client gets the 413 rather than a reset connection).
async function readBody(req: http.IncomingMessage): Promise<Buffer | null> {
  const chunks: Buffer[] = [];
  let size = headerBytes(req);
  for await (const chunk of req) {
    size += (chunk as Buffer).length;
    if (size <= MAX_REQUEST_BYTES) chunks.push(chunk as Buffer);
  }
  return size > MAX_REQUEST_BYTES ? null : Buffer.concat(chunks);
}

export function createLocalServer({ clientDir, config, api }: LocalServerOptions): http.Server {
  const root = path.resolve(clientDir);
  const notFound = path.join(root, '404.html');
  if (!isFile(notFound)) throw new Error(`${notFound} is missing: run the build first`);
  if (config.trailingSlash !== false) throw new Error('local-server.ts implements only trailingSlash: false, as vercel.json sets it');
  const rewrites = config.rewrites.map((rewrite) => {
    if (rewrite.destination !== FUNCTION_PATH) throw new Error(`local-server.ts can rewrite only to the function at ${FUNCTION_PATH}, not ${rewrite.destination}`);
    return rewriteMatcher(rewrite.source);
  });

  // Every answer but a redirect carries vercel.json's headers for its path (`extra`): files, the function's answers,
  // 404.html.
  function sendFile(res: http.ServerResponse, status: number, file: string, extra: Record<string, string>): void {
    res.writeHead(status, { 'content-type': typeOf(file), ...extra });
    fs.createReadStream(file).pipe(res);
  }

  // Measured on a Vercel preview (task-10-report.md, fix round 1): redirects carry none of vercel.json's headers.
  function redirect(res: http.ServerResponse, status: number, location: string): void {
    res.writeHead(status, { location });
    res.end();
  }

  async function callFunction(req: http.IncomingMessage, res: http.ServerResponse, url: URL, extra: Record<string, string>): Promise<void> {
    const headers = new Headers();
    for (const [name, value] of Object.entries(req.headers)) {
      if (value === undefined) continue;
      for (const one of Array.isArray(value) ? value : [value]) headers.append(name, one);
    }
    // Vercel sets X-Forwarded-For to the client's own address and drops any value the client sent.
    const client = req.socket.remoteAddress;
    if (client === undefined) throw new Error('local-server.ts: the connection has no remote address to put in X-Forwarded-For');
    headers.set('x-forwarded-for', client.startsWith('::ffff:') ? client.slice('::ffff:'.length) : client);
    // Vercel sets X-Forwarded-Proto to the scheme the client reached it by (always https: it redirects plain http at
    // its edge), replacing any value the client sent; the internal API trusts it in production
    // (app/lib/internal-api.server.ts). This server is reached over plain http, so it says http. NOT measured on a
    // preview yet (task-15-report.md, fix round 1): the deploy for the measurement was not permitted.
    headers.set('x-forwarded-proto', 'http');
    const hasBody = req.method !== 'GET' && req.method !== 'HEAD';
    const body = hasBody ? await readBody(req) : null;
    if (hasBody && body === null) {
      res.writeHead(413, { 'content-type': 'text/plain; charset=utf-8', 'x-vercel-error': 'FUNCTION_PAYLOAD_TOO_LARGE', ...extra });
      res.end('Request Entity Too Large\n\nFUNCTION_PAYLOAD_TOO_LARGE\n');
      return;
    }
    const response = await api(new Request(url, { method: req.method, headers, body: body === null ? undefined : new Uint8Array(body) }));
    // Every Set-Cookie header on its own line (Object.fromEntries of the headers would keep only the last one).
    const out: Record<string, string | string[]> = {};
    for (const [name, value] of response.headers) if (name !== 'set-cookie') out[name] = value;
    const cookies = response.headers.getSetCookie();
    if (cookies.length > 0) out['set-cookie'] = cookies;
    for (const name of Object.keys(extra)) {
      if (out[name] !== undefined) throw new Error(`the function set ${name} on ${url.pathname}, which vercel.json also sets; local-server.ts does not model which one wins`);
    }
    res.writeHead(response.status, { ...out, ...extra });
    res.end(Buffer.from(await response.arrayBuffer()));
  }

  async function route(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
    // The request target is joined to the origin as text: new URL('//evil.example/', base) would read the target as
    // another host.
    const url = new URL(`http://${req.headers.host as string}${req.url as string}`);
    const pathname = decodeURIComponent(url.pathname);
    const extra = configHeaders(config, pathname);
    const configured = config.redirects.find((r) => r.source === pathname);
    // Vercel first merges repeated slashes, with a 308 to the same path otherwise unchanged (measured:
    // //evil.example/ goes to /evil.example/, /.//x/ to /./x/), so no Location it writes starts with //.
    if (url.pathname.includes('//')) { redirect(res, 308, url.pathname.replace(/\/{2,}/g, '/') + url.search); return; }
    if (configured !== undefined) { redirect(res, configured.permanent ? 308 : 307, configured.destination); return; }
    // The Location keeps the path as it was sent, still percent-encoded: decoded, /%09/evil.example/ would become
    // /<tab>/evil.example, which a browser reads as //evil.example, another site (review I1).
    if (pathname !== '/' && pathname.endsWith('/')) { redirect(res, 308, url.pathname.replace(/\/+$/, '') + url.search); return; }
    const file = path.join(root, pathname);
    if (!file.startsWith(root)) { sendFile(res, 404, notFound, extra); return; }
    if (isFile(file)) { sendFile(res, 200, file, extra); return; }
    if (isFile(path.join(file, 'index.html'))) { sendFile(res, 200, path.join(file, 'index.html'), extra); return; }
    if (pathname === FUNCTION_PATH) { await callFunction(req, res, url, extra); return; }
    for (const matches of rewrites) {
      const added = matches(pathname);
      if (added === null) continue;
      for (const [name, value] of added) url.searchParams.append(name, value);
      await callFunction(req, res, url, extra);
      return;
    }
    sendFile(res, 404, notFound, extra);
  }

  // A handler that throws is a bug: like Vercel, answer 500 and print the error in full (nothing is hidden).
  return http.createServer((req, res) => {
    route(req, res).catch((error: unknown) => {
      console.error(`local-server: ${req.method} ${req.url} failed:`, error);
      if (!res.headersSent) res.writeHead(500, { 'content-type': 'text/plain; charset=utf-8' });
      res.end('The function failed; see the server log.');
    });
  });
}
