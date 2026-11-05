/** An empty proxy environment for a test file whose servers are on this machine and that does not test proxies: the
 * SDK follows the proxy variables (HTTPS_PROXY, HTTP_PROXY, ALL_PROXY, NO_PROXY and the like, either case) as the
 * Python SDK does, so one set in the shell would send these tests' requests to a proxy. They are unset before the
 * file's tests and put back after them (vitest runs each file in its own process). */

import { afterAll, beforeAll } from "vitest";

const PROXY_VARIABLES = /^(https?|all|no|ws|wss|socks)_proxy$/i;

export function withoutProxies(): void {
  const saved = new Map<string, string>();
  beforeAll(() => {
    for (const [name, value] of Object.entries(process.env)) {
      if (PROXY_VARIABLES.test(name) && value !== undefined) {
        saved.set(name, value);
        delete process.env[name];
      }
    }
  });
  afterAll(() => {
    for (const [name, value] of saved) process.env[name] = value;
    saved.clear();
  });
}
