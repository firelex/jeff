import assert from 'node:assert/strict';
import { createServer, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import { after, before, beforeEach, describe, it } from 'node:test';
import {
  Busy, Client, ConnectionFailed, InvalidRequest, NotReady, ProtocolError, ServerError, TooManyOptions, Unauthorised,
  UnknownModel, choiceQuestion, scoreQuestion, yesNoQuestion, type JeffError,
} from '../src/index.js';

interface Recorded { method: string; path: string; body: any; authorization: string | undefined; raw: string }
interface Reply { status: number; headers?: Record<string, string>; body: unknown }

/** A scripted server: each request is recorded and answered with the next scripted reply. */
let server: Server;
let url: string;
let requests: Recorded[] = [];
let replies: Reply[] = [];

before(async () => {
  server = createServer((req, res) => {
    let raw = '';
    req.on('data', (chunk) => { raw += chunk; });
    req.on('end', () => {
      requests.push({ method: req.method!, path: req.url!, body: raw ? JSON.parse(raw) : null, authorization: req.headers.authorization, raw });
      const reply = replies.shift();
      if (!reply) throw new Error('No reply scripted');
      res.writeHead(reply.status, { 'content-type': 'application/json', ...reply.headers });
      res.end(typeof reply.body === 'string' ? reply.body : JSON.stringify(reply.body));
    });
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});
after(() => new Promise<void>((resolve) => server.close(() => resolve())));
beforeEach(() => { requests = []; replies = []; });

const answer = (answers: Record<string, unknown>) => replies.push({ status: 200, body: { model: 'jeff-qwen3.5-0.8b', answers, usage: { input_tokens: 10, output_tokens: 0 } } });
const fail = (status: number, detail: unknown, headers: Record<string, string> = {}) => replies.push({ status, headers, body: { detail } });
const choiceAnswer = (probabilities: Record<string, number>) => ({
  type: 'choice', choice: Object.entries(probabilities).sort((a, b) => b[1] - a[1])[0]![0], probabilities, confidence: 0.5,
});

describe('questions', () => {
  it('chooses from an object, sending the options in order', async () => {
    answer({ choice: choiceAnswer({ refunds: 0.2, parcels: 0.7, login: 0.1 }) });
    const jeff = new Client({ url, model: 'jeff-latest' });
    const picked = await jeff.choose('My parcel arrived crushed.', { refunds: 'Refunds', parcels: 'Damaged or lost parcels', login: null }, 'Which team?');
    assert.deepEqual([picked.key, picked.index, picked.option, picked.probability], ['parcels', 1, 'Damaged or lost parcels', 0.7]);
    assert.deepEqual(picked.ranked[0], ['parcels', 0.7]);
    const [sent] = requests;
    assert.equal(sent!.path, '/v1/systemone');
    assert.equal(sent!.authorization, undefined);
    assert.deepEqual(sent!.body, {
      state: 'My parcel arrived crushed.', model: 'jeff-latest',
      questions: { choice: { type: 'choice', criteria: { refunds: 'Refunds', parcels: 'Damaged or lost parcels', login: null }, instructions: 'Which team?' } },
    });
    assert.ok(sent!.raw.indexOf('"refunds"') < sent!.raw.indexOf('"parcels"') && sent!.raw.indexOf('"parcels"') < sent!.raw.indexOf('"login"'));
  });

  it('chooses from an array, with keys that are not numbers', async () => {
    answer({ choice: choiceAnswer({ o1: 0.1, o2: 0.1, o3: 0.8 }) });
    const picked = await new Client({ url, model: 'jeff' }).choose('Turn the lights off', ['Play music', 'Set a timer', 'Lights off']);
    assert.deepEqual([picked.key, picked.index, picked.option], ['o3', 2, 'Lights off']);
    assert.deepEqual(requests[0]!.body.questions.choice, { type: 'choice', criteria: { o1: 'Play music', o2: 'Set a timer', o3: 'Lights off' } });
  });

  it('refuses number-like option keys and other bad questions before sending', () => {
    for (const key of ['1', '0', '12', ' 3', '2.5', '-1', '1e3']) {
      assert.throws(() => choiceQuestion({ [key]: 'x', other: 'y' }), /bare number/);
    }
    assert.throws(() => choiceQuestion([]), /at least one option/);
    assert.throws(() => scoreQuestion(['only']), /2 to 10 levels/);
    assert.throws(() => new Client({ url, model: 'jeff', orders: 3 as 1 }), /orders/);
    assert.throws(() => new Client({ url: 'localhost:8765', model: 'jeff' }), /http/);
    assert.throws(() => new Client({ url, model: '' }), /model/);
  });

  it('asks yes/no, score and several questions in one request', async () => {
    const jeff = new Client({ url, model: 'jeff-latest', apiKey: 'secret', orders: 2 });
    answer({ noul: { type: 'noul', noul: 0.83 } });
    assert.equal(await jeff.yesNo('I have asked three times now!', 'Is the customer angry?', { yes: 'Angry', no: 'Calm' }), 0.83);
    assert.deepEqual(requests.at(-1)!.body.questions.noul, { type: 'noul', instructions: 'Is the customer angry?', criteria: { true: 'Angry', false: 'Calm' } });
    assert.equal(requests.at(-1)!.body.orders, 2);
    assert.equal(requests.at(-1)!.authorization, 'Bearer secret');

    answer({ score: { type: 'score', score: 1.6, probabilities: { 0: 0.1, 1: 0.2, 2: 0.7 }, legend: { 0: 'low', 1: 'medium', 2: 'high' }, confidence: 0.4 } });
    const rated = await jeff.score('The server is down for everyone.', ['low', 'medium', 'high'], 'How urgent?', { orders: 1 });
    assert.deepEqual([rated.score, rated.level, rated.probabilities], [1.6, 2, [0.1, 0.2, 0.7]]);
    assert.equal(requests.at(-1)!.body.orders, 1);

    answer({ route: choiceAnswer({ billing: 0.9, tech: 0.1 }), angry: { type: 'noul', noul: 0.2 } });
    const { route, angry } = await jeff.ask('Why was I charged twice?', {
      route: choiceQuestion({ billing: 'Billing', tech: 'Tech' }), angry: yesNoQuestion('Is the customer angry?'),
    }, { model: 'jeff-support' });
    assert.equal(route.key, 'billing');
    assert.equal(angry, 0.2);
    assert.equal(requests.at(-1)!.body.model, 'jeff-support');
  });

  it('uses the client model unless the request names one', async () => {
    const nav = new Client({ url, model: 'jeff-latest' }).withModel('jeff-nav');
    answer({ q: { type: 'noul', noul: 0.5 } });
    answer({ q: { type: 'noul', noul: 0.5 } });
    await nav.decide({ state: 's', questions: { q: { type: 'noul' } } });
    await nav.decide({ model: 'jeff-guard', state: 's', questions: { q: { type: 'noul' } } });
    assert.deepEqual(requests.map((r) => r.body.model), ['jeff-nav', 'jeff-guard']);
    assert.equal('orders' in requests[0]!.body, false);
  });

  it('prepares with the fixed part and needs the changing field last', async () => {
    const jeff = new Client({ url, model: 'jeff-latest' });
    answer({ nav: choiceAnswer({ ask_question: 0.5, none_of_these: 0.5 }) });
    await jeff.prepare({ current_screen: 'Inbox', transcript: '' }, { nav: choiceQuestion({ ask_question: 'A question', none_of_these: 'None' }) });
    assert.deepEqual(Object.keys(requests[0]!.body.state), ['current_screen', 'transcript']);
    await assert.rejects(jeff.prepare('just text', { nav: choiceQuestion(['a', 'b']) }), /last field/);
  });
});

describe('errors', () => {
  const cases: Array<[number, unknown, new (...args: any[]) => JeffError]> = [
    [401, 'Missing or invalid API key.', Unauthorised],
    [422, "Question 'choice' has 30 options, but this model handles at most 26. Shortlist the options first.", TooManyOptions],
    [422, [{ loc: ['body', 'questions', 'choice', 'choice', 'criteria'], msg: 'too long', type: 'too_long' }], TooManyOptions],
    [422, [{ loc: ['body', 'model'], msg: 'Value error, Unknown model.', type: 'value_error' }], UnknownModel],
    [422, [{ loc: ['body', 'state'], msg: 'Field required', type: 'missing' }], InvalidRequest],
    [503, 'The model is not ready.', NotReady],
    [500, 'Internal Server Error', ServerError],
  ];
  for (const [status, detail, type] of cases) {
    it(`${status} ${JSON.stringify(detail).slice(0, 40)} becomes ${type.name}`, async () => {
      fail(status, detail, { 'x-request-id': 'abc' });
      const error = await new Client({ url, model: 'jeff' }).choose('s', ['a', 'b']).then(() => null, (e: unknown) => e);
      assert.ok(error instanceof type, `got ${String(error)}`);
      assert.equal(error.name, type.name);
      assert.deepEqual([error.status, error.detail, error.requestId], [status, detail, 'abc']);
      assert.equal(requests.length, 1);
    });
  }

  it('reports busy with Retry-After and does not retry', async () => {
    fail(529, 'The model is busy. Retry shortly.', { 'retry-after': '1' });
    const error = await new Client({ url, model: 'jeff' }).yesNo('s', 'Is it?').then(() => null, (e: unknown) => e);
    assert.ok(error instanceof Busy);
    assert.equal(error.retryAfter, 1);
    assert.equal(requests.length, 1);
  });

  it('reports malformed answers as protocol errors', async () => {
    const jeff = new Client({ url, model: 'jeff' });
    answer({});
    await assert.rejects(jeff.choose('s', ['a', 'b']), (e) => e instanceof ProtocolError && /no answer/.test(e.message));
    answer({ choice: choiceAnswer({ o1: 0.5, o9: 0.5 }) });
    await assert.rejects(jeff.choose('s', ['a', 'b']), (e) => e instanceof ProtocolError && /expected a probability for each/.test(e.message));
    replies.push({ status: 200, body: 'not json' });
    await assert.rejects(jeff.choose('s', ['a', 'b']), (e) => e instanceof ProtocolError && /not JSON/.test(e.message));
  });

  it('reports an unreachable server as a connection failure', async () => {
    const closed = createServer();
    await new Promise<void>((resolve) => closed.listen(0, '127.0.0.1', resolve));
    const port = (closed.address() as AddressInfo).port;
    await new Promise<void>((resolve) => closed.close(() => resolve()));
    await assert.rejects(new Client({ url: `http://127.0.0.1:${port}`, model: 'jeff' }).health(), (e) => e instanceof ConnectionFailed && /Could not reach/.test(e.message));
  });
});

describe('server information', () => {
  it('reads health and models', async () => {
    const health = { status: 'ready', model: 'jeff-qwen3.5-0.8b', checkpoint: 'c', max_options: 26, authentication: false, modalities: ['text'] };
    replies.push({ status: 200, body: health });
    replies.push({ status: 200, body: { models: [{ name: 'jeff', description: 'd', release_date: '2026-09-28' }] } });
    const jeff = new Client({ url: `${url}/`, model: 'jeff' });
    assert.deepEqual(await jeff.health(), health);
    assert.deepEqual((await jeff.models()).map((m) => m.name), ['jeff']);
    assert.deepEqual(requests.map((r) => [r.method, r.path]), [['GET', '/health'], ['GET', '/v1/models']]);
  });
});
