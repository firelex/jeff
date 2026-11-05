// Renders <ApiReference> against the real contract/openapi.yaml (parsed from disk, not a fixture) and checks the
// error-type table against the real contract/errors.yaml, so the page's rendering and its bundled error-type copy
// both stay honest about the actual contract, not just a synthetic document built to look like it.
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { parse } from 'yaml';
import type { Api } from '../api';
import { ApiReference, ERROR_TYPES_BY_STATUS } from './ApiReference';

const here = path.dirname(fileURLToPath(import.meta.url)); // apps/server/ui/src/pages
const repoRoot = path.resolve(here, '..', '..', '..', '..', '..'); // up to the repository root
const CONTRACT = parse(readFileSync(path.join(repoRoot, 'contract', 'openapi.yaml'), 'utf-8')) as {
  paths: Record<string, Record<string, { responses: Record<string, unknown> }>>;
};
const ERRORS_YAML = parse(readFileSync(path.join(repoRoot, 'contract', 'errors.yaml'), 'utf-8')) as
  Record<string, { status: number | null }>;

function fakeApi(): Api {
  return { openapi: vi.fn(async () => CONTRACT) } as unknown as Api;
}

const ROUTES = Object.entries(CONTRACT.paths).flatMap(
  ([path_, methods]) => Object.entries(methods).map(([method, operation]) => ({ path: path_, method, operation })),
);

describe('ApiReference against the real contract/openapi.yaml', () => {
  it('renders a route card for every real path and method', async () => {
    const { container } = render(<ApiReference api={fakeApi()} />);
    await screen.findAllByText('/v1/decide');
    const cards = container.querySelectorAll('[id^="route-"]');
    expect(cards.length).toBe(ROUTES.length);
    // [id="..."], not #...: a path with parameters ({run_id}) is not a valid CSS id selector.
    for (const { path: routePath, method } of ROUTES) {
      expect(screen.getAllByText(routePath).length).toBeGreaterThan(0);
      expect(container.querySelector(`[id="route-${method}-${routePath.replace(/\//g, '_')}"]`)).not.toBeNull();
    }
  });

  it('/v1/decide shows its three named request variants and its three response variants', async () => {
    render(<ApiReference api={fakeApi()} />);
    await screen.findAllByText('/v1/decide');
    // The request's oneOf members are all named $refs (contract/openapi.yaml's DecideCallsRequest and siblings).
    for (const name of ['DecideCallsRequest', 'DecideDispatchRequest', 'DecideGraphRequest']) {
      expect(screen.getAllByText(name).length).toBeGreaterThan(0);
    }
    // decide_response's oneOf members have no $ref or title of their own; each is named by its first required
    // property -- exactly the case the variant-naming fallback exists for.
    for (const name of ['decisions', 'decision', 'graph_result']) {
      expect(screen.getAllByText(name).length).toBeGreaterThan(0);
    }
  });

  it('every route\'s own response statuses render, each with its error type(s)', async () => {
    const { container } = render(<ApiReference api={fakeApi()} />);
    await screen.findAllByText('/v1/decide');
    for (const { method, path: routePath, operation } of ROUTES) {
      const card = container.querySelector(`[id="route-${method}-${routePath.replace(/\//g, '_')}"]`);
      if (!card) throw new Error(`No rendered card for ${method} ${routePath}`);
      for (const status of Object.keys(operation.responses)) {
        if (status === '200') continue; // the success status has no error-type row
        expect(card.textContent, `${method} ${routePath} should show status ${status}`).toContain(status);
      }
    }
  });

  it('ERROR_TYPES_BY_STATUS matches contract/errors.yaml\'s own status column exactly', () => {
    const expected: Record<string, string[]> = {};
    for (const [name, entry] of Object.entries(ERRORS_YAML)) {
      if (entry.status === null) continue; // never sent over HTTP
      const status = String(entry.status);
      (expected[status] ??= []).push(name);
    }
    const sort = (table: Record<string, string[]>) =>
      Object.fromEntries(Object.entries(table).map(([status, names]) => [status, [...names].sort()]));
    expect(sort(ERROR_TYPES_BY_STATUS)).toEqual(sort(expected));
  });
});
