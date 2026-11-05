/** Proxies from the environment (src/proxy.ts): the HTTP transport chooses its proxy as httpx with trust_env=True
 * does, the channel as websockets with proxy=True does (the expected answers below are what httpx 0.28.1 and
 * websockets 17.3 chose for the same environment and URL); requests then go through a local proxy started here:
 * forwarded for http://, a CONNECT tunnel for https://, ws:// and wss://, and straight to the server for a host in
 * NO_PROXY. */

import { afterEach, beforeAll, expect, test } from "vitest";
import { Jeff } from "../src/client.js";
import { BackendUnavailable, InvalidRequest } from "../src/errors.js";
import type { ChannelJeff } from "../src/channel.js";
import { type Environment, channelProxyFor, environmentProxies, httpProxyFor } from "../src/proxy.js";
import { startChannelServer } from "./helpers/channel-server.js";
import { selfSignedCertificate, startHttpProxy } from "./helpers/proxies.js";

const CTX = { company: "A shop.", channel: "email" };
const wsOf = (url: string) => url.replace(/^http/, "ws");
const channelShown = (url: string, env: Environment) => channelProxyFor(wsOf(url), env)?.shown ?? null;

/** [environment, URL, httpx's proxy, websockets' proxy for the URL as ws:// or wss://]. */
const RULES: Array<[Record<string, string>, string, string | null, string | null]> = [
  [{ HTTP_PROXY: "http://up:1", HTTPS_PROXY: "http://ups:2" }, "http://a.com", "http://up:1", "http://ups:2"],
  [{ HTTP_PROXY: "http://up:1", HTTPS_PROXY: "http://ups:2" }, "https://a.com", "http://ups:2", "http://ups:2"],
  [{ HTTPS_PROXY: "http://UP:2", https_proxy: "http://low:3" }, "https://a.com", "http://low:3", "http://low:3"],
  [{ HTTPS_PROXY: "http://up:2", https_proxy: "" }, "https://a.com", null, null],
  [{ HTTP_PROXY: "http://up:1", REQUEST_METHOD: "GET" }, "http://a.com", null, null],
  [{ http_proxy: "http://low:1", REQUEST_METHOD: "GET" }, "http://a.com", "http://low:1", "http://low:1"],
  [{ ALL_PROXY: "http://all:9" }, "https://a.com", "http://all:9", null],
  [{ ALL_PROXY: "http://all:9", HTTPS_PROXY: "http://ups:2" }, "https://a.com", "http://ups:2", "http://ups:2"],
  [{ HTTP_PROXY: "http://up:1" }, "https://a.com", null, null],
  [{ HTTPS_PROXY: "http://ups:2", NO_PROXY: "a.com" }, "https://a.com", null, null],
  [{ HTTPS_PROXY: "http://ups:2", NO_PROXY: "a.com" }, "https://www.a.com", null, null],
  [{ HTTPS_PROXY: "http://ups:2", NO_PROXY: "a.com" }, "https://wwwa.com", "http://ups:2", "http://ups:2"],
  [{ HTTPS_PROXY: "http://ups:2", NO_PROXY: ".a.com" }, "https://a.com", "http://ups:2", null],
  [{ HTTPS_PROXY: "http://ups:2", NO_PROXY: ".a.com" }, "https://www.a.com", null, null],
  [{ HTTPS_PROXY: "http://ups:2", NO_PROXY: "b.com, *" }, "https://a.com", null, "http://ups:2"],
  [{ HTTPS_PROXY: "http://ups:2", NO_PROXY: "*" }, "https://a.com", null, null],
  [{ HTTPS_PROXY: "http://ups:2", NO_PROXY: "a.com:8443" }, "https://a.com:8443", null, null],
  [{ HTTPS_PROXY: "http://ups:2", NO_PROXY: "a.com:8443" }, "https://a.com", "http://ups:2", "http://ups:2"],
  [{ HTTPS_PROXY: "http://ups:2", NO_PROXY: "127.0.0.1" }, "https://127.0.0.1:9000", null, null],
  [{ HTTPS_PROXY: "http://ups:2", NO_PROXY: "localhost" }, "https://LOCALHOST:9000", null, null],
  [{ HTTPS_PROXY: "http://ups:2", NO_PROXY: "::1" }, "https://[::1]:9000", null, null],
  [{ HTTPS_PROXY: "http://ups:2", NO_PROXY: "http://a.com" }, "https://a.com", "http://ups:2", "http://ups:2"],
  [{ HTTP_PROXY: "http://up:1", NO_PROXY: "http://a.com" }, "http://a.com", null, "http://up:1"],
  [{ HTTPS_PROXY: "http://ups:2", NO_PROXY: "A.COM" }, "https://a.com", null, null],
  [{ HTTP_PROXY: "http://up:1", WS_PROXY: "http://ws:5" }, "http://a.com", "http://up:1", "http://ws:5"],
  [{ HTTP_PROXY: "http://up:1", HTTPS_PROXY: "http://ups:2", WS_PROXY: "http://ws:5" }, "https://a.com", "http://ups:2",
    "http://ups:2"],
  [{ HTTPS_PROXY: "http://ups:2", WSS_PROXY: "http://wss:6" }, "https://a.com", "http://ups:2", "http://wss:6"],
];

