// @vitest-environment node
// The pages' Content-Security-Policy (vercel.json) lets the browser reach the Blob store's upload address, the one
// @vercel/blob's client sends a data set's zip to (its control API, https://vercel.com/api/blob, with /?pathname=...
// for one request and /mpu?... for a multipart upload), and nothing else of vercel.com.
import fs from 'node:fs';
import path from 'node:path';
import { expect, it } from 'vitest';

const HUB = path.join(import.meta.dirname, '..');

it("names the exact upload address of the installed @vercel/blob in connect-src", () => {
  const dist = path.join(HUB, 'node_modules/@vercel/blob/dist');
  const sources = fs.readdirSync(dist).filter((f) => f.endsWith('.js')).map((f) => fs.readFileSync(path.join(dist, f), 'utf8')).join('\n');
  const found = /defaultVercelBlobApiUrl = "([^"]+)"/.exec(sources);
  expect(found?.[1]).toBe('https://vercel.com/api/blob');
  const config = JSON.parse(fs.readFileSync(path.join(HUB, 'vercel.json'), 'utf8')) as { headers: { source: string; headers: { key: string; value: string }[] }[] };
  const pages = config.headers.find((h) => h.source === '/((?!api(?:/|$)).*)')!;
  const policy = pages.headers.find((h) => h.key === 'Content-Security-Policy')!.value;
  const connect = policy.split(';').map((d) => d.trim()).find((d) => d.startsWith('connect-src '))!;
  expect(connect).toBe(`connect-src 'self' ${found![1]}/`);
});
