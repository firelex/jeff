#!/usr/bin/env node
// Runs after static-404.mjs (package.json's "build" script): gives every built page its script policy, a
// Content-Security-Policy meta tag with the hashes of its own inline scripts (scripts/csp.mjs says why).
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { withScriptPolicy } from './csp.mjs';

const CLIENT_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'build/client');
const pages = fs.readdirSync(CLIENT_DIR, { recursive: true }).filter((name) => String(name).endsWith('.html'));
if (pages.length === 0) throw new Error(`csp-meta: no built pages in ${CLIENT_DIR}; run the build first`);
for (const name of pages) {
  const file = path.join(CLIENT_DIR, String(name));
  fs.writeFileSync(file, withScriptPolicy(fs.readFileSync(file, 'utf8')));
}
console.log(`csp-meta: ${pages.length} pages given their script policy`);
