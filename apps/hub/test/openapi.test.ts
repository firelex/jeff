import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { isPublic, loadOperations } from '../app/lib/openapi.server';

const file = path.join(__dirname, '../../../contract/openapi.yaml');

describe('API reference', () => {
  const ops = loadOperations(file, 'https://api.example.com');
  it('lists the public operations only', () => {
    expect(ops.some((o) => o.path === '/v1/decide' && o.method === 'POST')).toBe(true);
    expect(ops.some((o) => o.path.startsWith('/v1/factory/'))).toBe(false);
    expect(ops.some((o) => o.path.startsWith('/ui/'))).toBe(false);
  });
  it('leaves out /v1/systemone, the older question API the server still answers but no longer documents', () => {
    expect(ops.some((o) => o.path === '/v1/systemone')).toBe(false);
    expect(ops.some((o) => o.group === 'Older API')).toBe(false);
  });
  it('has a page id (derived from method and path) and a curl example for each', () => {
    expect(ops.some((o) => o.id === 'post-v1-decide')).toBe(true);
    for (const o of ops) {
      expect(o.id, `${o.method} ${o.path}`).toMatch(/^[a-z0-9-]+$/);
      expect(o.curl).toContain(`https://api.example.com${o.path}`);
    }
  });
  it('covers every public path in the document', () => {
    const doc = require('yaml').parse(require('node:fs').readFileSync(file, 'utf8'));
    const publicPaths = Object.keys(doc.paths).filter(isPublic);
    for (const p of publicPaths) expect(ops.some((o) => o.path === p), p).toBe(true);
  });
  it('fails on a public path missing from the GROUPS map, naming it', () => {
    const fixture = path.join(__dirname, 'fixtures/openapi/unmapped-path.yaml');
    expect(() => loadOperations(fixture, 'https://api.example.com')).toThrow('/v1/unknown-thing');
  });
  it('fails when two operations derive the same id, naming it', () => {
    const fixture = path.join(__dirname, 'fixtures/openapi/duplicate-ids.yaml');
    expect(() => loadOperations(fixture, 'https://api.example.com')).toThrow('get-v1-decide');
  });
  it('fails on an operation with no summary, naming it', () => {
    const fixture = path.join(__dirname, 'fixtures/openapi/no-summary.yaml');
    expect(() => loadOperations(fixture, 'https://api.example.com')).toThrow('POST /v1/decide has no summary');
  });
});
