/**
 * Call a Jeff server (jeff-serve): a situation and questions in, a probability per option out.
 *
 * Conventions that matter:
 * - Option keys are never bare numbers ("1", "2"): JavaScript puts number-like keys ahead of all others, so the
 *   options would silently be sent in a different order. Options given as an array get the keys o1, o2, ...
 * - Advance preparation: a server can prepare the unchanging start of a request once and reuse it. Give the state as
 *   an object whose fields that stay the same come first and whose one changing field (for example a voice
 *   transcript) comes last, and list the options that never change first, word for word, before the ones that do.
 * - Nothing is retried and nothing is guessed: every failure throws a JeffError subclass that says what went wrong.
 */

export type Json = string | number | boolean | null | Json[] | { [key: string]: Json };
/** A state, instructions or option description: plain text, or JSON the model reads as data. */
export type Content = string | { [key: string]: Json } | Json[];
/** 2: answer each question twice, the second time with its options reversed, and average (twice the cost). */
export type Orders = 1 | 2;
/** Key to description (null: the key says it all), or a list of descriptions (keyed o1, o2, ...). */
export type Options = Readonly<Record<string, Content | null>> | readonly Content[];

// The wire format, as jeff-serve reads and writes it.

export interface ChoiceWireQuestion { type: 'choice'; instructions?: Content; criteria: Record<string, Content | null> }
export interface NoulWireQuestion { type: 'noul'; instructions?: Content; criteria?: { true: Content | null; false: Content | null } }
export interface ScoreWireQuestion { type: 'score'; instructions?: Content; criteria: Content[] }
export type WireQuestion = ChoiceWireQuestion | NoulWireQuestion | ScoreWireQuestion;

export interface DecisionRequest {
  /** The client's model when left out. With several adapters on one server, the model name chooses the adapter. */
  model?: string;
  state: Content;
  questions: Record<string, WireQuestion>;
  /** Base64 PNG, JPEG or WebP data URLs, at most four. */
  images?: string[];
  /** The client's orders when left out (the server answers once when neither is set). */
  orders?: Orders;
}

export interface ChoiceAnswer { type: 'choice'; choice: string; probabilities: Record<string, number>; confidence: number }
export interface NoulAnswer { type: 'noul'; noul: number }
export interface ScoreAnswer {
  type: 'score'; score: number; legend: Record<string, Content | null>; probabilities: Record<string, number>; confidence: number;
}
export type WireAnswer = ChoiceAnswer | NoulAnswer | ScoreAnswer;

export interface DecisionResponse {
  model?: string;
  answers: Record<string, WireAnswer>;
  usage: { input_tokens?: number; output_tokens?: number; orders?: number };
}

export interface Health {
  status: 'ready' | 'loading';
  model: string;
  checkpoint: string;
  /** The most options one question may have. */
  max_options: number;
  authentication: boolean;
  modalities: string[];
}

export interface ModelInfo { name: string; description: string; release_date: string }

// Errors. Every failure is one of these; none is retried.

export interface ErrorDetails { status: number | null; detail: unknown; requestId: string | null }

/** A Jeff request failed. `status` is the HTTP status (null when no response arrived). */
export class JeffError extends Error {
  readonly status: number | null;
  readonly detail: unknown;
  readonly requestId: string | null;

  constructor(message: string, details: ErrorDetails = { status: null, detail: null, requestId: null }, options?: { cause?: unknown }) {
    super(message, options);
    this.name = new.target.name;
    this.status = details.status;
    this.detail = details.detail;
    this.requestId = details.requestId;
  }
}
/** The server could not be reached, or did not answer in time. */
export class ConnectionFailed extends JeffError {}
/** 401: the server has JEFF_API_KEY set and the client sent no key or the wrong one. */
export class Unauthorised extends JeffError {}
/** 422: the server refused the request as malformed. `detail` has the server's list of problems. */
export class InvalidRequest extends JeffError {}
/** 422: the server does not serve the model (or adapter) named in the request. */
export class UnknownModel extends InvalidRequest {}
/** 422: a question lists more options than the model handles. Shortlist the options first, or split the question. */
export class TooManyOptions extends InvalidRequest {}
/** 503: the server is still loading the model. */
export class NotReady extends JeffError {}
/**
 * 529: the server is answering another request. `retryAfter` is the server's Retry-After in seconds (null when the
 * server sent none). The client never retries by itself; wait and call again if that suits the application.
 */
