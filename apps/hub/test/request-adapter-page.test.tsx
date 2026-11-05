import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { strToU8, zipSync } from 'fflate';
import { File as NodeFile } from 'node:buffer';
import { createRoutesStub } from 'react-router';
import { afterEach, expect, it, vi } from 'vitest';

// The file store's client: the zip "goes up" and the store answers the name it was stored as. Nothing reaches Vercel.
const put = vi.hoisted(() => vi.fn(async (_pathname: string, _file: unknown, _options: unknown) => ({ pathname: 'adapter-requests/u1/data-set-Xyz789.zip' })));
vi.mock('@vercel/blob/client', () => ({ put }));
import RequestAdapter, { CHECKING } from '../app/routes/request-adapter';

// The prerendered /request-adapter shell: it learns from GET /api/adapter-requests whether the visitor is signed
// in, shows the form, counts the pasted examples per option, keeps Send off until the minimums are met, shows every
// problem in its section, and on a 201 shows the request number (a stubbed fetch here).
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

function hub(post: (init: RequestInit) => Response = () => Response.json({ id: 'AR-ABCDEFGH' }, { status: 201 }),
  list = Response.json({ requests: [], dataSetFolder: 'adapter-requests/u1/' })) {
  const fetchImpl = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === '/api/adapter-requests/upload') return Response.json({ type: 'blob.generate-client-token', clientToken: 'vercel_blob_client_test', pathname: 'adapter-requests/u1/data-set-Xyz789.zip' });
    if (url !== '/api/adapter-requests') throw new Error(`no stubbed answer for ${url}`);
    return init?.method === 'POST' ? post(init) : list.clone();
  });
  vi.stubGlobal('fetch', fetchImpl);
  return fetchImpl;
}

function show() {
  const Stub = createRoutesStub([
    { path: '/request-adapter', Component: RequestAdapter },
    { path: '/signin', Component: () => <p>The sign-in page</p> },
  ]);
  return render(<Stub initialEntries={['/request-adapter']} />);
}

const type = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });

// Thirty emails, ten per team, as CSV.
const CSV = ['input,answer,tricky', ...Array.from({ length: 30 }, (_, i) => `"Email ${i}, about ${['billing', 'tech', 'none'][i % 3]}",${['billing', 'tech', 'none'][i % 3]},${i === 0 ? 'yes' : ''}`)].join('\n');

async function fillTheRest() {
  type('The question, in one sentence', 'Which team should handle this support email?');
  type('Why it matters, in one or two sentences', 'Emails reach the right team faster.');
  fireEvent.click(screen.getByLabelText('One option from a fixed list'));
  fireEvent.click(screen.getByLabelText('A "none" option'));
  type('Options: key 1', 'billing');
  type('Options: when "billing" is right', 'Payments and refunds.');
  type('Options: key 2', 'tech');
  type('Options: when "tech" is right', 'Bugs and errors.');
  type('Options: when "none" is right', 'Neither team.');
  fireEvent.click(screen.getByLabelText('One text'));
  type('Typical length (characters)', '400');
  type('Maximum length (characters)', '4000');
  type('Language or languages', 'English');
  type('Calls a day, roughly', '2000');
  fireEvent.click(screen.getAllByLabelText('No')[0]);
  type('What accuracy would make it useful?', 'Right 95 times in 100');
  fireEvent.click(screen.getAllByLabelText('Yes')[1]);
  fireEvent.click(screen.getByLabelText('I have the right to share these examples.'));
  fireEvent.click(screen.getByLabelText('They contain no personal data, or it has been removed.'));
}

async function fillAllButExamples() {
  await fillTheRest();
  fireEvent.click(screen.getByLabelText('Generate the data for me'));
}

it('sends a visitor who is not signed in to sign in, and back here afterwards', async () => {
  hub(undefined, Response.json({ error: { type: 'SignInRequired', message: 'sign in first', details: {} } }, { status: 401 }));
  show();
  expect(screen.getByRole('status').textContent).toBe(CHECKING);
  expect(await screen.findByText('The sign-in page')).toBeTruthy();
});

