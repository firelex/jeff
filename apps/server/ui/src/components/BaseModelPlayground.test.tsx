import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ApiError, type Api, type SystemOneRequest, type SystemOneResponse } from '../api';
import { BaseModelPlayground, questionsWithTemplate } from './BaseModelPlayground';
import { SystemOneResult } from './SystemOneResult';

const MODELS = { models: [
  { name: 'jeff', description: 'Local Jeff text and image decisions.', release_date: '2026-10-01' },
  { name: 'triage', description: 'Support ticket triage', release_date: '2026-10-01' },
] };

const ANSWER: SystemOneResponse = {
  model: 'jeff',
  answers: {
    incident: { type: 'choice', choice: 'stolen_login', probabilities: { stolen_login: 0.8, malware: 0.1, data_leak: 0.05, false_alarm: 0.05 }, confidence: 0.8 },
    severity: { type: 'score', score: 1.7, probabilities: { '0': 0.1, '1': 0.1, '2': 0.8 }, legend: { '0': 'Low', '1': 'Medium', '2': 'High' }, confidence: 0.8 },
    lock_now: { type: 'noul', noul: 0.9 },
  },
  usage: { input_tokens: 412, output_tokens: 0, orders: 1 },
  timing: { server_ms: 4.25 },
};

function fakeApi(overrides: Partial<Api>): Api {
  return { models: vi.fn(async () => MODELS), ...overrides } as unknown as Api;
}

describe('BaseModelPlayground', () => {
  it('loads a base example, sends it to /v1/systemone with the chosen model, and shows each answer', async () => {
    const systemOne = vi.fn(async (_request: SystemOneRequest) => ({ body: ANSWER, requestId: 'req-7', roundTripMs: 9 }));
    const user = userEvent.setup();
    render(<BaseModelPlayground api={fakeApi({ systemOne })} health={{ status: 'ready', modalities: ['text'] }} />);
    await user.click(await screen.findByText('Triage a security alert'));
    await user.click(screen.getByRole('switch', { name: 'Both option orders' }));
    await user.click(screen.getByRole('button', { name: 'Run' }));

    const request = systemOne.mock.calls[0][0];
    expect(request.model).toBe('jeff');
    expect(request.orders).toBe(2);
    expect(typeof request.state).toBe('string');
    expect(Object.keys(request.questions)).toEqual(['incident', 'severity', 'lock_now']);
    expect(await screen.findByText('stolen_login', { selector: 'p' })).toBeTruthy();
    expect(screen.getByText('True')).toBeTruthy();
    expect(screen.getByText('req-7')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'This server does not read images' })).toHaveProperty('disabled', true);
  });

  it('a malformed GET /v1/models answer shows the problem in an error alert', async () => {
    const models = vi.fn(async () => ({}));
    render(<BaseModelPlayground api={fakeApi({ models } as unknown as Partial<Api>)} health={null} />);
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('TypeError');
  });

  it('shows the server\'s typed error with its request id', async () => {
    const systemOne = vi.fn(async () => {
      throw new ApiError({ status: 404, type: 'NotInstalled', message: 'base is not installed', details: {}, requestId: 'req-404' });
    });
    const user = userEvent.setup();
    render(<BaseModelPlayground api={fakeApi({ systemOne })} health={null} />);
    await user.click(await screen.findByText('Read a product review'));
    await user.click(screen.getByRole('button', { name: 'Run' }));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('NotInstalled: base is not installed');
    expect(alert.textContent).toContain('Request id req-404');
  });

  it('with no models to ask, says so and cannot run', async () => {
    render(<BaseModelPlayground api={fakeApi({ models: vi.fn(async () => ({ models: [] })) })} health={null} />);
    expect(await screen.findByText('No model to ask: GET /v1/models lists none')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Run' })).toHaveProperty('disabled', true);
  });

  it('a failure to list the models is shown', async () => {
    const models = vi.fn(async () => {
      throw new ApiError({ status: 503, type: 'Busy', message: 'All engines are busy', details: {}, requestId: 'r' });
    });
    render(<BaseModelPlayground api={fakeApi({ models })} health={null} />);
    expect((await screen.findByRole('alert')).textContent).toContain('Busy: All engines are busy');
  });
});

describe('SystemOneResult', () => {
  it('shows the timings, every answer, and the response and request JSON', async () => {
    const user = userEvent.setup();
    render(<SystemOneResult result={{ body: ANSWER, requestId: 'req-9', roundTripMs: 12.5 }} requestJson='{"model": "jeff"}' />);
    expect(screen.getByText('4.25')).toBeTruthy();
    expect(screen.getByText('12.5')).toBeTruthy();
    expect(screen.getByText('412')).toBeTruthy();
    expect(screen.getByText('1.70 of 2')).toBeTruthy();
    expect(screen.getAllByRole('meter')).toHaveLength(4 + 3 + 2);
    await user.click(screen.getByRole('tab', { name: 'Response JSON' }));
    expect(screen.getByText(/"server_ms": 4.25/)).toBeTruthy();
    await user.click(screen.getByRole('tab', { name: 'Request JSON' }));
    expect(screen.getByText('{"model": "jeff"}')).toBeTruthy();
  });
});

describe('questionsWithTemplate', () => {
  const template = { type: 'noul' as const, instructions: 'The sender wants a reply from us.' };

  it('starts the questions from an empty editor', () => {
    expect(JSON.parse(questionsWithTemplate('', template).text as string)).toEqual({ question_1: template });
  });

  it('adds the template under the next free name', () => {
    const text = JSON.stringify({ question_1: template, question_2: template });
    expect(Object.keys(JSON.parse(questionsWithTemplate(text, template).text as string))).toEqual(['question_1', 'question_2', 'question_3']);
  });

  it('refuses with the parse problem rather than replacing questions that are not valid JSON', () => {
    const result = questionsWithTemplate('{"incident": {"type": "choice",', template);
    expect(result.text).toBeUndefined();
    expect(result.problem).toMatch(/JSON/);
  });
});