test.each(RULES)("with %j, %s goes through %s over HTTP and %s over the channel", (env, url, http, channel) => {
  expect(httpProxyFor(url, env)).toBe(http);
  expect(channelShown(url, env)).toBe(channel);
});

test("a proxy without a scheme gets http:// over HTTP; the channel refuses it, as websockets does", () => {
  const env = { HTTPS_PROXY: "ups:2" };
  expect(httpProxyFor("https://a.com", env)).toBe("http://ups:2");
  expect(() => channelProxyFor("wss://a.com", env)).toThrow(new InvalidRequest(
    "The proxy HTTPS_PROXY=ups:2 cannot be used for the channel: scheme \"ups\" isn't supported"));
});

test("SOCKS proxies and other schemes are refused with InvalidRequest naming the variable, never their password", () => {
  expect(() => httpProxyFor("http://a.com", { ALL_PROXY: "socks5://user:secret@s:1080" })).toThrow(
    /^The proxy ALL_PROXY=socks5:\/\/\*\*\*@s:1080 is a SOCKS proxy \(socks5\); the Jeff SDKs do not support SOCKS proxies/);
  // as httpx: every configured proxy is checked, even one for another scheme
  expect(() => httpProxyFor("https://a.com", { HTTP_PROXY: "ftp://p:21", HTTPS_PROXY: "http://ups:2" })).toThrow(
    new InvalidRequest("The proxy HTTP_PROXY=ftp://p:21 has the scheme \"ftp\"; a proxy URL must start with http:// or https://"));
  expect(() => channelProxyFor("ws://a.com", { SOCKS_PROXY: "http://s:1080" })).toThrow(/SOCKS proxy \(socks5h\)/);
  expect(() => channelProxyFor("ws://a.com", { HTTP_PROXY: "http://user@p:1" })).toThrow(
    new InvalidRequest("The proxy HTTP_PROXY=http://***@p:1 cannot be used for the channel: username provided without password"));
  expect(() => channelProxyFor("ws://a.com", { HTTP_PROXY: "http://p:1/path" })).toThrow(/path is meaningless/);
  expect(channelProxyFor("ws://a.com", { HTTP_PROXY: "https://u:p%40ss@p" })).toEqual({ shown: "https://***@p",
    secure: true, host: "p", port: 443, authorization: `Basic ${Buffer.from("u:p@ss").toString("base64")}` });
});

test("lower-case variables win over every other case, and an empty one removes the variable", () => {
  expect(environmentProxies({ No_Proxy: "a", NO_PROXY: "b", no_proxy: "c", HTTP_PROXY: "", ws_proxy: "w" }))
    .toEqual(new Map([["no", { value: "c", variable: "no_proxy" }], ["ws", { value: "w", variable: "ws_proxy" }]]));
});

// ---- through a local proxy

const PROXY_VARIABLES = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "WS_PROXY", "WSS_PROXY", "SOCKS_PROXY"];
const saved = new Map<string, string | undefined>();
for (const name of [...PROXY_VARIABLES, ...PROXY_VARIABLES.map((item) => item.toLowerCase()), "NODE_TLS_REJECT_UNAUTHORIZED"]) {
  saved.set(name, process.env[name]);
}

/** Sets the proxy variables to `env` (every other one unset) for one test. */
function useEnvironment(env: Record<string, string>): void {
  for (const name of saved.keys()) delete process.env[name];
  Object.assign(process.env, env);
}

afterEach(() => {
  for (const [name, value] of saved) {
    if (value === undefined) delete process.env[name];
    else process.env[name] = value;
  }
});

let certificate: { key: string; cert: string };
beforeAll(() => { certificate = selfSignedCertificate(); });

