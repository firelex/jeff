import { fileURLToPath } from 'node:url';
import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

const here = fileURLToPath(new URL('.', import.meta.url));

// The paths the dev server passes on to a running Jeff server: the API, its health and schema, and the console's own
// JSON endpoints (everything under /ui except the page itself and its assets, which Vite serves).
const PROXIED = ['/v1', '/health', '/openapi.json', '/ui/login', '/ui/me', '/ui/events', '/ui/examples', '/ui/access-log'];

export default defineConfig(({ command, mode }) => {
  const env = loadEnv(mode, here, '');
  if (command === 'serve' && !env.JEFF_URL) {
    throw new Error('Set JEFF_URL to the Jeff server to talk to, for example JEFF_URL=http://127.0.0.1:8790 pnpm --filter jeff-console dev');
  }
  // The rail's "Documentation" item links straight to the public hub's docs; the build bakes the URL in, so a
  // build without it would silently ship a broken link rather than naming what is missing.
  if (command === 'build' && !env.VITE_JEFF_DOCS_URL) {
    throw new Error('Set VITE_JEFF_DOCS_URL to the public hub\'s docs URL, for example VITE_JEFF_DOCS_URL=https://jeff.example.com/docs/guides/introduction pnpm --filter jeff-console build');
  }
  return {
    // The build names its assets relatively (./assets/...), so the console works under any path prefix a proxy
    // adds (the gateway serves the server at /jeff/, the console at /jeff/ui/); the dev server stays at /ui/.
    base: command === 'build' ? './' : '/ui/',
    plugins: [react(), tailwindcss()],
    server: command === 'serve'
      ? { port: 5190, proxy: Object.fromEntries(PROXIED.map((path) => [path, { target: env.JEFF_URL, changeOrigin: true }])) }
      : undefined,
    // apps/server serves the built files at /ui (jeff_server.console); the build is committed with its source.
    build: { outDir: '../src/jeff_server/ui', emptyOutDir: true },
  };
});
