// @vitest-environment node
// The owner-only endpoints of the hidden adapters (server/endpoints/admin-adapters.ts): GET /api/admin/adapters gives
// the owner the hidden adapters' entries and examples from the function's content module, GET /api/admin/owner says
// whether the visitor is the owner; to anyone else, signed in or not, the first is the 404 of a path that does not exist.
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { HIDDEN_ADAPTERS as HIDDEN } from '../app/lib/owner-only/hidden-adapters.generated';
import fs from 'node:fs';
import path from 'node:path';
import { parse } from 'yaml';
import type { HiddenAdapter } from '../app/lib/hidden-adapters';
import { HUB_ROOT } from '../app/lib/paths.server';

const HIDDEN_ADAPTERS = HIDDEN as readonly HiddenAdapter[];
const WITHOUT_NUMBERS = parse(fs.readFileSync(path.join(HUB_ROOT, 'content/owner-only/without-numbers.yaml'), 'utf8')) as string[];
import type { SessionUser } from '../app/lib/session.server';
import { serveEndpoint } from '../server/endpoint';
import { adminAdapterEndpoints } from '../server/endpoints/admin-adapters';
import { ApiError } from '../server/http';
import { endpointFor } from '../server/router';
import { TEST_ENV } from './helpers/settings';

const OWNER: SessionUser = { id: 'owner', email: 'Owner@Example.com', emailVerified: true, name: 'Owner', image: null };
const ADA: SessionUser = { id: 'ada', email: 'ada@example.com', emailVerified: true, name: 'Ada', image: null };

beforeEach(() => { for (const [name, value] of Object.entries(TEST_ENV)) vi.stubEnv(name, value); });

function endpointsFor(visitor: SessionUser | null) {
  return adminAdapterEndpoints({
    ownerEmail: () => 'owner@example.com',
    signedIn: async () => {
      if (visitor === null) throw new ApiError(401, 'SignInRequired', 'sign in first');
      return visitor;
    },
    hidden: () => HIDDEN_ADAPTERS,
  });
}

const get = (path: string) => new Request(`https://jeffhub.ai${path}`);

describe('the hidden adapters endpoints', () => {
  it('give the owner every hidden adapter with its numbers and examples', async () => {
    const response = await serveEndpoint(endpointsFor(OWNER).adapters, get('/api/admin/adapters'));
    expect(response.status).toBe(200);
    const { adapters } = await response.json() as { adapters: typeof HIDDEN_ADAPTERS };
    // Every adapter with text in content/owner-only (every one the API serves that is not open).
    const hiddenText = parse(fs.readFileSync(path.join(HUB_ROOT, 'content/owner-only/catalogue-text.yaml'), 'utf8')) as Record<string, unknown>;
    expect(adapters.map((a) => a.entry.id)).toEqual(Object.keys(hiddenText).sort());
    for (const adapter of adapters) {
      expect(adapter.entry.kind).toBe('proprietary');
      expect(adapter.numbers === null).toBe(WITHOUT_NUMBERS.includes(adapter.entry.id));
      expect(adapter.examples.length).toBeGreaterThan(0);
      for (const { example, request } of adapter.examples) expect(request.calls[0].function).toBe(example.function);
    }
    const owner = await serveEndpoint(endpointsFor(OWNER).owner, get('/api/admin/owner'));
    expect(await owner.json()).toEqual({ owner: true });
  });

  it('answer anyone else, signed in or not, with the 404 of a path that does not exist, and that they are not the owner', async () => {
    for (const visitor of [ADA, null]) {
      const path = '/api/admin/adapters';
      const response = await serveEndpoint(endpointsFor(visitor).adapters, get(path));
      expect(response.status).toBe(404);
      expect((await response.json()).error).toEqual({ type: 'NotFound', message: `the hub has no endpoint at ${path}`, details: { path } });
      const owner = await serveEndpoint(endpointsFor(visitor).owner, get('/api/admin/owner'));
      expect(owner.status).toBe(200);
      expect(await owner.json()).toEqual({ owner: false });
    }
  });

  it('answer a visitor whose email is OWNER_EMAIL but not verified as anyone else (final review C1)', async () => {
    const unverified: SessionUser = { ...OWNER, emailVerified: false };
    const response = await serveEndpoint(endpointsFor(unverified).adapters, get('/api/admin/adapters'));
    expect(response.status).toBe(404);
    const owner = await serveEndpoint(endpointsFor(unverified).owner, get('/api/admin/owner'));
    expect(await owner.json()).toEqual({ owner: false });
  });

  it('are routed by their paths', () => {
    expect(endpointFor('/api/admin/adapters')).toBeDefined();
    expect(endpointFor('/api/admin/owner')).toBeDefined();
  });
});