it('counts pasted examples per option, opens Send at the minimums, and shows the request number', async () => {
  const fetchImpl = hub();
  show();
  await screen.findByLabelText('The question, in one sentence');
  await act(async () => { await fillAllButExamples(); });
  const sendButton = screen.getByRole('button', { name: 'Send request' }) as HTMLButtonElement;
  expect(sendButton.disabled).toBe(true);
  type('Paste CSV or JSON Lines', CSV.split('\n').slice(0, 29).join('\n'));
  fireEvent.click(screen.getByRole('button', { name: 'Add pasted examples' }));
  expect(screen.getByText('Pasted: 28 examples added.')).toBeTruthy();
  expect(screen.getByLabelText('Examples per option').textContent).toBe('billing: 10tech: 9none: 9');
  expect(sendButton.disabled).toBe(true);
  type('Paste CSV or JSON Lines', ['input,answer,tricky', ...CSV.split('\n').slice(29)].join('\n'));
  fireEvent.click(screen.getByRole('button', { name: 'Add pasted examples' }));
  expect(screen.getByText((_, el) => el?.tagName === 'P' && (el.textContent ?? '').startsWith('30 of at least 30 examples'))).toBeTruthy();
  expect(sendButton.disabled).toBe(false);
  type('How many more real examples could you supply later?', '500');
  await act(async () => { fireEvent.click(sendButton); });
  const [, init] = fetchImpl.mock.calls.find(([, i]) => i?.method === 'POST')!;
  const body = JSON.parse(String(init!.body));
  expect(body.examples).toHaveLength(30);
  expect(body.examples[0]).toEqual({ input: 'Email 0, about billing', context: {}, answer: 'billing', reason: '', tricky: true });
  expect(body.answer.options.map((o: { key: string }) => o.key)).toEqual(['billing', 'tech', 'none']);
  expect(body.use).toEqual({ callsPerDay: 2000, speedMatters: false, accuracy: 'Right 95 times in 100', abstainBetter: true });
  expect(await screen.findByRole('heading', { level: 1, name: 'Request sent' })).toBeTruthy();
  expect(screen.getByText("Thank you. We'll reply within a few days by email.")).toBeTruthy();
  expect(screen.getByText('AR-ABCDEFGH')).toBeTruthy();
});

it('the first Send with a problem shows it in its section, and sends nothing', async () => {
  const fetchImpl = hub();
  show();
  await screen.findByLabelText('The question, in one sentence');
  await act(async () => { await fillAllButExamples(); });
  type('The question, in one sentence', 'Too short');
  type('Paste CSV or JSON Lines', CSV);
  fireEvent.click(screen.getByRole('button', { name: 'Add pasted examples' }));
  type('How many more real examples could you supply later?', '0');
  expect(screen.queryByText('The question must be 10 to 300 characters; it has 9.')).toBeNull();
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Send request' })); });
  const section = screen.getByRole('region', { name: '1. The decision' });
  expect(section.textContent).toContain('The question must be 10 to 300 characters; it has 9.');
  expect(screen.getByText('1 thing needs fixing before the request can be sent; each is shown in its section above.')).toBeTruthy();
  expect(fetchImpl.mock.calls.filter(([, i]) => i?.method === 'POST')).toEqual([]);
});

it("a refused send says why, in the hub's words", async () => {
  hub(() => Response.json({ error: { type: 'RequestRefused', details: {},
    message: 'You have 3 open adapter requests, the most an account may have at once; wait until one is delivered or declined.' } }, { status: 409 }));
  show();
  await screen.findByLabelText('The question, in one sentence');
  await act(async () => { await fillAllButExamples(); });
  type('Paste CSV or JSON Lines', CSV);
  fireEvent.click(screen.getByRole('button', { name: 'Add pasted examples' }));
  type('How many more real examples could you supply later?', '0');
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Send request' })); });
  expect(screen.getByText('You have 3 open adapter requests, the most an account may have at once; wait until one is delivered or declined.')).toBeTruthy();
});

it('a pasted file of the wrong shape is refused, naming what is wrong', async () => {
  hub();
  show();
  await screen.findByLabelText('The question, in one sentence');
  fireEvent.click(screen.getByLabelText('One option from a fixed list'));
  fireEvent.click(screen.getByLabelText('No answer (abstain)'));
  fireEvent.click(screen.getByLabelText('One text'));
  fireEvent.click(screen.getByLabelText('Generate the data for me'));
  type('Paste CSV or JSON Lines', 'text,label\nx,y');
  fireEvent.click(screen.getByRole('button', { name: 'Add pasted examples' }));
  expect(screen.getByRole('alert').textContent).toBe('The CSV header has no column "input", "answer"; the columns are input, answer, reason, tricky.');
});

it('says what "Generate the data for me" does, and where the examples go', async () => {
  hub();
  show();
  await screen.findByLabelText('The question, in one sentence');
  fireEvent.click(screen.getByLabelText('Generate the data for me'));
  expect(screen.getByText('We write the training data in the same style with our own language model, and test the adapter on a share of your examples it never saw.')).toBeTruthy();
  expect(screen.getByText('Your examples are given to our own language model to write the training data; they are not sent to any outside service.')).toBeTruthy();
});

