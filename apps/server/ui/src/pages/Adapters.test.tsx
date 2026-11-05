import { describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ApiError, type Api, type AdapterEntry, type Me } from '../api';
import { TRIAGE_ROUTE, TRIAGE_URGENCY } from '../test/fixtures';
import { Adapters } from './Adapters';

const TRIAGE: AdapterEntry = {
  id: 'jeff.support.triage', aliases: ['triage'], version: '1.3.0',
  description: 'Routes a support request to a team and rates its urgency.',
  functions: [
    { id: 'jeff.support.triage.route', interface: 'route', calibrated: false },
    { id: 'jeff.support.triage.urgency', interface: 'rate', calibrated: true },
  ],
  served: true,
};
const SAFETY_SPAM: AdapterEntry = {
  id: 'jeff.safety.spam', aliases: ['spam'], version: '2.0.0',
  description: 'Says whether a message is legitimate, spam or phishing.',
  functions: [{ id: 'jeff.safety.spam.message_type', interface: 'classify', calibrated: true }],
  served: false,
};

function fakeApi(overrides: Partial<Api> = {}): Api {
  return {
    adapters: vi.fn(async () => ({ adapters: [TRIAGE, SAFETY_SPAM], adapters_version: 4 })),
    functions: vi.fn(async () => ({ base_format: { format: 'bf16' }, functions: [TRIAGE_ROUTE, TRIAGE_URGENCY] })),
    setServed: vi.fn(async () => ({ adapters: [TRIAGE, SAFETY_SPAM], adapters_version: 5 })),
    ...overrides,
  } as unknown as Api;
}

const CONSOLE_OWNER: Me = { user: 'menlo@scissero.com', kind: 'console', admin: false };
const API_KEY_OWNER: Me = { user: 'api-key', kind: 'api-key', admin: false };
const HUB_CUSTOMER: Me = { user: 'customer-7', kind: 'hub', admin: false };