export class Busy extends JeffError {
  readonly retryAfter: number | null;

  constructor(message: string, retryAfter: number | null, details: ErrorDetails) {
    super(message, details);
    this.retryAfter = retryAfter;
  }
}
/** Any other error status from the server. */
export class ServerError extends JeffError {}
/** The server answered, but not in the shape this client understands. */
export class ProtocolError extends JeffError {}

// Answers.

export interface Choice {
  /** The chosen option's key (o1, o2, ... when the options were an array). */
  key: string;
  /** Its position among the options as sent. */
  index: number;
  /** Its description as sent. */
  option: Content | null;
  probability: number;
  /** Every option's probability, in the order sent. */
  probabilities: Record<string, number>;
  /** 0 when the answer is no better than a uniform guess, 1 when certain. */
  confidence: number;
  /** [key, probability] pairs, most likely first. */
  ranked: Array<[string, number]>;
}

export interface Score {
  /** The expected level: 0 is the first level, levels.length - 1 the last. */
  score: number;
  /** The most likely level. */
  level: number;
  /** One per level, in the order sent. */
  probabilities: number[];
  confidence: number;
}

// Questions, built by the helpers below and sent together with Client.ask.

/** One question as sent, plus how to read its answer. */
export interface Question<Result> {
  readonly wire: WireQuestion;
  read(answer: WireAnswer, key: string): Result;
}

export type Answers<Q extends Record<string, Question<unknown>>> = { [K in keyof Q]: Q[K] extends Question<infer R> ? R : never };

const NUMBER_LIKE = /^\s*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?\s*$/;

/**
 * Options as [key, description] pairs in the order given. An array gets the keys o1, o2, ...; an object keeps its
 * keys, which must not be bare numbers (JavaScript would already have moved them to the front).
 */
export function optionPairs(options: Options): Array<[string, Content | null]> {
  if (typeof options === 'string') throw new TypeError('options must be an object of key to description or an array of descriptions, not one string');
  let pairs: Array<[string, Content | null]>;
  if (Array.isArray(options)) {
    pairs = (options as readonly Content[]).map((description, index) => [`o${index + 1}`, description]);
  } else {
    pairs = Object.entries(options as Readonly<Record<string, Content | null>>);
    for (const [key] of pairs) {
      if (!key) throw new Error('Option keys must be non-empty strings');
      if (NUMBER_LIKE.test(key)) {
        throw new Error(`Option key "${key}" is a bare number. JavaScript reorders number-like keys, which changes the option order; use keys such as "o1" or a short word.`);
      }
    }
  }
  if (pairs.length === 0) throw new Error('A question needs at least one option');
  return pairs;
}

function num(answer: WireAnswer, field: string, key: string): number {
  const value = (answer as unknown as Record<string, unknown>)[field];
  if (typeof value !== 'number' || !Number.isFinite(value)) throw new ProtocolError(`Question "${key}": the answer has no number "${field}": ${JSON.stringify(answer)}`);
  return value;
}

function expect<T extends WireAnswer['type']>(answer: WireAnswer, type: T, key: string): Extract<WireAnswer, { type: T }> {
  if (answer?.type !== type) throw new ProtocolError(`Question "${key}" was a ${type} question but the answer is ${JSON.stringify(answer)}`);
  return answer as Extract<WireAnswer, { type: T }>;
}

/** The answer's probabilities under the keys sent, in the order sent. */
function inOrder(answer: ChoiceAnswer | ScoreAnswer, keys: string[], key: string): Record<string, number> {
  const given = answer.probabilities;
  const matches = given !== null && typeof given === 'object' && Object.keys(given).length === keys.length
    && keys.every((k) => typeof given[k] === 'number');
  if (!matches) throw new ProtocolError(`Question "${key}": expected a probability for each of ${keys.join(', ')}, got ${JSON.stringify(given)}`);
  return Object.fromEntries(keys.map((k) => [k, given[k]!]));
}

