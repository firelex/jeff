/** Proxies from the environment, chosen exactly as the Python SDK's transports choose them: RestTransport as httpx
 * (trust_env=True) does, the channel as websockets (proxy=True) does. Both read the variables as Python's
 * urllib.request.getproxies_environment reads them.
 *
 * Reading the environment: every variable named <scheme>_PROXY in any case, with an empty value ignored; the
 * lower-case form wins over any other (HTTPS_PROXY is used only when https_proxy is not set, and https_proxy set to ""
 * removes it); when REQUEST_METHOD is set (a CGI script), HTTP_PROXY in upper case is ignored, as a client's "Proxy:"
 * header can set it.
 *
 * HTTP (httpx): HTTP_PROXY for http:// URLs, HTTPS_PROXY for https:// URLs, ALL_PROXY for either when the one for the
 * scheme is not set. A value without "://" gets "http://" in front. NO_PROXY is a comma-separated list: "*" anywhere in
 * it turns every proxy off; an IPv4 or IPv6 address, or "localhost", matches that host exactly; ".example.com" matches
 * the hosts under example.com but not example.com; any other name, "example.com", matches example.com and the hosts
 * under it (not "myexample.com"); "name:port" matches only that port; an entry with "://" is a URL pattern
 * ("http://example.com" bypasses the proxy for http only). The proxy's scheme must be http or https (https: TLS to the
 * proxy); an http:// URL goes to an http proxy as a forwarded request, an https:// URL through a CONNECT tunnel. Every
 * proxy configured is checked when the transport is made, as httpx checks them when its client is made.
 *
 * Channel (websockets): for wss:// the first of WSS_PROXY, SOCKS_PROXY, HTTPS_PROXY; for ws:// the first of WS_PROXY,
 * SOCKS_PROXY, HTTPS_PROXY, HTTP_PROXY (ALL_PROXY is not read). NO_PROXY as urllib's proxy_bypass_environment reads it:
 * "*" as the whole value bypasses every host; otherwise each entry, without its leading dots, matches the host
 * itself, "host:port", or any host ending in "." + entry. The proxy must be a URL with scheme http or https, a host,
 * and no path, query or fragment; ws:// and wss:// both go through a CONNECT tunnel to <host>:<port> (TLS to the
 * server then runs inside it for wss://).
 *
 * In both, user:password in the proxy URL is sent as Proxy-Authorization: Basic. SOCKS proxies are refused with an
 * InvalidRequest: the Python SDK installs neither socksio (httpx) nor python-socks (websockets), so it cannot use them
 * either. Unlike Python on macOS and Windows, the operating system's proxy settings are not read when no variable is
 * set. */

import { Socket, connect as netConnect, isIP, isIPv4, isIPv6 } from "node:net";
import { connect as tlsConnect, type ConnectionOptions } from "node:tls";
import { InvalidRequest } from "./errors.js";

export type Environment = Readonly<Record<string, string | undefined>>;

/** A proxy setting read from the environment: its value and the variable it came from. */
export interface EnvironmentProxy { value: string; variable: string }

/** The <scheme>_PROXY variables of `env`, by lower-case scheme ("http", "https", "all", "no", ...), as
 * urllib.request.getproxies_environment reads them. */
export function environmentProxies(env: Environment = process.env): Map<string, EnvironmentProxy> {
  const proxies = new Map<string, EnvironmentProxy>();
  const found: Array<[string, string, string]> = [];
  for (const [variable, value] of Object.entries(env)) {
    if (value === undefined || variable.length <= 5 || variable[variable.length - 6] !== "_"
      || variable.slice(-5).toLowerCase() !== "proxy") continue;
    const scheme = variable.slice(0, -6).toLowerCase();
    found.push([variable, value, scheme]);
    if (value !== "") proxies.set(scheme, { value, variable });
  }
  // As urllib, against CVE-2016-1000110: in a CGI script a client's "Proxy:" header sets HTTP_PROXY.
  if (env.REQUEST_METHOD !== undefined) proxies.delete("http");
  for (const [variable, value, scheme] of found) {
    if (variable.slice(-6) !== "_proxy") continue; // the lower-case forms win
    if (value !== "") proxies.set(scheme, { value, variable });
    else proxies.delete(scheme);
  }
  return proxies;
}

