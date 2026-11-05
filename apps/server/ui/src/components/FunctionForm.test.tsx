import { useState } from 'react';
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { FunctionListing } from '../api';
import { FunctionForm, FormError, buildCall, emptyForm, formFromCall, type FormState } from './FunctionForm';
import { SPAM, TRIAGE_ROUTE, TRIAGE_URGENCY } from '../test/fixtures';

// Renders the form with its own state, and hands back a reader for the latest state.
function renderForm(listing: FunctionListing, initial: FormState = emptyForm(listing)) {
  let latest = initial;
  function Harness() {
    const [value, setValue] = useState(initial);
    return <FunctionForm listing={listing} value={value} onChange={(next) => { latest = next; setValue(next); }} />;
  }
  render(<Harness />);
  return () => latest;
}

describe('FunctionForm', () => {
  it('builds a /v1/decide call for a choice function whose labels the caller passes', async () => {
    const user = userEvent.setup();
    const state = renderForm(TRIAGE_ROUTE);
    await user.type(screen.getByLabelText('Input: message'), 'The wardrobe arrived crushed.');
    await user.type(screen.getByLabelText('Context: company'), 'A furniture retailer.');
    await user.type(screen.getByLabelText('Context: channel'), 'email');
    // The fixed first option is shown, not editable: it is always sent by the server itself.
    expect(screen.getByText('other')).toBeTruthy();
    await user.type(screen.getByLabelText('Label 1 key'), 'deliveries');
    await user.type(screen.getByLabelText('Label 1 description'), 'Late, lost or damaged deliveries');
    await user.click(screen.getByRole('button', { name: 'Add a label' }));
    await user.type(screen.getByLabelText('Label 2 key'), 'refunds');

    expect(buildCall(TRIAGE_ROUTE, state())).toEqual({
      function: 'jeff.support.triage.route',
      input: 'The wardrobe arrived crushed.',
      context: { company: 'A furniture retailer.', channel: 'email' },
      labels: { deliveries: 'Late, lost or damaged deliveries', refunds: null },
    });
  });

  it('builds a call with the required context and no labels for a function whose labels are fixed', async () => {
    const user = userEvent.setup();
    const state = renderForm(TRIAGE_URGENCY);
    expect(screen.getByText('Handle immediately')).toBeTruthy();
    expect(screen.queryByLabelText('Label 1 key')).toBeNull();
    await user.type(screen.getByLabelText('Input: message'), 'Two doors are split.');
    await user.type(screen.getByLabelText('Context: company'), 'A furniture retailer.');
    await user.type(screen.getByLabelText('Context: channel'), 'web form');

    expect(buildCall(TRIAGE_URGENCY, state())).toEqual({
      function: 'jeff.support.triage.urgency',
      input: 'Two doors are split.',
      context: { company: 'A furniture retailer.', channel: 'web form' },
    });
  });

  it('marks required context, and leaves a missing field out so the server names it', async () => {
    const user = userEvent.setup();
    const state = renderForm(TRIAGE_URGENCY);
    // The input and both context fields.
    expect(screen.getAllByText('required')).toHaveLength(3);
    await user.type(screen.getByLabelText('Input: message'), 'Hello');
    await user.type(screen.getByLabelText('Context: company'), 'A shop.');
    expect(buildCall(TRIAGE_URGENCY, state())).toEqual({
      function: 'jeff.support.triage.urgency', input: 'Hello', context: { company: 'A shop.' },
    });
  });

  it('names and sends a context field whose key has spaces by its code name', async () => {
    const user = userEvent.setup();
    const spaced: FunctionListing = {
      ...SPAM,
      context: [{ key: 'counterparty name', code_name: 'counterparty_name', description: 'Who it is from.', required: true }],
    };
    const state = renderForm(spaced);
    await user.type(screen.getByLabelText('Input: message'), 'Win a prize');
    await user.type(screen.getByLabelText('Context: counterparty_name'), 'Acme Ltd');
    expect(buildCall(spaced, state())).toEqual({
      function: 'jeff.safety.spam.is_spam', input: 'Win a prize', context: { counterparty_name: 'Acme Ltd' },
    });
    expect(formFromCall(spaced, { function: spaced.id, input: 'x', context: { counterparty_name: 'Acme Ltd' } })
      .context.counterparty_name).toEqual({ mode: 'text', text: 'Acme Ltd' });
    expect(() => formFromCall(spaced, { function: spaced.id, input: 'x', context: { 'counterparty name': 'Acme Ltd' } }))
      .toThrow("jeff.safety.spam.is_spam has no context field counterparty name; context is given by code name, so pass 'counterparty name' as counterparty_name");
  });

  it('takes a plain-text input as text only, and refuses an example whose input is not a string', async () => {
    const user = userEvent.setup();
    const plain: FunctionListing = {
      ...SPAM, context: [], input: { key: 'puzzle', code_name: 'puzzle', description: 'The puzzle.', form: 'text' },
    };
    const state = renderForm(plain);
    expect(screen.queryByRole('button', { name: 'puzzle as JSON' })).toBeNull();
    await user.type(screen.getByLabelText('Input: puzzle'), 'Who has the ball?');
    expect(buildCall(plain, state())).toEqual({ function: 'jeff.safety.spam.is_spam', input: 'Who has the ball?' });
    expect(() => formFromCall(plain, { function: plain.id, input: { puzzle: 'x' } }))
      .toThrow('jeff.safety.spam.is_spam takes plain text: its input (puzzle) must be a string');
  });

  it('sends a context field as JSON when switched to JSON, and refuses JSON that does not parse', async () => {
    const user = userEvent.setup();
    const state = renderForm(SPAM);
    await user.type(screen.getByLabelText('Input: message'), 'Win a prize');
    await user.click(screen.getByRole('button', { name: 'channel as JSON' }));
    await user.type(screen.getByLabelText('Context: channel'), '{{"kind": "sms"}');
    expect(buildCall(SPAM, state())).toEqual({ function: 'jeff.safety.spam.is_spam', input: 'Win a prize', context: { channel: { kind: 'sms' } } });

    await user.type(screen.getByLabelText('Context: channel'), ',');
    expect(() => buildCall(SPAM, state())).toThrow(FormError);
    expect(() => buildCall(SPAM, state())).toThrow(/Context channel is not valid JSON/);
  });

  it('an empty input is refused before sending', () => {
    expect(() => buildCall(SPAM, emptyForm(SPAM))).toThrow('The input (message) is empty');
  });

  it('loads an example call back into the form, JSON values as JSON', () => {
    const form = formFromCall(TRIAGE_ROUTE, {
      function: 'jeff.support.triage.route', input: 'Hi', context: { company: 'Shop', channel: { kind: 'chat' } },
      labels: { k: 'A team' },
    });
    expect(form.input).toEqual({ mode: 'text', text: 'Hi' });
    expect(form.context.channel.mode).toBe('json');
    expect(buildCall(TRIAGE_ROUTE, form)).toEqual({
      function: 'jeff.support.triage.route', input: 'Hi', context: { company: 'Shop', channel: { kind: 'chat' } }, labels: { k: 'A team' },
    });
    expect(() => formFromCall(TRIAGE_ROUTE, { function: 'jeff.support.triage.route', input: 'x', context: { nope: 1 } }))
      .toThrow('jeff.support.triage.route has no context field nope');
  });

  it('a subset function sends the chosen keys, its required ones always included', async () => {
    const subset: FunctionListing = {
      ...SPAM, id: 'jeff.x.y.kind', type: 'choice',
      labels: { source: 'subset', fixed: { a: 'A', b: 'B', c: 'C' }, required: ['a'] },
    };
    const user = userEvent.setup();
    const state = renderForm(subset);
    await user.type(screen.getByLabelText('Input: message'), 'm');
    expect((screen.getByLabelText('a') as HTMLInputElement).disabled).toBe(true);
    await user.click(screen.getByLabelText('c'));
    expect(buildCall(subset, state()).labels).toEqual(['a', 'c']);
  });
});