/** Pick one of the options. */
export function choiceQuestion(options: Options, instructions?: Content): Question<Choice> {
  const pairs = optionPairs(options);
  const keys = pairs.map(([key]) => key);
  const wire: ChoiceWireQuestion = { type: 'choice', criteria: Object.fromEntries(pairs) };
  if (instructions !== undefined) wire.instructions = instructions;
  return {
    wire,
    read(answer, key) {
      const choice = expect(answer, 'choice', key);
      const probabilities = inOrder(choice, keys, key);
      const index = keys.indexOf(choice.choice);
      if (index < 0) throw new ProtocolError(`Question "${key}": the server chose "${choice.choice}", which was not one of the options sent`);
      return {
        key: choice.choice, index, option: pairs[index]![1], probability: probabilities[choice.choice]!, probabilities,
        confidence: num(choice, 'confidence', key), ranked: Object.entries(probabilities).sort((a, b) => b[1] - a[1]),
      };
    },
  };
}

/** A yes/no question, answered as the probability of yes. `yes` and `no` optionally say what each answer means. */
export function yesNoQuestion(instructions: Content, meaning: { yes?: Content; no?: Content } = {}): Question<number> {
  const wire: NoulWireQuestion = { type: 'noul', instructions };
  if (meaning.yes !== undefined || meaning.no !== undefined) wire.criteria = { true: meaning.yes ?? null, false: meaning.no ?? null };
  return { wire, read: (answer, key) => num(expect(answer, 'noul', key), 'noul', key) };
}

/** A point on a scale of 2 to 10 levels, lowest first. The score runs from 0 (the first level) to levels.length - 1. */
export function scoreQuestion(levels: readonly Content[], instructions?: Content): Question<Score> {
  if (!Array.isArray(levels) || levels.length < 2 || levels.length > 10) throw new Error('A score question needs a list of 2 to 10 levels, lowest first');
  const keys = levels.map((_, index) => String(index));
  const wire: ScoreWireQuestion = { type: 'score', criteria: [...levels] };
  if (instructions !== undefined) wire.instructions = instructions;
  return {
    wire,
    read(answer, key) {
      const score = expect(answer, 'score', key);
      const probabilities = Object.values(inOrder(score, keys, key));
      const level = probabilities.reduce((best, value, index) => (value > probabilities[best]! ? index : best), 0);
      return { score: num(score, 'score', key), level, probabilities, confidence: num(score, 'confidence', key) };
    },
  };
}

// The client.

export interface ClientOptions {
  /** The server, for example http://localhost:8765. */
  url: string;
  /** The model every request uses unless a call names another (for example jeff-latest, or an application's adapter). */
  model: string;
  /** The server's JEFF_API_KEY, if it has one. */
  apiKey?: string;
  /** 2 answers every question twice (options reversed the second time) and averages; left out, the server answers once. */
  orders?: Orders;
  /** How long to wait for each answer. 30 000 ms when left out. */
  timeoutMs?: number;
}

export interface CallOptions {
  model?: string;
  orders?: Orders;
  images?: string[];
  signal?: AbortSignal;
}

function checkOrders(orders: unknown): void {
  if (orders !== undefined && orders !== 1 && orders !== 2) throw new Error(`orders must be 1 (the options as given) or 2 (also reversed, then averaged); got ${String(orders)}`);
}