test("over HTTP, an http:// server is reached through HTTP_PROXY as forwarded requests, with the proxy's credentials", async () => {
  const [server, proxy] = [await startChannelServer(), await startHttpProxy()];
  const port = new URL(proxy.url).port;
  useEnvironment({ HTTP_PROXY: `http://alice:s%3Acret@127.0.0.1:${port}` });
  const jeff = await Jeff.remote(server.url, "secret-token", { timeoutMs: 2000 });
  expect(proxy.seen).toEqual([`GET ${server.url}/v1/functions`]);
  expect(proxy.authorizations).toEqual([`Basic ${Buffer.from("alice:s:cret").toString("base64")}`]);
  await jeff.close();
  await Promise.all([server.close(), proxy.close()]);
});

test("the channel to an http:// server goes through a CONNECT tunnel; HTTP through the forwarding proxy", async () => {
  const [server, proxy] = [await startChannelServer(), await startHttpProxy()];
  useEnvironment({ http_proxy: proxy.url });
  const jeff = await Jeff.remote(server.url, "secret-token", { timeoutMs: 2000, channel: true }) as ChannelJeff;
  const decisions = await jeff.decide([{ function: "triage.urgency", input: "Fire", context: CTX }]);
  expect(decisions[0]?.value).toBe(2);
  const host = new URL(server.url).host;
  expect(proxy.seen).toEqual([`CONNECT ${host}`, `GET ${server.url}/v1/functions`]);
  expect(server.headers).toEqual(["Bearer secret-token"]);
  await jeff.close();
  await Promise.all([server.close(), proxy.close()]);
});

test("an https:// server is reached through HTTPS_PROXY in CONNECT tunnels, over HTTP and over the channel", async () => {
  const [server, proxy] = [await startChannelServer({ tls: certificate }), await startHttpProxy()];
  useEnvironment({ HTTPS_PROXY: proxy.url, HTTP_PROXY: "http://127.0.0.1:1", NODE_TLS_REJECT_UNAUTHORIZED: "0" });
  const host = new URL(server.url).host;
  const rest = await Jeff.remote(server.url, null, { timeoutMs: 2000 });
  expect(proxy.seen).toEqual([`CONNECT ${host}`]);
  await rest.close();
  const jeff = await Jeff.remote(server.url, null, { timeoutMs: 2000, channel: true }) as ChannelJeff;
  const bound = await jeff.function("triage.urgency", { context: CTX });
  expect((await bound.call("Fire")).value).toBe(2);
  expect(proxy.seen).toEqual([`CONNECT ${host}`, `CONNECT ${host}`, `CONNECT ${host}`]);
  await jeff.close();
  await Promise.all([server.close(), proxy.close()]);
});

test("a host in NO_PROXY is reached directly, over HTTP and over the channel", async () => {
  const [server, proxy] = [await startChannelServer(), await startHttpProxy()];
  useEnvironment({ HTTP_PROXY: proxy.url, HTTPS_PROXY: proxy.url, NO_PROXY: "example.com, 127.0.0.1" });
  const jeff = await Jeff.remote(server.url, null, { timeoutMs: 2000, channel: true }) as ChannelJeff;
  expect((await jeff.decide([{ function: "triage.urgency", input: "Fire", context: CTX }]))[0]?.value).toBe(2);
  expect(proxy.seen).toEqual([]);
  await jeff.close();
  await Promise.all([server.close(), proxy.close()]);
});

test("a proxy refusing the channel's tunnel is BackendUnavailable naming the proxy and its answer", async () => {
  const [server, proxy] = [await startChannelServer(), await startHttpProxy({ refuseConnect: [407, "Proxy Authentication Required"] })];
  useEnvironment({ HTTP_PROXY: proxy.url });
  const error = await Jeff.remote(server.url, null, { timeoutMs: 2000, channel: true }).then(() => null, (caught: unknown) => caught);
  const host = new URL(server.url).host;
  expect(error).toEqual(new BackendUnavailable(`Cannot reach the Jeff server at ${server.url} (${wsOf(server.url)}/v1/channel): `
    + `Error: the proxy ${proxy.url}: it refused CONNECT ${host} with HTTP 407 Proxy Authentication Required`));
  await Promise.all([server.close(), proxy.close()]);
});

test("a proxy that cannot be reached is BackendUnavailable, over HTTP and over the channel", async () => {
  const server = await startChannelServer();
  useEnvironment({ HTTP_PROXY: "http://127.0.0.1:1" });
  await expect(Jeff.remote(server.url, null, { timeoutMs: 2000 })).rejects.toThrow(BackendUnavailable);
  await expect(Jeff.remote(server.url, null, { timeoutMs: 2000, channel: true })).rejects.toThrow(
    /^Cannot reach the Jeff server at .*: Error: the proxy http:\/\/127\.0\.0\.1:1: Error: connect ECONNREFUSED/);
  await server.close();
});
