// One request from a page to the hub's function, and the words for what came back, shared by the pages that act
// through it (/account, /request-adapter, /admin/requests).

// Each request's time limit, as /account/return has.
export const REQUEST_TIMEOUT_MS = 10_000;

export type Answer = { kind: 'answer'; status: number; body: unknown } | { kind: 'failed'; reason: string };

// One request to the hub's function: a GET, or a POST with a JSON body or none, within `timeoutMs` (REQUEST_TIMEOUT_MS
// unless a page sends something large and says how long it waits). The
// answer is read to its end and parsed when it says it is JSON. A request that fails, whether sending it or reading
// its answer (no network, the connection dropped, the time limit), or an answer that is not the JSON it claims to be,
// is returned as what it is, for the page to show.
export async function askHub(path: string, post: { body?: unknown } | null, timeoutMs = REQUEST_TIMEOUT_MS): Promise<Answer> {
  const signal = AbortSignal.timeout(timeoutMs);
  const init: RequestInit = post === null
    ? { credentials: 'same-origin', headers: { accept: 'application/json' }, signal }
    : post.body === undefined
      ? { method: 'POST', credentials: 'same-origin', headers: { accept: 'application/json' }, signal }
      : { method: 'POST', credentials: 'same-origin', headers: { accept: 'application/json', 'content-type': 'application/json' },
        body: JSON.stringify(post.body), signal };
  let status: number;
  let type: string | null;
  let text: string;
  try {
    const response = await fetch(path, init);
    status = response.status;
    type = response.headers.get('content-type');
    text = await response.text();
  } catch (error) {
    // AbortSignal.timeout's error is a DOMException named TimeoutError (compared by name: a test DOM's DOMException
    // class is not the browser's). A failed send or a body cut off is a TypeError.
    if (typeof error === 'object' && error !== null && (error as { name?: unknown }).name === 'TimeoutError') {
      return { kind: 'failed', reason: `the hub did not answer within ${timeoutMs / 1000} seconds` };
    }
    if (error instanceof TypeError) return { kind: 'failed', reason: `the request failed: ${error.message}` };
    throw error;
  }
  if (type === null || type.split(';')[0].trim() !== 'application/json') return { kind: 'answer', status, body: text };
  try {
    return { kind: 'answer', status, body: JSON.parse(text) };
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
    return { kind: 'failed', reason: `the hub answered ${status} with JSON that does not parse` };
  }
}

// Some of the hub's messages start in lower case or end without a full stop (they read as a clause in a log); on the
// page each is a sentence.
export function sentence(text: string): string {
  const capital = text.charAt(0).toUpperCase() + text.slice(1);
  return /[.!?]$/.test(capital) ? capital : `${capital}.`;
}

// The words of a refusal: the hub's error shape carries them; anything else is named by its status.
export function refusalOf(answer: Answer): string {
  if (answer.kind === 'failed') return `Something went wrong: ${answer.reason}. Try again.`;
  const error = (answer.body as { error?: { message?: unknown } } | null)?.error;
  if (typeof error === 'object' && error !== null && typeof error.message === 'string') return sentence(error.message);
  return `Something went wrong: the hub answered ${answer.status}. Try again.`;
}