/** A proxy URL as it may be shown: its user and password replaced. */
export function shownProxy(url: string): string {
  return url.replace(/^([a-zA-Z][a-zA-Z0-9+.-]*:\/\/)[^/?#]*@/, "$1***@");
}

/** Default ports httpx's URL drops (WHATWG). */
const DEFAULT_PORTS: Record<string, number> = { http: 80, https: 443, ws: 80, wss: 443, ftp: 21 };

/** The parts of a URL httpx's URLPattern compares: scheme, lower-case host (IPv6 without brackets), and port (null
 * when absent or the scheme's default). */
interface UrlParts { scheme: string; host: string; port: number | null }

function urlParts(url: string): UrlParts {
  const at = url.indexOf("://");
  const scheme = url.slice(0, at).toLowerCase();
  let authority = url.slice(at + 3).split(/[/?#]/, 1)[0] as string;
  authority = authority.slice(authority.lastIndexOf("@") + 1);
  let host = authority;
  let port: number | null = null;
  const bracketed = /^\[([^\]]*)\](?::(\d*))?$/.exec(authority);
  const plain = /^(.*?)(?::(\d*))?$/.exec(authority) as RegExpExecArray;
  if (bracketed !== null) {
    host = bracketed[1] as string;
    if (bracketed[2]) port = Number(bracketed[2]);
  } else {
    host = plain[1] as string;
    if (plain[2]) port = Number(plain[2]);
  }
  if (port !== null && DEFAULT_PORTS[scheme] === port) port = null;
  return { scheme, host: host.toLowerCase(), port };
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** httpx's URLPattern: "all://" any URL, "https://" a scheme, "all://*example.com" a domain and its subdomains,
 * "all://*.example.com" its subdomains only, "all://example.com" that host only, with an optional port. */
class UrlPattern {
  readonly scheme: string;
  readonly host: string;
  readonly port: number | null;
  private readonly hostRegex: RegExp | null;

  constructor(readonly pattern: string) {
    const parts = urlParts(pattern);
    this.scheme = parts.scheme === "all" ? "" : parts.scheme;
    this.host = parts.host === "*" ? "" : parts.host;
    this.port = parts.port;
    if (parts.host === "" || parts.host === "*") this.hostRegex = null;
    else if (parts.host.startsWith("*.")) this.hostRegex = new RegExp(`^.+\\.${escapeRegExp(parts.host.slice(2))}$`);
    else if (parts.host.startsWith("*")) this.hostRegex = new RegExp(`^(.+\\.)?${escapeRegExp(parts.host.slice(1))}$`);
    else this.hostRegex = new RegExp(`^${escapeRegExp(parts.host)}$`);
  }

  matches(url: UrlParts): boolean {
    if (this.scheme !== "" && this.scheme !== url.scheme) return false;
    if (this.host !== "" && this.hostRegex !== null && !this.hostRegex.test(url.host)) return false;
    return this.port === null || this.port === url.port;
  }

  /** Most specific first: a port, then a longer host, then a longer scheme. */
  get priority(): [number, number, number] {
    return [this.port === null ? 1 : 0, -this.host.length, -this.scheme.length];
  }
}

function comparePriority(a: UrlPattern, b: UrlPattern): number {
  const [x, y] = [a.priority, b.priority];
  for (let index = 0; index < 3; index += 1) {
    if (x[index] !== y[index]) return (x[index] as number) - (y[index] as number);
  }
  return 0;
}

function refuseSocks(proxy: EnvironmentProxy, scheme: string, python: string): never {
  throw new InvalidRequest(`The proxy ${proxy.variable}=${shownProxy(proxy.value)} is a SOCKS proxy (${scheme}); the Jeff `
    + `SDKs do not support SOCKS proxies (the Python SDK would need ${python}, which it does not install). Use an HTTP `
    + "proxy, or add the server's host to NO_PROXY");
}

/** The proxy for an HTTP request to `url`, as httpx with trust_env=True chooses it, or null for a direct connection.
 * InvalidRequest when any proxy the environment configures has a scheme other than http or https. */
export function httpProxyFor(url: string, env: Environment = process.env): string | null {
  const proxies = environmentProxies(env);
  const mounts = new Map<string, EnvironmentProxy | null>();
  for (const scheme of ["http", "https", "all"]) {
    const proxy = proxies.get(scheme);
    if (proxy !== undefined) {
      mounts.set(`${scheme}://`, { ...proxy, value: proxy.value.includes("://") ? proxy.value : `http://${proxy.value}` });
    }
  }
  for (const entry of (proxies.get("no")?.value ?? "").split(",").map((item) => item.trim())) {
    if (entry === "*") return null;
    if (entry === "") continue;
    const host = entry.split("/")[0] as string;
    if (entry.includes("://")) mounts.set(entry, null);
    else if (isIPv4(host)) mounts.set(`all://${entry}`, null);
    else if (isIPv6(host)) mounts.set(`all://[${entry}]`, null);
    else if (entry.toLowerCase() === "localhost") mounts.set(`all://${entry}`, null);
    else mounts.set(`all://*${entry}`, null);
  }
  for (const proxy of mounts.values()) {
    if (proxy === null) continue;
    const scheme = proxy.value.slice(0, proxy.value.indexOf("://")).toLowerCase();
    if (scheme === "socks5" || scheme === "socks5h") refuseSocks(proxy, scheme, "socksio");
    if (scheme !== "http" && scheme !== "https") {
      throw new InvalidRequest(`The proxy ${proxy.variable}=${shownProxy(proxy.value)} has the scheme ${JSON.stringify(scheme)}; `
        + "a proxy URL must start with http:// or https://");
    }
  }
  const target = urlParts(url);
  const ordered = [...mounts.entries()].map(([pattern, proxy]) => ({ pattern: new UrlPattern(pattern), proxy }))
    .sort((a, b) => comparePriority(a.pattern, b.pattern));
  for (const { pattern, proxy } of ordered) {
    if (pattern.matches(target)) return proxy === null ? null : proxy.value;
  }
  return null;
}

/** An HTTP proxy for the channel's CONNECT tunnel. */
export interface TunnelProxy {
  /** The proxy URL with its user and password hidden. */
  shown: string;
  secure: boolean;
  host: string;
  port: number;
  /** The Proxy-Authorization header's value, or null. */
  authorization: string | null;
}

/** urllib's proxy_bypass_environment for "host:port". */
function bypassed(host: string, port: number, noProxy: string | undefined): boolean {
  if (noProxy === undefined) return false;
  if (noProxy === "*") return true;
  const hostOnly = host.toLowerCase();
  const hostPort = `${hostOnly}:${port}`;
  for (let name of noProxy.split(",")) {
    name = name.trim();
    if (name === "") continue;
    name = name.replace(/^\.+/, "").toLowerCase();
    if (hostOnly === name || hostPort === name) return true;
    if (hostOnly.endsWith(`.${name}`) || hostPort.endsWith(`.${name}`)) return true;
  }
  return false;
}

/** websockets' parse_proxy: a URL with scheme http or https (SOCKS refused, as above), a host, and no path, query or
 * fragment. */
function tunnelProxy(proxy: EnvironmentProxy): TunnelProxy {
  const shown = shownProxy(proxy.value);
  const problem = (what: string) => new InvalidRequest(`The proxy ${proxy.variable}=${shown} cannot be used for the channel: `
    + what);
  const scheme = /^([a-zA-Z][a-zA-Z0-9+.-]*):/.exec(proxy.value)?.[1]?.toLowerCase() ?? "";
  if (["socks5h", "socks5", "socks4a", "socks4"].includes(scheme)) refuseSocks(proxy, scheme, "python-socks");
  if (scheme !== "http" && scheme !== "https") throw problem(`scheme ${JSON.stringify(scheme)} isn't supported`);
  let url: URL;
  try {
    url = new URL(proxy.value);
  } catch {
    throw problem("it is not a URL");
  }
  if (url.hostname === "") throw problem("hostname isn't provided");
  if (url.pathname !== "" && url.pathname !== "/") throw problem("path is meaningless");
  if (url.search !== "" || proxy.value.includes("?")) throw problem("query is meaningless");
  if (url.hash !== "" || proxy.value.includes("#")) throw problem("fragment is meaningless");
  let authorization: string | null = null;
  const userinfo = /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\/([^/?#]*)@/.exec(proxy.value)?.[1];
  if (userinfo !== undefined) {
    if (!userinfo.includes(":")) throw problem("username provided without password");
    const pair = `${decodeURIComponent(url.username)}:${decodeURIComponent(url.password)}`;
    authorization = `Basic ${Buffer.from(pair).toString("base64")}`;
  }
  const host = url.hostname.startsWith("[") ? url.hostname.slice(1, -1) : url.hostname;
  return { shown, secure: scheme === "https", host, port: url.port === "" ? (scheme === "https" ? 443 : 80) : Number(url.port),
    authorization };
}

/** The proxy for the channel's WebSocket at `wsUrl`, as websockets with proxy=True chooses it, or null for a direct
 * connection. InvalidRequest when the proxy it chooses cannot be used. */
export function channelProxyFor(wsUrl: string, env: Environment = process.env): TunnelProxy | null {
  const proxies = environmentProxies(env);
  const url = new URL(wsUrl);
  const secure = url.protocol === "wss:";
  const host = url.hostname.startsWith("[") ? url.hostname.slice(1, -1) : url.hostname;
  const port = url.port === "" ? (secure ? 443 : 80) : Number(url.port);
  if (bypassed(host, port, proxies.get("no")?.value)) return null;
  for (const scheme of secure ? ["wss", "socks", "https"] : ["ws", "socks", "https", "http"]) {
    const proxy = proxies.get(scheme);
    if (proxy === undefined) continue;
    if (scheme === "socks" && proxy.value.startsWith("http://")) {
      return tunnelProxy({ ...proxy, value: `socks5h://${proxy.value.slice(7)}` });
    }
    return tunnelProxy(proxy);
  }
  return null;
}

/** "host:port" as a CONNECT request and a Host header carry it (IPv6 in brackets). */
function hostPort(host: string, port: number): string {
  return `${isIPv6(host) ? `[${host}]` : host}:${port}`;
}

/** Opens a CONNECT tunnel through `proxy` to host:port: the socket, ready for the server's bytes. Fails with an Error
 * naming the proxy when it cannot be reached, refuses (any status outside 2xx), or has not answered within
 * `timeoutMs`. */
export function openTunnel(proxy: TunnelProxy, host: string, port: number, secureTarget: boolean,
  timeoutMs: number): Promise<Socket> {
  return new Promise((resolve, reject) => {
    const socket: Socket = proxy.secure
      ? tlsConnect({ host: proxy.host, port: proxy.port, servername: isIP(proxy.host) ? "" : proxy.host })
      : netConnect({ host: proxy.host, port: proxy.port });
    let settled = false;
    let answer = Buffer.alloc(0);
    const fail = (message: string) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      socket.destroy();
      reject(new Error(`the proxy ${proxy.shown}: ${message}`));
    };
    const timer = setTimeout(() => fail(`no tunnel to ${hostPort(host, port)} within ${timeoutMs} ms`), timeoutMs);
    socket.once("error", (error: Error) => fail(`${error.name}: ${error.message}`));
    socket.once("close", () => fail(`the connection closed before the proxy answered CONNECT ${hostPort(host, port)}`));
    socket.once(proxy.secure ? "secureConnect" : "connect", () => {
      const defaultPort = secureTarget ? 443 : 80;
      const lines = [`CONNECT ${hostPort(host, port)} HTTP/1.1`,
        `Host: ${port === defaultPort ? (isIPv6(host) ? `[${host}]` : host) : hostPort(host, port)}`];
      if (proxy.authorization !== null) lines.push(`Proxy-Authorization: ${proxy.authorization}`);
      socket.write(`${lines.join("\r\n")}\r\n\r\n`);
    });
    const onData = (chunk: Buffer) => {
      answer = Buffer.concat([answer, chunk]);
      const end = answer.indexOf("\r\n\r\n");
      if (end === -1) {
        if (answer.length > 64 * 1024) fail("its answer to CONNECT has no end of headers within 64 KiB");
        return;
      }
      socket.off("data", onData);
      const status = /^HTTP\/1\.[01] (\d{3})(?: ([^\r\n]*))?\r\n/.exec(answer.subarray(0, end + 2).toString("latin1"));
      if (status === null) {
        fail(`its answer to CONNECT ${hostPort(host, port)} is not an HTTP response`);
        return;
      }
      const code = Number(status[1]);
      if (code < 200 || code > 299) {
        fail(`it refused CONNECT ${hostPort(host, port)} with HTTP ${code}${status[2] ? ` ${status[2]}` : ""}`);
        return;
      }
      if (answer.length > end + 4) {
        fail(`it sent bytes after its answer to CONNECT ${hostPort(host, port)}, before the server could speak`);
        return;
      }
      settled = true;
      clearTimeout(timer);
      socket.removeAllListeners("error");
      socket.removeAllListeners("close");
      resolve(socket);
    };
    socket.on("data", onData);
  });
}

/** A createConnection for ws (and Node's http/https request) that opens the WebSocket's connection through `proxy`:
 * the tunnel, then TLS to the server inside it for wss://. Node's ClientRequest takes the socket from the callback. */
export function tunnelledConnection(proxy: TunnelProxy, secure: boolean, timeoutMs: number) {
  return (options: ConnectionOptions & { host?: string; port?: number | string },
    done: (error: Error | null, socket?: Socket) => void): undefined => {
    const host = options.host as string;
    const port = Number(options.port);
    openTunnel(proxy, host, port, secure, timeoutMs).then((socket) => {
      if (!secure) {
        done(null, socket);
        return;
      }
      const servername = options.servername !== undefined && options.servername !== null ? options.servername
        : isIP(host) ? "" : host;
      done(null, tlsConnect({ ...options, host: undefined, port: undefined, path: undefined, socket, servername } as ConnectionOptions));
    }, (error: Error) => done(error));
    return undefined;
  };
}