function errorFor(status: number, text: string, headers: Headers): JeffError {
  let detail: unknown = text;
  try {
    const parsed: unknown = JSON.parse(text);
    if (parsed !== null && typeof parsed === 'object' && 'detail' in parsed) detail = parsed.detail;
  } catch {
    // Not JSON: the raw text is the detail.
  }
  const details: ErrorDetails = { status, detail, requestId: headers.get('x-request-id') };
  const message = `Jeff answered ${status}: ${(typeof detail === 'string' ? detail : JSON.stringify(detail)).slice(0, 500)}`;
  if (status === 401) return new Unauthorised(`${message} (pass the server's JEFF_API_KEY as apiKey)`, details);
  if (status === 422) {
    const problems = Array.isArray(detail) ? detail.filter((p): p is { loc?: unknown; type?: unknown } => p !== null && typeof p === 'object') : [];
    const last = (p: { loc?: unknown }) => (Array.isArray(p.loc) ? p.loc[p.loc.length - 1] : undefined);
    if ((typeof detail === 'string' && detail.includes('options, but this model handles at most'))
      || problems.some((p) => p.type === 'too_long' && last(p) === 'criteria')) return new TooManyOptions(message, details);
    if (problems.some((p) => last(p) === 'model')) return new UnknownModel(message, details);
    return new InvalidRequest(message, details);
  }
  if (status === 503) return new NotReady(message, details);
  if (status === 529) {
    const retry = headers.get('retry-after');
    if (retry !== null && !NUMBER_LIKE.test(retry)) throw new ProtocolError(`Jeff answered 529 with a Retry-After that is not a number of seconds: "${retry}"`, details);
    return new Busy(message, retry === null ? null : Number(retry), details);
  }
  return new ServerError(message, details);
}

export class Client {
  readonly url: string;
  readonly model: string;
  readonly orders: Orders | undefined;
  private readonly apiKey: string | undefined;
  private readonly timeoutMs: number;