describe('FunctionForm labels, strictly', () => {
  const subset: FunctionListing = {
    ...SPAM, id: 'jeff.x.y.kind', type: 'choice',
    labels: { source: 'subset', fixed: { a: 'A', b: 'B', c: 'C' }, required: ['a'] },
  };
  const describe_: FunctionListing = {
    ...SPAM, id: 'jeff.x.y.grounded', type: 'choice',
    labels: { source: 'describe', keys: ['supported', 'contradicted'] },
  };

  it('a subset call round-trips, and keys outside its labels are refused', () => {
    const call = { function: subset.id, input: 'm', labels: ['a', 'c'] };
    expect(buildCall(subset, formFromCall(subset, call))).toEqual(call);
    expect(() => formFromCall(subset, { ...call, labels: ['a', 'z'] })).toThrow(FormError);
    expect(() => formFromCall(subset, { ...call, labels: ['a', 'z'] })).toThrow('jeff.x.y.kind has no label(s) z');
  });

  it('a subset listing with no fixed labels is an error, not an empty list', () => {
    const broken: FunctionListing = { ...subset, labels: { source: 'subset' } };
    expect(() => buildCall(broken, { ...emptyForm(broken), input: { mode: 'text', text: 'm' } })).toThrow('names no fixed labels');
  });

  it('a describe call round-trips, and must give exactly its keys', () => {
    const call = { function: describe_.id, input: 'm', labels: { supported: 'The sources say so', contradicted: null } };
    expect(buildCall(describe_, formFromCall(describe_, call))).toEqual(call);
    expect(() => formFromCall(describe_, { ...call, labels: { supported: 'x' } })).toThrow('takes exactly the labels supported, contradicted');
    expect(() => formFromCall(describe_, { ...call, labels: { supported: 'x', contradicted: 'y', extra: 'z' } })).toThrow(FormError);
  });

  it('a caller-labels call with no labels at all is refused before sending', () => {
    const form = { ...emptyForm(TRIAGE_ROUTE), input: { mode: 'text' as const, text: 'Hi' } };
    expect(() => buildCall(TRIAGE_ROUTE, form)).toThrow('Add at least one label');
  });
});