describe('Adapters', () => {
  it('groups every adapter by area, with its functions and licence when it is served', async () => {
    render(<Adapters api={fakeApi()} me={CONSOLE_OWNER} />);
    expect(await screen.findByText('Support')).toBeTruthy();
    expect(screen.getByText('Safety')).toBeTruthy();
    expect(screen.getByText('jeff.support.triage')).toBeTruthy();
    expect(screen.getByText('version 1.3.0')).toBeTruthy();
    expect(screen.getByText('route')).toBeTruthy();
    expect(screen.getByText('urgency')).toBeTruthy();
    expect(screen.getByText('Apache-2.0')).toBeTruthy();
    // jeff.safety.spam is not served, so GET /v1/functions names none of its functions: its licence is unknown,
    // and its functions are GET /v1/adapters' own, with their interfaces.
    expect(screen.getByText('licence unknown')).toBeTruthy();
    expect(screen.getByText('message_type')).toBeTruthy();
    expect(screen.getByText('classify')).toBeTruthy();
    // each adapter's one-line description, as its manifest writes it
    expect(screen.getByText('Routes a support request to a team and rates its urgency.')).toBeTruthy();
    expect(screen.getByText('Says whether a message is legitimate, spam or phishing.')).toBeTruthy();
    expect(screen.getByText('adapters_version 4 -- the engines\' own change counter.')).toBeTruthy();
  });

  it('an owner (a console session) can switch an adapter on, sending the full served set and reloading', async () => {
    const setServed = vi.fn(async () => ({ adapters: [TRIAGE, { ...SAFETY_SPAM, served: true }], adapters_version: 5 }));
    const adapters = vi.fn(async () => ({ adapters: [TRIAGE, SAFETY_SPAM], adapters_version: 4 }));
    const user = userEvent.setup();
    render(<Adapters api={fakeApi({ setServed, adapters })} me={CONSOLE_OWNER} />);
    await screen.findByText('jeff.safety.spam');
    const spamSwitch = screen.getByRole('switch', { name: 'Serve jeff.safety.spam' });
    expect(spamSwitch).not.toHaveProperty('disabled', true);
    await user.click(spamSwitch);
    expect(setServed).toHaveBeenCalledWith(['jeff.support.triage', 'jeff.safety.spam']);
    await waitFor(() => expect(adapters).toHaveBeenCalledTimes(2));
  });

  it('two quick changes both take effect: the second is built from the server\'s answer to the first', async () => {
    const OTHER: AdapterEntry = {
      id: 'jeff.safety.toxicity', aliases: ['toxicity'], version: '1.0.0', description: 'Says whether a message is toxic.',
      functions: [{ id: 'jeff.safety.toxicity.toxic', interface: 'flag', calibrated: false }], served: false,
    };
    let served = new Set(['jeff.support.triage']);
    const listing = () => [TRIAGE, SAFETY_SPAM, OTHER].map((entry) => ({ ...entry, served: served.has(entry.id) }));
    const releases: (() => void)[] = [];
    const setServed = vi.fn((ids: string[]) => new Promise<{ adapters: AdapterEntry[]; adapters_version: number }>((resolve) => {
      releases.push(() => {
        served = new Set(ids);
        resolve({ adapters: listing(), adapters_version: 5 });
      });
    }));
    const adapters = vi.fn(async () => ({ adapters: listing(), adapters_version: 4 }));
    const user = userEvent.setup();
    render(<Adapters api={fakeApi({ setServed, adapters } as unknown as Partial<Api>)} me={CONSOLE_OWNER} />);
    await screen.findByText('jeff.safety.toxicity');
    await user.click(screen.getByRole('switch', { name: 'Serve jeff.safety.spam' }));
    await user.click(screen.getByRole('switch', { name: 'Serve jeff.safety.toxicity' }));
    await waitFor(() => expect(setServed).toHaveBeenCalledTimes(1));
    releases[0]();
    await waitFor(() => expect(setServed).toHaveBeenCalledTimes(2));
    releases[1]();
    expect(new Set(setServed.mock.calls[1][0])).toEqual(new Set(['jeff.support.triage', 'jeff.safety.spam', 'jeff.safety.toxicity']));
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Serve jeff.safety.toxicity' }).getAttribute('aria-checked')).toBe('true'));
    expect(screen.getByRole('switch', { name: 'Serve jeff.safety.spam' }).getAttribute('aria-checked')).toBe('true');
  });

  it('an owner via the API key may also switch adapters', async () => {
    render(<Adapters api={fakeApi()} me={API_KEY_OWNER} />);
    await screen.findByText('jeff.safety.spam');
    expect(screen.getByRole('switch', { name: 'Serve jeff.safety.spam' })).not.toHaveProperty('disabled', true);
    expect(screen.queryByText(/only the server's owner/)).toBeNull();
  });

  it('an "open" caller is the owner too, on a server with no credentials configured at all', async () => {
    const OPEN_CALLER: Me = { user: 'anonymous', kind: 'open', admin: false };
    render(<Adapters api={fakeApi()} me={OPEN_CALLER} />);
    await screen.findByText('jeff.safety.spam');
    expect(screen.getByRole('switch', { name: 'Serve jeff.safety.spam' })).not.toHaveProperty('disabled', true);
    expect(screen.queryByText(/only the server's owner/)).toBeNull();
  });

  it('a non-owner (a hub token) sees the switches disabled, with a note', async () => {
    render(<Adapters api={fakeApi()} me={HUB_CUSTOMER} />);
    await screen.findByText('jeff.safety.spam');
    expect(screen.getByRole('switch', { name: 'Serve jeff.safety.spam' })).toHaveProperty('disabled', true);
    expect(screen.getByText(/only the server's owner \(its API key or a console session\) can/)).toBeTruthy();
    expect(screen.getByText(/Signed in as hub/)).toBeTruthy();
  });

  it('with nobody signed in yet (me still loading), the switches are disabled too', async () => {
    render(<Adapters api={fakeApi()} me={null} />);
    await screen.findByText('jeff.safety.spam');
    expect(screen.getByRole('switch', { name: 'Serve jeff.safety.spam' })).toHaveProperty('disabled', true);
    expect(screen.getByText('Checking who is signed in.')).toBeTruthy();
    expect(screen.queryByText(/unknown:/)).toBeNull();
  });

  it('a 403 refusal (a hub token somehow reaching PUT) shows the server\'s message, not a silent no-op', async () => {
    const setServed = vi.fn(async () => {
      throw new ApiError({ status: 403, type: 'NotLicensed', message: 'the hub token for customer-7 cannot change the server', details: {}, requestId: 'req-403' });
    });
    const user = userEvent.setup();
    render(<Adapters api={fakeApi({ setServed })} me={CONSOLE_OWNER} />);
    await screen.findByText('jeff.safety.spam');
    await user.click(screen.getByRole('switch', { name: 'Serve jeff.safety.spam' }));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('NotLicensed: the hub token for customer-7 cannot change the server');
    expect(alert.textContent).toContain('Request id req-403');
  });

  it('a public server\'s refusal (422) shows its message', async () => {
    const setServed = vi.fn(async () => {
      throw new ApiError({ status: 422, type: 'InvalidRequest', message: "This server is public: its adapters stay as started; change them on the private server", details: {}, requestId: 'req-422' });
    });
    const user = userEvent.setup();
    render(<Adapters api={fakeApi({ setServed })} me={CONSOLE_OWNER} />);
    await screen.findByText('jeff.support.triage');
    await user.click(screen.getByRole('switch', { name: 'Serve jeff.support.triage' }));
    expect(await screen.findByText(/This server is public/)).toBeTruthy();
  });

  it('a failure to load the catalogue is shown, not swallowed', async () => {
    const adapters = vi.fn(async () => {
      throw new ApiError({ status: 500, type: 'EngineError', message: 'the engines serve different adapters', details: {}, requestId: 'req-500' });
    });
    render(<Adapters api={fakeApi({ adapters })} me={CONSOLE_OWNER} />);
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('EngineError: the engines serve different adapters');
  });

  it('an empty catalogue says so', async () => {
    render(<Adapters api={fakeApi({ adapters: vi.fn(async () => ({ adapters: [], adapters_version: 0 })), functions: vi.fn(async () => ({ base_format: { format: 'bf16' }, functions: [] })) })} me={CONSOLE_OWNER} />);
    expect(await screen.findByText('The catalogue has no adapters')).toBeTruthy();
  });
});
