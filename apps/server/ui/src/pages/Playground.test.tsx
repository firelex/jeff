import { describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Api } from '../api';
import { SPAM, TRIAGE_ROUTE, decision } from '../test/fixtures';
import { Playground } from './Playground';

function fakeApi(): Api {
  return {
    functions: vi.fn(async () => ({ base_format: { format: 'bf16' }, functions: [TRIAGE_ROUTE] })),
    examples: vi.fn(async () => ({ examples: [] })),
    models: vi.fn(async () => ({ models: [{ name: 'jeff', description: 'The base model.', release_date: '2026-10-01' }] })),
  } as unknown as Api;
}

describe('Playground', () => {
  it('each tab\'s panel is a tabpanel labelled by its tab, and the JSON editors have accessible names', async () => {
    const user = userEvent.setup();
    render(<Playground api={fakeApi()} health={null} seed={null} onSeedApplied={() => {}} />);
    const functions = await screen.findByRole('tabpanel', { name: 'Functions' });
    expect(within(functions).getByRole('tab', { name: 'Form' })).toBeTruthy();
    await user.click(within(functions).getByRole('tab', { name: 'JSON request' }));
    const jsonPanel = within(functions).getByRole('tabpanel', { name: 'JSON request' });
    expect(within(jsonPanel).getByRole('textbox', { name: 'POST /v1/decide request body' })).toBeTruthy();

    await user.click(screen.getByRole('tab', { name: 'Base model' }));
    expect(screen.getByRole('tabpanel', { name: 'Base model' })).toBeTruthy();
  });

  const PROPRIETARY = { ...SPAM, id: 'jeff.operations.sample.action', licence: { kind: 'proprietary' as const, adapter: 'Example proprietary' } };
  const spamExample = (name: string, message: string, expected?: boolean) => ({
    name, call: { function: SPAM.id, input: message, context: {}, labels: null }, ...(expected === undefined ? {} : { expected }),
  });

  function examplesApi(decided = decision({ value: true, function: SPAM.id })): Api {
    return {
      functions: vi.fn(async () => ({ base_format: { format: 'bf16' }, functions: [SPAM, PROPRIETARY] })),
      examples: vi.fn(async () => ({
        examples: [
          spamExample(`${SPAM.id} (test row s0)`, 'Hello'),
          spamExample('true · Win a prize now', 'Win a prize now', true),
          { name: 'block · Payment', call: { function: PROPRIETARY.id, input: 'Payment', context: {}, labels: null }, expected: 'block' },
        ],
      })),
      decide: vi.fn(async () => ({ body: { decisions: [decided] }, requestId: 'r1', roundTripMs: 5 })),
      models: vi.fn(async () => ({ models: [] })),
    } as unknown as Api;
  }

  it('the licence filter narrows the functions to open-source or proprietary adapters, with counts', async () => {
    const user = userEvent.setup();
    render(<Playground api={examplesApi()} health={null} seed={null} onSeedApplied={() => {}} />);
    const licence = await screen.findByRole('combobox', { name: 'Licence' });
    expect(within(licence).getAllByRole('option').map((option) => option.textContent))
      .toEqual(['All adapters (2)', 'Open source (1)', 'Proprietary (1)']);
    const functionIds = () => within(screen.getByRole('combobox', { name: 'Function' })).getAllByRole('option')
      .filter((option) => !(option as HTMLOptionElement).disabled).map((option) => option.textContent);
    expect(functionIds()).toEqual([PROPRIETARY.id, SPAM.id]);
    await user.selectOptions(licence, 'proprietary');
    expect(functionIds()).toEqual([PROPRIETARY.id]);
    await user.selectOptions(licence, 'open');
    expect(functionIds()).toEqual([SPAM.id]);
  });

  it('offers only the chosen function\'s examples, and compares the run with the test set\'s answer', async () => {
    const user = userEvent.setup();
    render(<Playground api={examplesApi()} health={null} seed={null} onSeedApplied={() => {}} />);
    const example = await screen.findByRole('combobox', { name: 'Example' });
    expect((example as HTMLSelectElement).disabled).toBe(true);
    await user.selectOptions(screen.getByRole('combobox', { name: 'Function' }), SPAM.id);
    expect(within(example).getAllByRole('option').map((option) => option.textContent))
      .toEqual(['Load an example (2)', 'Reference case (test row s0)', 'true · Win a prize now']);

    await user.selectOptions(example, '1');
    expect(await screen.findByText('Test set answer')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: /Run/ }));
    expect(await screen.findByText('Jeff agrees')).toBeTruthy();

    await user.selectOptions(example, '0');
    expect(screen.queryByText('Test set answer')).toBeNull();
  });

  it('names the answer Jeff gave when it differs from the test set\'s', async () => {
    const user = userEvent.setup();
    render(<Playground api={examplesApi(decision({ value: false, function: SPAM.id }))} health={null} seed={null} onSeedApplied={() => {}} />);
    await user.selectOptions(await screen.findByRole('combobox', { name: 'Function' }), SPAM.id);
    await user.selectOptions(screen.getByRole('combobox', { name: 'Example' }), '1');
    await user.click(screen.getByRole('button', { name: /Run/ }));
    expect(await screen.findByText('Jeff answered false')).toBeTruthy();
  });
});