  constructor(options: ClientOptions) {
    if (!/^https?:\/\//.test(options.url)) throw new Error(`The server URL must start with http:// or https://; got "${options.url}"`);
    if (!options.model) throw new Error('model must name the model to use, for example "jeff-latest"');
    checkOrders(options.orders);
    const timeoutMs = options.timeoutMs ?? 30_000;
    if (!(timeoutMs > 0)) throw new Error(`timeoutMs must be positive; got ${timeoutMs}`);
    this.url = options.url.replace(/\/+$/, '');
    this.model = options.model;
    this.orders = options.orders;
    this.apiKey = options.apiKey;
    this.timeoutMs = timeoutMs;
  }

  /** The same client, with another default model (for example one application's adapter). */
  withModel(model: string): Client {
    return new Client({
      url: this.url, model, timeoutMs: this.timeoutMs,
      ...(this.apiKey !== undefined && { apiKey: this.apiKey }), ...(this.orders !== undefined && { orders: this.orders }),
    });
  }

  /** Send one request as it is and return the server's response. A request without model or orders gets the client's. */
  async decide(request: DecisionRequest, signal?: AbortSignal): Promise<DecisionResponse> {
    checkOrders(request.orders);
    const body: DecisionRequest = { ...request, model: request.model ?? this.model };
    const orders = request.orders ?? this.orders;
    if (orders !== undefined) body.orders = orders;
    const value = await this.send('POST', '/v1/systemone', body, signal);
    if (value === null || typeof value !== 'object' || typeof (value as { answers?: unknown }).answers !== 'object' || (value as { answers?: unknown }).answers === null) {
      throw new ProtocolError(`Expected a JSON object with answers, got ${JSON.stringify(value).slice(0, 300)}`);
    }
    return value as DecisionResponse;
  }

  /**
   * Several independent questions about one state in one request, answered together:
   * `const { route, angry } = await jeff.ask(state, { route: choiceQuestion(...), angry: yesNoQuestion(...) })`.
   */
  async ask<Q extends Record<string, Question<unknown>>>(state: Content, questions: Q, call: CallOptions = {}): Promise<Answers<Q>> {
    const entries = Object.entries(questions);
    if (entries.length === 0) throw new Error('Ask at least one question');
    const request: DecisionRequest = { state, questions: Object.fromEntries(entries.map(([key, q]) => [key, q.wire])) };
    if (call.model !== undefined) request.model = call.model;
    if (call.orders !== undefined) request.orders = call.orders;
    if (call.images !== undefined && call.images.length > 0) request.images = call.images;
    const response = await this.decide(request, call.signal);
    const missing = entries.map(([key]) => key).filter((key) => !(key in response.answers));
    if (missing.length > 0) throw new ProtocolError(`The response has no answer for ${missing.join(', ')}`);
    return Object.fromEntries(entries.map(([key, q]) => [key, q.read(response.answers[key]!, key)])) as Answers<Q>;
  }

  /** Which option fits the state best. */
  async choose(state: Content, options: Options, instructions?: Content, call: CallOptions = {}): Promise<Choice> {
    return (await this.ask(state, { choice: choiceQuestion(options, instructions) }, call)).choice;
  }

  /** The probability that the answer to the yes/no question is yes. */
  async yesNo(state: Content, instructions: Content, call: CallOptions & { yes?: Content; no?: Content } = {}): Promise<number> {
    const meaning: { yes?: Content; no?: Content } = {};
    if (call.yes !== undefined) meaning.yes = call.yes;
    if (call.no !== undefined) meaning.no = call.no;
    return (await this.ask(state, { noul: yesNoQuestion(instructions, meaning) }, call)).noul;
  }

  /** Where the state sits on a scale of 2 to 10 levels, lowest first. */
  async score(state: Content, levels: readonly Content[], instructions?: Content, call: CallOptions = {}): Promise<Score> {
    return (await this.ask(state, { score: scoreQuestion(levels, instructions) }, call)).score;
  }

  /**
   * Send a request ahead of time so the server can prepare its unchanging start; the answer is discarded.
   * Send the state with its changing last field empty (or as it stands now) and the questions with only the options
   * that never change. A later request that starts the same way, word for word, can then be answered faster by a
   * server that reuses prepared work; a server that does not simply answers.
   */
  async prepare(state: Content, questions: Record<string, Question<unknown>>, call: Omit<CallOptions, 'images'> = {}): Promise<void> {
    if (state === null || typeof state !== 'object' || Array.isArray(state) || Object.keys(state).length === 0) {
      throw new Error('prepare needs the state as an object whose last field is the one that changes (sent empty or as it stands now); a server can only reuse the fields before it');
    }
    await this.ask(state, questions, call);
  }

  /** The server's state: "ready" or "loading", the model name, the most options it handles and more. */
  async health(signal?: AbortSignal): Promise<Health> {
    const value = await this.send('GET', '/health', undefined, signal);
    const fields = ['status', 'model', 'checkpoint', 'max_options', 'authentication', 'modalities'];
    if (value === null || typeof value !== 'object' || !fields.every((f) => f in value)) {
      throw new ProtocolError(`Expected a health report with ${fields.join(', ')}, got ${JSON.stringify(value).slice(0, 300)}`);
    }
    return value as Health;
  }

  /** The model names this server answers to. */
  async models(signal?: AbortSignal): Promise<ModelInfo[]> {
    const value = await this.send('GET', '/v1/models', undefined, signal);
    const models = value !== null && typeof value === 'object' ? (value as { models?: unknown }).models : undefined;
    if (!Array.isArray(models) || !models.every((m) => m !== null && typeof m === 'object' && typeof m.name === 'string')) {
      throw new ProtocolError(`Expected a list of models, got ${JSON.stringify(value).slice(0, 300)}`);
    }
    return models as ModelInfo[];
  }

  private async send(method: 'GET' | 'POST', path: string, body: unknown, signal: AbortSignal | undefined): Promise<unknown> {
    const headers: Record<string, string> = { accept: 'application/json' };
    if (body !== undefined) headers['content-type'] = 'application/json';
    if (this.apiKey !== undefined) headers.authorization = `Bearer ${this.apiKey}`;
    const timeout = AbortSignal.timeout(this.timeoutMs);
    let response: Response;
    let text: string;
    try {
      response = await fetch(this.url + path, {
        method, headers, signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
        ...(body !== undefined && { body: JSON.stringify(body) }),
      });
      text = await response.text();
    } catch (error) {
      if (signal?.aborted) throw error;  // the caller cancelled: their own reason, unchanged
      const reason = timeout.aborted ? `no answer within ${this.timeoutMs} ms` : String(error);
      throw new ConnectionFailed(`Could not reach Jeff at ${this.url}: ${reason}`, undefined, { cause: error });
    }
    if (!response.ok) throw errorFor(response.status, text, response.headers);
    try {
      return JSON.parse(text);
    } catch (error) {
      throw new ProtocolError(`Jeff answered with something that is not JSON: ${text.slice(0, 300)}`, { status: response.status, detail: text, requestId: response.headers.get('x-request-id') }, { cause: error });
    }
  }
}
