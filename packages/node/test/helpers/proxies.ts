/** Local proxies for the tests: an HTTP proxy (forwarded requests and CONNECT tunnels) that records what it was asked
 * for, a TCP relay whose client-to-server direction can be paused or throttled (as the Python tests' Proxy), and a
 * self-signed certificate for a TLS server. */

import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { createServer, request as httpRequest } from "node:http";
import { type AddressInfo, type Socket, connect, createServer as createTcpServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";

export interface HttpProxy {
  url: string;
  /** "METHOD target" of every request, in order ("CONNECT host:port", "GET http://host:port/path"). */
  seen: string[];
  /** The Proxy-Authorization header of every request ("" when there was none). */
  authorizations: string[];
  close(): Promise<void>;
}

/** An HTTP proxy on 127.0.0.1. `refuseConnect`: the status every CONNECT is answered with instead of a tunnel. */
export async function startHttpProxy(options: { refuseConnect?: [number, string] } = {}): Promise<HttpProxy> {
  const seen: string[] = [];
  const authorizations: string[] = [];
  const sockets = new Set<Socket>();
  const server = createServer((request, response) => {
    seen.push(`${request.method} ${request.url}`);
    authorizations.push(request.headers["proxy-authorization"] ?? "");
    const target = new URL(request.url as string);
    const headers = { ...request.headers };
    delete headers["proxy-authorization"];
    delete headers["proxy-connection"];
    const upstream = httpRequest({ host: target.hostname, port: target.port, path: target.pathname + target.search,
      method: request.method, headers }, (answer) => {
      response.writeHead(answer.statusCode as number, answer.headers);
      answer.pipe(response);
    });
    upstream.on("error", () => response.destroy());
    request.pipe(upstream);
  });
  server.on("connection", (socket) => {
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
  });
  server.on("connect", (request, client: Socket, head: Buffer) => {
    seen.push(`CONNECT ${request.url}`);
    authorizations.push(request.headers["proxy-authorization"] ?? "");
    if (options.refuseConnect !== undefined) {
      const [status, reason] = options.refuseConnect;
      client.end(`HTTP/1.1 ${status} ${reason}\r\nContent-Length: 0\r\n\r\n`);
      return;
    }
    const at = (request.url as string).lastIndexOf(":");
    const upstream = connect({ host: (request.url as string).slice(0, at).replace(/^\[|\]$/g, ""),
      port: Number((request.url as string).slice(at + 1)) }, () => {
      client.write("HTTP/1.1 200 Connection Established\r\n\r\n");
      if (head.length > 0) upstream.write(head);
      upstream.pipe(client);
      client.pipe(upstream);
    });
    sockets.add(upstream);
    upstream.on("close", () => sockets.delete(upstream));
    upstream.on("error", () => client.destroy());
    client.on("error", () => upstream.destroy());
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  return {
    url: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, seen, authorizations,
    close: () => new Promise<void>((resolve) => {
      for (const socket of sockets) socket.destroy();
      server.close(() => resolve());
    }),
  };
}

export interface Relay {
  /** The relay as an http:// server URL. */
  url: string;
  /** While true, the relay reads nothing more from the client (its socket buffers fill and its sends stall). */
  paused: boolean;
  /** Bytes a second the relay passes from the client to the server; null for no limit. */
  rate: number | null;
  close(): Promise<void>;
}

/** A TCP relay on 127.0.0.1 to 127.0.0.1:`port`, whose client-to-server direction can be paused or throttled. */
export async function startRelay(port: number): Promise<Relay> {
  const sockets = new Set<Socket>();
  const relay: Relay = { url: "", paused: false, rate: null, close: async () => {} };
  const server = createTcpServer((client) => {
    const upstream = connect({ host: "127.0.0.1", port });
    for (const socket of [client, upstream]) {
      sockets.add(socket);
      socket.on("close", () => { sockets.delete(socket); client.destroy(); upstream.destroy(); });
      socket.on("error", () => {});
    }
    upstream.pipe(client);
    const resumeLater = (ms: number) => setTimeout(() => {
      if (relay.paused) resumeLater(10);
      else client.resume();
    }, ms);
    client.on("data", (chunk: Buffer) => {
      upstream.write(chunk);
      if (relay.paused || relay.rate !== null) {
        client.pause();
        resumeLater(relay.rate === null ? 10 : (chunk.length / relay.rate) * 1000);
      }
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  relay.url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  relay.close = () => new Promise<void>((resolve) => {
    relay.paused = false;
    for (const socket of sockets) socket.destroy();
    server.close(() => resolve());
  });
  return relay;
}

/** A self-signed key and certificate for CN=localhost, made with openssl. */
export function selfSignedCertificate(): { key: string; cert: string } {
  const directory = mkdtempSync(join(tmpdir(), "jeff-sdk-tls-"));
  try {
    const [key, cert] = [join(directory, "key.pem"), join(directory, "cert.pem")];
    execFileSync("openssl", ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert, "-days", "1",
      "-subj", "/CN=localhost"], { stdio: "pipe" });
    return { key: readFileSync(key, "utf8"), cert: readFileSync(cert, "utf8") };
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}