describe('FunctionForm fixed labels that allow a subset (labels.subset true)', () => {
  // As v1.4 soc's action: fixed labels in the manifest's order, of which a caller may offer some.
  const soc: FunctionListing = {
    ...SPAM, id: 'jeff.soc.action', type: 'choice',
    labels: { source: 'fixed', fixed: { block: 'Block it', isolate: 'Isolate the host', ignore: null }, subset: true },
  };
  const fixedOnly: FunctionListing = {
    ...SPAM, id: 'jeff.x.y.fixed', type: 'choice', labels: { source: 'fixed', fixed: { a: 'A', b: 'B' }, subset: false },
  };

  it('shows a checkbox per label in the manifest order, all ticked, and then sends no labels', async () => {
    const user = userEvent.setup();
    const state = renderForm(soc);
    await user.type(screen.getByLabelText('Input: message'), 'Beaconing to a known C2 host');
    const boxes = screen.getAllByRole('checkbox') as HTMLInputElement[];
    expect(boxes.map((box) => box.getAttribute('aria-label'))).toEqual(['block', 'isolate', 'ignore']);
    expect(boxes.every((box) => box.checked && !box.disabled)).toBe(true);
    expect(buildCall(soc, state())).toEqual({ function: 'jeff.soc.action', input: 'Beaconing to a known C2 host' });
  });

  it('sends the chosen subset, in the manifest order, when it differs from all', async () => {
    const user = userEvent.setup();
    const state = renderForm(soc);
    await user.type(screen.getByLabelText('Input: message'), 'm');
    await user.click(screen.getByLabelText('block'));
    await user.click(screen.getByLabelText('block'));  // ticked again: it goes back to its place, first
    await user.click(screen.getByLabelText('isolate'));
    expect(buildCall(soc, state()).labels).toEqual(['block', 'ignore']);
  });

  it('refuses an empty selection with a clear message', async () => {
    const user = userEvent.setup();
    const state = renderForm(soc);
    await user.type(screen.getByLabelText('Input: message'), 'm');
    for (const key of ['block', 'isolate', 'ignore']) await user.click(screen.getByLabelText(key));
    expect(() => buildCall(soc, state())).toThrow(FormError);
    expect(() => buildCall(soc, state())).toThrow('Tick at least one label to offer');
  });

  it('an example call with a subset round-trips; unknown keys are refused', () => {
    const call = { function: soc.id, input: 'm', labels: ['ignore', 'block'] };
    expect(buildCall(soc, formFromCall(soc, call))).toEqual({ ...call, labels: ['block', 'ignore'] });
    expect(() => formFromCall(soc, { ...call, labels: ['block', 'escalate'] })).toThrow('jeff.soc.action has no label(s) escalate');
    expect(buildCall(soc, formFromCall(soc, { function: soc.id, input: 'm' }))).toEqual({ function: soc.id, input: 'm' });
  });

  it('fixed labels with subset false still show a plain list and take no labels', () => {
    render(<FunctionForm listing={fixedOnly} value={emptyForm(fixedOnly)} onChange={() => {}} />);
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
    expect(() => formFromCall(fixedOnly, { function: fixedOnly.id, input: 'm', labels: ['a'] }))
      .toThrow("The example's labels do not fit jeff.x.y.fixed");
  });
});
