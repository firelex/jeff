import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { createApi, type Api } from '../api';
import { TRIAGE_ROUTE } from '../test/fixtures';
import { Home } from './Home';

function fakeApi(overrides: Partial<Api>): Api {
  return {
    functions: vi.fn(async () => ({ base_format: { format: 'bf16' }, functions: [TRIAGE_ROUTE] })),
    examples: vi.fn(async () => ({ examples: [] })),
    ...overrides,
  } as unknown as Api;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('Home', () => {
  it('a server that cannot be reached shows a NetworkError naming its address', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new TypeError('Failed to fetch');
    }));
    render(<Home api={createApi({ token: 't', onUnauthorised: () => {} })} user="menlo@scissero.com" onTry={() => {}} />);
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain(`NetworkError: Could not reach the Jeff server at ${window.location.origin}`);
  });

  it('lists the served functions', async () => {
    render(<Home api={fakeApi({})} user="menlo@scissero.com" onTry={() => {}} />);
    expect(await screen.findByText('jeff.support.triage')).toBeTruthy();
  });

  it('marks a function calibrated by its threshold on the base the server serves', async () => {
    const int4 = { format: 'int4-g64', format_id: 'jeff-base@v1.3+int4-g64-gptq.0123abcd' };
    const entry = { ...TRIAGE_ROUTE.calibration!, excluded: '64 rows the quantiser saw (quantization.json)' };
    const served = { ...TRIAGE_ROUTE, calibration_by_format: { [int4.format_id]: entry } };
    const missing = { ...TRIAGE_ROUTE, id: 'jeff.support.triage.urgency' };
    const uncalibrated = { ...TRIAGE_ROUTE, id: 'jeff.support.triage.needs_human', calibration: null };
    const functions = vi.fn(async () => ({ base_format: int4, functions: [served, missing, uncalibrated] }));
    render(<Home api={fakeApi({ functions } as unknown as Partial<Api>)} user="menlo@scissero.com" onTry={() => {}} />);
    expect(await screen.findByText('calibrated')).toBeTruthy();
    expect(screen.getByText('no threshold for this base')).toBeTruthy();
    expect(screen.getByText('not calibrated')).toBeTruthy();
  });

  it('on a bfloat16 base the calibration block alone counts', async () => {
    render(<Home api={fakeApi({})} user="menlo@scissero.com" onTry={() => {}} />);
    expect(await screen.findByText('calibrated')).toBeTruthy();
    expect(screen.queryByText('no threshold for this base')).toBeNull();
  });

  it('a malformed GET /ui/examples answer shows the problem in an error alert, not a spinner forever', async () => {
    const examples = vi.fn(async () => ({ examples: [{ name: 'an example with no call' }] }));
    render(<Home api={fakeApi({ examples } as unknown as Partial<Api>)} user="menlo@scissero.com" onTry={() => {}} />);
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('TypeError');
    expect(screen.queryByText('Loading the served functions')).toBeNull();
  });
});
