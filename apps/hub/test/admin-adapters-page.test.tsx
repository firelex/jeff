import { render, screen } from '@testing-library/react';
import { createRoutesStub } from 'react-router';
import { afterEach, expect, it, vi } from 'vitest';
import { HIDDEN_ADAPTERS as HIDDEN } from '../app/lib/owner-only/hidden-adapters.generated';
import type { HiddenAdapter } from '../app/lib/hidden-adapters';

const HIDDEN_ADAPTERS = HIDDEN as readonly HiddenAdapter[];
import AdminAdapters from '../app/routes/admin-adapters';

// The prerendered /admin/adapters shell: it asks GET /api/admin/adapters (a stubbed fetch here), shows the owner every
// hidden adapter with its examples, and says Not found to anyone the endpoint answers 404.
afterEach(() => { vi.unstubAllGlobals(); });

function show(answer: Response) {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url !== '/api/admin/adapters') throw new Error(`no stubbed answer for ${url}`);
    return answer;
  }));
  const Stub = createRoutesStub([{ path: '/admin/adapters', Component: AdminAdapters }]);
  return render(<Stub initialEntries={['/admin/adapters']} />);
}

it('shows the owner every hidden adapter with its API only tag, its numbers and its examples', async () => {
  show(Response.json({ adapters: HIDDEN_ADAPTERS }));
  for (const { entry, examples } of HIDDEN_ADAPTERS) {
    expect(await screen.findByRole('heading', { name: entry.title })).toBeTruthy();
    for (const { example } of examples) expect(screen.getAllByText(example.name ?? example.id).length).toBeGreaterThan(0);
  }
  expect(screen.queryAllByText('API only')).toHaveLength(HIDDEN_ADAPTERS.length);
  expect(document.querySelectorAll('[data-measured]')).toHaveLength(HIDDEN_ADAPTERS.filter((a) => a.numbers !== null).length);
  expect(screen.queryAllByText('No approved numbers were given for this adapter.')).toHaveLength(HIDDEN_ADAPTERS.filter((a) => a.numbers === null).length);
});

it('says Not found when the hub answers 404', async () => {
  show(Response.json({ error: { type: 'NotFound', message: 'the hub has no endpoint at /api/admin/adapters' } }, { status: 404 }));
  expect(await screen.findByRole('heading', { name: 'Not found' })).toBeTruthy();
});
