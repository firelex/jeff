// Serves the built hub the way Vercel does (scripts/local-server.ts): build/client and the function api/index.ts, as
// vercel.json sets them up. Used by the browser checks (playwright.config.ts).
// Usage: pnpm exec tsx scripts/serve-local.ts <port>
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import api from '../api/index.js';
import { createLocalServer, type VercelConfig } from './local-server.js';

const HUB_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const config = JSON.parse(fs.readFileSync(path.join(HUB_ROOT, 'vercel.json'), 'utf8')) as VercelConfig;

const port = Number(process.argv[2]);
if (!Number.isInteger(port) || port <= 0) throw new Error('Usage: pnpm exec tsx scripts/serve-local.ts <port>');

createLocalServer({ clientDir: path.join(HUB_ROOT, config.outputDirectory), config, api: api.fetch })
  .listen(port, '127.0.0.1', () => console.log(`serve-local: build/client and /api on http://127.0.0.1:${port}/`));