// A zip of 330 rows, 110 per team. Node's File, which reads as a stream as a browser's does (the test DOM's File
// cannot).
function aZip(rows = 330) {
  const csv = ['input,answer', ...Array.from({ length: rows }, (_, i) => `Email ${i},${['billing', 'tech', 'none'][i % 3]}`)].join('\n');
  return new NodeFile([zipSync({ 'emails.csv': strToU8(csv) })], 'emails.zip', { type: 'application/zip' });
}

it('checks a data set in the browser, shows its rows per option, uploads it on Send and sends where it was stored', async () => {
  const fetchImpl = hub();
  show();
  await screen.findByLabelText('The question, in one sentence');
  await act(async () => { await fillTheRest(); });
  fireEvent.click(screen.getByLabelText('I have a data set'));
  type('How many more real examples could you supply later?', '0');
  const sendButton = screen.getByRole('button', { name: 'Send request' }) as HTMLButtonElement;
  expect(sendButton.disabled).toBe(true);
  await act(async () => { fireEvent.change(screen.getByLabelText('Choose the zip of your data set'), { target: { files: [aZip()] } }); });
  await waitFor(() => expect(screen.getByLabelText('Rows per option').textContent).toBe('billing: 110tech: 110none: 110'));
  expect(screen.getByText((_, el) => el?.tagName === 'P' && (el.textContent ?? '').startsWith('emails.zip: 330 rows in 1 file'))).toBeTruthy();
  expect(sendButton.disabled).toBe(false);
  await act(async () => { fireEvent.click(sendButton); });
  expect(put).toHaveBeenCalledTimes(1);
  // The pathname the hub picked for the token (re-review R1), not the one the page asked for.
  expect(put.mock.calls[0][0]).toBe('adapter-requests/u1/data-set-Xyz789.zip');
  expect(put.mock.calls[0][2]).toMatchObject({ access: 'private', token: 'vercel_blob_client_test', contentType: 'application/zip' });
  const [, tokenInit] = fetchImpl.mock.calls.find(([url]) => url === '/api/adapter-requests/upload')!;
  expect(JSON.parse(String(tokenInit!.body))).toMatchObject({ type: 'blob.generate-client-token', payload: { pathname: 'adapter-requests/u1/data-set.zip' } });
  const [, init] = fetchImpl.mock.calls.find(([url, i]) => url === '/api/adapter-requests' && i?.method === 'POST')!;
  const body = JSON.parse(String(init!.body));
  expect(body).toMatchObject({ source: 'data_set', examples: [], dataSet: { pathname: 'adapter-requests/u1/data-set-Xyz789.zip', rows: 330,
    counts: [{ key: 'billing', count: 110 }, { key: 'tech', count: 110 }, { key: 'none', count: 110 }] } });
  expect(await screen.findByRole('heading', { level: 1, name: 'Request sent' })).toBeTruthy();
});

it('refuses a data set that breaks a rule, naming the file and row, and keeps Send off', async () => {
  hub();
  show();
  await screen.findByLabelText('The question, in one sentence');
  await act(async () => { await fillTheRest(); });
  fireEvent.click(screen.getByLabelText('I have a data set'));
  await act(async () => { fireEvent.change(screen.getByLabelText('Choose the zip of your data set'), { target: { files: [aZip(299)] } }); });
  await waitFor(() => expect(screen.getByText('The data set has 299 rows; it needs at least 300.')).toBeTruthy());
  expect((screen.getByRole('button', { name: 'Send request' }) as HTMLButtonElement).disabled).toBe(true);
  expect(put).not.toHaveBeenCalled();
});

it('asks for the check again when sections 2 to 5 change after it', async () => {
  hub();
  show();
  await screen.findByLabelText('The question, in one sentence');
  await act(async () => { await fillTheRest(); });
  fireEvent.click(screen.getByLabelText('I have a data set'));
  await act(async () => { fireEvent.change(screen.getByLabelText('Choose the zip of your data set'), { target: { files: [aZip()] } }); });
  await waitFor(() => expect(screen.getByLabelText('Rows per option')).toBeTruthy());
  type('Maximum length (characters)', '3000');
  expect(screen.getByText('Sections 2 to 5 changed since emails.zip was checked; check it again.')).toBeTruthy();
  expect((screen.getByRole('button', { name: 'Send request' }) as HTMLButtonElement).disabled).toBe(true);
});
