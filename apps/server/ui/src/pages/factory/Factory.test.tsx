import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ApiError, type Api } from '../../api';
import { APPROVED, REPLAY, RUN } from '../../test/factory';
import { Factory } from './Factory';

type Answer = unknown | (() => unknown);

/** A fake console API whose factory calls are answered by "<METHOD> <path>"; any other call fails the test. The
 * event stream never answers, as a run with no new events. */
function fakeApi(routes: Record<string, Answer>) {
  const factory = vi.fn(async (method: string, path: string) => {
    const key = `${method} ${path}`;
    if (!(key in routes)) throw new Error(`No fake answer for ${key}`);
    const answer = routes[key];
    return typeof answer === 'function' ? (answer as () => unknown)() : answer;
  });
  const api = {
    factory,
    factoryText: vi.fn(async () => 'text'),
    factoryStream: vi.fn(() => new Promise<Response>(() => {})),
  } as unknown as Api;
  return { api, factory };
}

const OWNER_MODE = { mode: 'owner', replays: true, description_check: true, preview: false };
const OFF_MODE = { mode: 'off', replays: true, description_check: false, preview: false };

describe('Factory', () => {
  it('shows the worker-down banner when the server builds adapters and no worker is running', async () => {
    const { api } = fakeApi({
      'GET /mode': OWNER_MODE, 'GET /replays': { replays: [] },
      'GET /worker': { running: false, heartbeat_age_s: 42.3, current_job: null },
    });
    render(<Factory api={api} onTry={() => undefined} />);
    const banner = await screen.findByText(/Worker not running: start/);
    expect(banner.textContent).toBe('Worker not running: start jeff-server factory worker');
    expect(screen.getByText(/Its last heartbeat was 42 s ago/)).toBeTruthy();
  });

  it('a running worker is a status line naming the job it runs', async () => {
    const { api } = fakeApi({
      'GET /mode': OWNER_MODE, 'GET /replays': { replays: [] },
      'GET /worker': { running: true, heartbeat_age_s: 2, current_job: { id: 'j1', run: 'parcel-claims-1', steps: ['train'], queued: RUN.created } },
    });
    render(<Factory api={api} onTry={() => undefined} />);
    expect(await screen.findByText(/Factory worker running · heartbeat 2 s ago · running Train of parcel-claims-1/)).toBeTruthy();
    expect(screen.queryByText(/Worker not running/)).toBeNull();
  });

  it('with the factory off, shows the replays and the off message, and never asks for the worker', async () => {
    const { api, factory } = fakeApi({ 'GET /mode': OFF_MODE, 'GET /replays': { replays: [] } });
    render(<Factory api={api} onTry={() => undefined} />);
    expect(await screen.findByText('Building adapters is switched off on this server (JEFF_FACTORY=off)')).toBeTruthy();
    expect(await screen.findByText('No recorded runs yet.')).toBeTruthy();
    expect(screen.queryByRole('tab', { name: 'Live' })).toBeNull();
    expect(factory.mock.calls.map(([method, path]) => `${method} ${path}`)).not.toContain('GET /worker');
  });

  it('a refused mode call shows the server\'s typed error', async () => {
    const { api } = fakeApi({
      'GET /mode': () => {
        throw new ApiError({ status: 403, type: 'NotLicensed', message: 'A hub token cannot use the factory', details: {}, requestId: 'req-403' });
      },
    });
    render(<Factory api={api} onTry={() => undefined} />);
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('NotLicensed: A hub token cannot use the factory');
    expect(alert.textContent).toContain('Request id req-403');
  });

  it('a 409 Conflict when queueing the rest of a run is shown in an Alert with its type and message', async () => {
    const { api, factory } = fakeApi({
      'GET /mode': OWNER_MODE, 'GET /replays': { replays: [] },
      'GET /worker': { running: true, heartbeat_age_s: 1, current_job: null },
      'GET /runs': { runs: [{ id: RUN.id, created: RUN.created, status: 'review done', clean: true, approved: RUN.approved, name: 'parcel-claims' }], busy: null },
      [`GET /runs/${RUN.id}`]: APPROVED,
      [`POST /runs/${RUN.id}/run-rest`]: () => {
        throw new ApiError({ status: 409, type: 'Conflict', message: `Run ${RUN.id} already has a job waiting (generate); wait for it to end`, details: {}, requestId: 'req-409' });
      },
    });
    const user = userEvent.setup();
    render(<Factory api={api} onTry={() => undefined} />);
    await user.click(await screen.findByRole('tab', { name: 'Live' }));
    await user.selectOptions(await screen.findByRole('combobox', { name: 'Run' }), RUN.id);
    expect(await screen.findByText(/Still to run: Prepare generation → Write the rows/)).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Run the rest' }));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain(`Conflict: Run ${RUN.id} already has a job waiting (generate); wait for it to end`);
    expect(alert.textContent).toContain('Request id req-409');
    expect(factory).toHaveBeenCalledWith('POST', `/runs/${RUN.id}/run-rest`, undefined);
  });

  it('a replay draws each card\'s step list from the recorded events, at the playback position', async () => {
    const { api } = fakeApi({
      'GET /mode': OFF_MODE,
      'GET /replays': { replays: [{ id: RUN.id, name: 'parcel-claims', summary: '', created: RUN.created, accuracy: { synthetic: 0.96 } }] },
      [`GET /replays/${RUN.id}`]: REPLAY,
    });
    render(<Factory api={api} onTry={() => undefined} />);
    const describeSteps = await screen.findByRole('list', { name: 'Describe and confirm steps' });
    // At the start of the recording only the description check has started.
    expect(within(describeSteps).getAllByRole('listitem').map((item) => item.textContent?.includes('Check the description (teacher)'))[0]).toBe(true);
    expect(within(describeSteps).getByLabelText('running')).toBeTruthy();
    expect(within(describeSteps).getAllByLabelText('waiting')).toHaveLength(3);

    const slider = screen.getByRole('slider', { name: 'Position in the run' });
    fireEvent.change(slider, { target: { value: slider.getAttribute('max') } });

    for (const card of ['Describe and confirm', 'Generate the data', 'Train', 'Publish']) {
      const steps = screen.getByRole('list', { name: `${card} steps` });
      expect(within(steps).queryByLabelText('waiting'), card).toBeNull();
      expect(within(steps).queryByLabelText('running'), card).toBeNull();
    }
    const evaluate = screen.getByRole('list', { name: 'Evaluate steps' });
    expect(within(evaluate).getByText('Held-out synthetic test').parentElement?.textContent).toMatch(/· 19 s$/);
    // No independent test was run: that step stays waiting.
    expect(within(evaluate).getAllByLabelText('waiting')).toHaveLength(1);
    expect(within(screen.getByRole('list', { name: 'Train steps' })).getByText('Train').parentElement?.textContent).toMatch(/· 3 min 09 s$/);
  });

  it('a published run names its function and shows how to call it through /v1/decide', async () => {
    const { api } = fakeApi({
      'GET /mode': OFF_MODE,
      'GET /replays': { replays: [{ id: RUN.id, name: 'parcel-claims', summary: '', created: RUN.created, accuracy: {} }] },
      [`GET /replays/${RUN.id}`]: REPLAY,
    });
    render(<Factory api={api} onTry={() => undefined} />);
    const slider = await screen.findByRole('slider', { name: 'Position in the run' });
    fireEvent.change(slider, { target: { value: slider.getAttribute('max') } });
    const publish = screen.getByRole('region', { name: 'Publish' });
    expect(within(publish).getByText('jeff.factory.parcel-claims')).toBeTruthy();
    const snippet = JSON.parse(within(publish).getByText(/"calls"/).textContent as string);
    expect(snippet).toEqual({ calls: [{ function: 'jeff.factory.parcel-claims.claim', input: 'The sole came off', context: { shop: 'A shoe shop' } }] });
    // A replay is from another time (or server): no "try it", which only a live run on this server offers.
    expect(within(publish).queryByRole('button', { name: /Try it in the playground/ })).toBeNull();
  });

  it('a step that failed in the recording is shown with the error it recorded', async () => {
    const failing = { ...REPLAY, events: [...REPLAY.events.slice(0, 16), { t: REPLAY.events[16].t, kind: 'step.failed', step: 'checks', error: 'ValueError: 3 test families also appear in training splits' }] };
    const { api } = fakeApi({
      'GET /mode': OFF_MODE,
      'GET /replays': { replays: [{ id: RUN.id, name: 'parcel-claims', summary: '', created: RUN.created, accuracy: {} }] },
      [`GET /replays/${RUN.id}`]: failing,
    });
    render(<Factory api={api} onTry={() => undefined} />);
    const slider = await screen.findByRole('slider', { name: 'Position in the run' });
    fireEvent.change(slider, { target: { value: slider.getAttribute('max') } });
    await waitFor(() => expect(screen.getByText('Check the data failed')).toBeTruthy());
    expect(screen.getByText('ValueError: 3 test families also appear in training splits')).toBeTruthy();
  });
});
