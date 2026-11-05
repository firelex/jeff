import { createServer, type IncomingMessage } from "node:http";
import type { AddressInfo } from "node:net";
import { expect, test } from "vitest";
import { JeffError } from "../../src/errors.js";
import type { Question } from "../../src/escalation.js";
import { RemoteJeff, RestTransport } from "../../src/remote.js";
import { catalogueFor, fixture, normalised, wireOf } from "./fixtures.js";
import { runOp } from "./run-decisions.js";
import { withoutProxies } from "../helpers/no-proxies.js";

withoutProxies();

interface Exchange {
  request: { method: string; path: string; authorization: string | null; body: unknown };
  response: { status: number; headers: Record<string, string>; body: string };
}
interface RemoteCase {
  name: string; catalogue: string; token: string | null; actions: Array<{ op: string; args: any }>;
  connect: unknown; exchanges: Exchange[]; questions: unknown[]; results: unknown[];
}

function bodyOf(request: IncomingMessage): Promise<string> {
  return new Promise((resolve) => {
    let text = "";
    request.on("data", (chunk) => { text += chunk; });
    request.on("end", () => resolve(text));
  });
}

/** Serves the recorded answers in order, each only after the request it answers is the recorded one (method, path,
 * Authorization, JSON body); a request that differs is answered 599 and kept, so the test reports it. */
async function recordedServer(exchanges: Exchange[]) {
  const sent: Exchange["request"][] = [];
  let next = 0;
  const server = createServer(async (request, response) => {
    const text = await bodyOf(request);
    const got = { method: request.method as string, path: (request.url as string).split("?")[0] as string,
      authorization: request.headers.authorization ?? null, body: text ? JSON.parse(text) : null };
    sent.push(got);
    const answer = exchanges[next];
    next += 1;
    if (answer === undefined) {
      response.writeHead(599).end("no recorded answer left");
      return;
    }
    if (JSON.stringify(normalisedOrder(got)) !== JSON.stringify(normalisedOrder(answer.request))) {
      response.writeHead(599).end("this request is not the recorded one");
      return;
    }
    response.writeHead(answer.response.status, answer.response.headers).end(answer.response.body);
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  return { url, sent, close: () => new Promise<void>((resolve) => { server.closeAllConnections(); server.close(() => resolve()); }) };
}

/** The value with every object's keys sorted: JSON bodies are compared by content, not key order. */
function normalisedOrder(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(normalisedOrder);
  if (value === null || typeof value !== "object") return value;
  return Object.fromEntries(Object.keys(value).sort().map((key) => [key, normalisedOrder((value as any)[key])]));
}

const withId = (error: unknown) => normalised({ ...wireOf(error), request_id: (error as JeffError).requestId });

const cases = fixture<{ cases: RemoteCase[] }>("remote.json").cases;

test.each(cases.map((item) => [item.name, item] as const))("%s", async (_name, item) => {
  const server = await recordedServer(item.exchanges);
  const questions: unknown[] = [];
  const asked: Question[] = [];
  const results: unknown[] = [];
  let connect: unknown = { ok: true };
  let client: RemoteJeff | null = null;
  try {
    const transport = new RestTransport(server.url, item.token, 10000);
    client = await RemoteJeff.connect(transport, catalogueFor(item.catalogue), transport.url);
  } catch (error) {
    connect = { error: withId(error) };
  }
  if (client !== null) {
    for (const action of item.actions) {
      try {
        results.push({ result: normalised(await runOp(client, action.op, action.args, questions, asked)) });
      } catch (error) {
        results.push({ error: withId(error) });
      }
    }
    await client.close();
  }
  await server.close();
  expect(server.sent).toEqual(item.exchanges.map((exchange) => exchange.request));
  expect(connect).toEqual(item.connect);
  expect(results).toEqual(item.results);
  expect(questions).toEqual(item.questions);
});
