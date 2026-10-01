# @jeff/client

Call a Jeff server (`jeff-serve`) from TypeScript or JavaScript. You describe a situation (the state) and ask one or
more questions; Jeff returns a probability for each option. No runtime dependencies: it uses `fetch`, so it runs in
Node 22 or later, Deno, Bun and browsers. ESM only.

This package is not published. Build it from this folder (`npm install && npm run build`) and depend on it by path,
for example `"@jeff/client": "file:../jev/clients/typescript"`.

## Examples

A support-ticket router:

```ts
import { Client, choiceQuestion, yesNoQuestion } from '@jeff/client';

const jeff = new Client({ url: 'http://localhost:8765', model: 'jeff-latest' });

const ticket = 'The parcel arrived crushed and I want my money back.';
const { team, angry } = await jeff.ask(ticket, {
  team: choiceQuestion({
    refunds: 'Refunds and payments',
    parcels: 'Damaged or lost parcels',
    login: 'Account and login problems',
  }, 'Which team should handle this ticket?'),
  angry: yesNoQuestion('Is the customer angry?'),
});
console.log(team.key, team.probability, angry);  // "parcels" 0.91 0.64
```

A voice command, with the screen prepared in advance while the user is still speaking:

```ts
import { Client, choiceQuestion } from '@jeff/client';

const jeff = new Client({ url: 'http://localhost:8765', model: 'jeff-latest' });
const fixed = { ask_question: 'The user is asking a question', none_of_these: 'None of these' };
const screen = { current_screen: 'Inbox', previous_screen: 'All deals' };

// The screen opened: let the server prepare everything that stays the same until the user speaks.
await jeff.prepare({ ...screen, transcript: '' }, { command: choiceQuestion(fixed, 'What does the user want?') });

// The user spoke.
const picked = await jeff.choose(
  { ...screen, transcript: 'open the email from Sarah' },
  { ...fixed, o1: 'Open the latest email', o2: 'Archive this thread', o3: 'Go back' },
  'What does the user want?',
);
if (picked.probability < 0.5) console.log('Not sure; ask the user which they meant:', picked.ranked.slice(0, 3));
```

## API

`new Client({ url, model, apiKey?, orders?, timeoutMs? })`

- `model`: the model every request uses unless a call names another. When one server holds several application
  adapters (nav, tools, guard and so on), the model name chooses the adapter; `client.withModel('...')` gives a
  client bound to another one.
- `apiKey`: the server's `JEFF_API_KEY`, sent as a bearer token. Leave it out when the server has none.
- `orders: 2`: answer every question twice, the second time with its options reversed, and average the two. This
  evens out a small model's lean towards options by position, at twice the cost. Left out, the server answers once.
- `timeoutMs`: how long to wait for each answer; 30 000 when left out.

Every call below also takes `{ model?, orders?, images?, signal? }` as its last argument, to override the client's
setting for that one request (`images` are base64 PNG, JPEG or WebP data URLs, at most four).

| Call | Returns |
|---|---|
| `choose(state, options, instructions?)` | `Choice`: `key`, `index`, `option`, `probability`, `probabilities` (every option, in the order sent), `confidence`, `ranked` |
| `yesNo(state, instructions, { yes?, no? })` | the probability of yes, a number |
| `score(state, levels, instructions?)` | `Score`: `score` (0 for the first level up to `levels.length - 1`), `level` (the most likely), `probabilities`, `confidence` |
| `ask(state, { key: question, ... })` | one typed answer per key; build questions with `choiceQuestion`, `yesNoQuestion` and `scoreQuestion` |
| `prepare(state, questions)` | nothing: sends the request ahead of time and drops the answer (see below) |
| `decide(request)` | the raw response for a raw request (`DecisionRequest`, `DecisionResponse`) |
| `health()` | `{ status: 'ready' \| 'loading', model, checkpoint, max_options, authentication, modalities }` |
| `models()` | the model names the server answers to |

`options` is an object of key to description (`null` when the key says it all), or an array of descriptions, which
get the keys `o1`, `o2`, and so on. `state`, instructions and descriptions can be plain text or JSON.

## Conventions

- **Option keys are never bare numbers.** JavaScript moves number-like keys (`"1"`, `"2"`) ahead of all others in
  every object, so `{ special: ..., 1: ..., 2: ... }` would be sent with the numbers first. The client refuses such
  keys; use `o1`, `o2` or short words.
- **Unchanging parts first, the changing field last.** A server can prepare the start of a request once and reuse it.
  Give the state as an object whose unchanging fields come first and whose one changing field (a voice transcript, a
  ticket's text) comes last. List the options that never change first, word for word, before the ones that do.
  `prepare` sends that fixed part ahead of time (the changing field empty), so the real request only adds the rest.
- **Several questions about the same state go in one request** (`ask`): they are answered together.

## Errors

Nothing is retried and nothing is guessed. Every failure throws a subclass of `JeffError`, which carries `status`,
`detail` (the server's explanation) and `requestId`:

| Error | When |
|---|---|
| `Unauthorised` | 401: the server has `JEFF_API_KEY` and the key is missing or wrong |
| `TooManyOptions` | 422: a question has more options than the model handles (see `health().max_options`); shortlist first |
| `UnknownModel` | 422: the server does not serve that model or adapter |
| `InvalidRequest` | 422: any other malformed request |
| `NotReady` | 503: the server is still loading the model |
| `Busy` | 529: the server is answering another request; `retryAfter` is the server's wait in seconds. Retry yourself if that suits you |
| `ServerError` | any other error status |
| `ConnectionFailed` | the server could not be reached or did not answer within `timeoutMs` |
| `ProtocolError` | the server answered in a shape the client does not understand |

## Development

```bash
npm install
npm test        # compiles, then runs the tests against a small fake server
npm run build   # writes dist/
```
