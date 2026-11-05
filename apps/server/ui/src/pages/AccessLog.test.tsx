import { describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ApiError, type Api, type AccessLogReport } from '../api';
import { AccessLog } from './AccessLog';

const REPORT: AccessLogReport = {
  total: 5,
  locations_on: true,
  anonymous_visits: 2,
  anonymous_ips: ['203.0.113.9'],
  people: [
    {
      user: 'menlo@scissero.com', first: 1_000, last: 2_000, sign_ins: 3, failed_sign_ins: 1,
      ips: { '10.0.0.5': 4, '198.51.100.2': 1 },
      locations: { 'private network (tailnet or LAN)': 4, 'Lisbon, Lisbon, Portugal': 1 },
    },
  ],
  entries: [
    { time: 2_000, event: 'signin', user: 'menlo@scissero.com', ip: '10.0.0.5', peer: '10.0.0.5', location: 'private network (tailnet or LAN)', path: '/ui/login', status: 200, server: 'srv-1', detail: null, agent: 'Mozilla/5.0', forwarded: null },
    { time: 1_900, event: 'view', user: 'menlo@scissero.com', ip: '10.0.0.5', peer: '10.0.0.5', location: 'private network (tailnet or LAN)', path: '/ui/events', status: 200, server: 'srv-1', detail: 'home', agent: 'Mozilla/5.0', forwarded: null },
    { time: 1_800, event: 'api', user: 'menlo@scissero.com', ip: '10.0.0.5', peer: '10.0.0.5', location: 'private network (tailnet or LAN)', path: '/v1/decide', status: 200, server: 'srv-1', detail: 'POST', agent: 'Mozilla/5.0', forwarded: null },
    { time: 1_700, event: 'console', user: null, ip: '203.0.113.9', peer: '10.0.0.9', location: 'unknown', path: '/ui', status: 200, server: 'srv-1', detail: null, agent: 'curl/8.0', forwarded: '203.0.113.9, 10.0.0.9' },
  ],
};

function fakeApi(overrides: Partial<Api> = {}): Api {
  return { accessLog: vi.fn(async (_limit: number) => REPORT), ...overrides } as unknown as Api;
}

describe('AccessLog', () => {
  it('shows people, their addresses and places, and the anonymous-visit count', async () => {
    render(<AccessLog api={fakeApi()} />);
    const card = await screen.findByRole('button', { name: /menlo@scissero\.com/ });
    expect(within(card).getByText(/3 sign-ins, 1 failed/)).toBeTruthy();
    expect(within(card).getByText('private network (tailnet or LAN)')).toBeTruthy();
    expect(within(card).getByText('Lisbon, Lisbon, Portugal')).toBeTruthy();
    expect(screen.getByText(/2 console visits from 1 address/)).toBeTruthy();
    expect(screen.getByText(/Every visit starts here; addresses that never sign in afterwards opened the link without a login\./)).toBeTruthy();
    expect(screen.getByText(/5 entries in all; the newest 4 are loaded\./)).toBeTruthy();
    // DB-IP attribution shows only when locations are on (the fixture's locations_on: true).
    expect(screen.getByRole('link', { name: 'DB-IP' }).getAttribute('href')).toBe('https://db-ip.com');
  });

  it('the address cell\'s tooltip names the connection\'s own peer and the whole X-Forwarded-For header', async () => {
    render(<AccessLog api={fakeApi()} />);
    await screen.findByRole('button', { name: /menlo@scissero\.com/ });
    const cell = screen.getByText('203.0.113.9', { selector: 'td' });
    expect(cell.getAttribute('title')).toBe('connection from 10.0.0.9; X-Forwarded-For: 203.0.113.9, 10.0.0.9');
    // peer equals ip with no X-Forwarded-For header (the common case): the tooltip still names the connection
    // (jeff-server's own AccessLog.tsx behaviour), just without the X-Forwarded-For line.
    const directCells = screen.getAllByText('10.0.0.5', { selector: 'td' });
    expect(directCells.length).toBeGreaterThan(0);
    for (const direct of directCells) expect(direct.getAttribute('title')).toBe('connection from 10.0.0.5');
  });

  it('hides API calls by default, and the toggle shows them', async () => {
    const user = userEvent.setup();
    render(<AccessLog api={fakeApi()} />);
    await screen.findByRole('button', { name: /menlo@scissero\.com/ });
    expect(screen.queryByText('API call')).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Show API calls' }));
    expect(screen.getByText('API call')).toBeTruthy();
    expect(screen.getByText('POST /v1/decide')).toBeTruthy();
  });

  it('filtering by a person shows only their events; "show everyone" clears it', async () => {
    const user = userEvent.setup();
    render(<AccessLog api={fakeApi()} />);
    await screen.findByRole('button', { name: /menlo@scissero\.com/ });
    await user.click(screen.getByText('Visits without a sign-in'));
    expect(screen.getByText('not signed in')).toBeTruthy();
    expect(screen.queryByText('Signed in')).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Show everyone' }));
    expect(screen.getByText('Signed in')).toBeTruthy();
  });

  it('a non-admin\'s 403 shows the server\'s message, not an empty page', async () => {
    const accessLog = vi.fn(async () => {
      throw new ApiError({ status: 403, type: 'NotLicensed', message: "Only the server's administrators (JEFF_ADMINS) may read the access log; menlo@scissero.com is not one", details: {}, requestId: 'req-403' });
    });
    render(<AccessLog api={fakeApi({ accessLog })} />);
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain("NotLicensed: Only the server's administrators");
  });

  it('an entry with an event kind this page does not know is reported, not shown with a blank label', async () => {
    const entries = [{ ...REPORT.entries[0], event: 'teleported' }];
    const accessLog = vi.fn(async () => ({ ...REPORT, entries }));
    render(<AccessLog api={fakeApi({ accessLog })} />);
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('teleported');
  });

  it('a server with no access log configured (422) shows that message too', async () => {
    const accessLog = vi.fn(async () => {
      throw new ApiError({ status: 422, type: 'InvalidRequest', message: 'No access log on this server: set JEFF_ACCESS_LOG', details: {}, requestId: 'req-422' });
    });
    render(<AccessLog api={fakeApi({ accessLog })} />);
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('No access log on this server: set JEFF_ACCESS_LOG');
  });

  it('locations off shows a notice instead of guessing a location', async () => {
    render(<AccessLog api={fakeApi({ accessLog: vi.fn(async () => ({ ...REPORT, locations_on: false })) })} />);
    expect(await screen.findByText('Locations are off')).toBeTruthy();
  });
});
