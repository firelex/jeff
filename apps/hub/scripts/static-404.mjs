#!/usr/bin/env node
// Runs after `react-router build` (package.json's "build" script): turns the SPA fallback page react-router writes
// (build/client/__spa-fallback.html, root.tsx's HydrateFallback: the site's header and the Not found page) into
// build/client/404.html, the page the host serves with status 404 for an address with no file (vercel.json), and
// removes the fallback. Its scripts are taken out: every real page is prerendered, so this page is only ever shown
// at an address the site does not have, where the app could not start (its routes would look for data that was never
// built), and it needs none. scripts/check-build.mjs checks the result.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const CLIENT_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'build/client');
const fallback = path.join(CLIENT_DIR, '__spa-fallback.html');
if (!fs.existsSync(fallback)) throw new Error(`${fallback} is missing: run \`react-router build\` first`);

const html = fs.readFileSync(fallback, 'utf8');
const withoutScripts = html
  .replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '')
  .replace(/<link rel="modulepreload"[^>]*\/>/g, '')
  .replace(/<!--\/?\$-->/g, '');
if (/<script\b|modulepreload/.test(withoutScripts)) throw new Error('static-404.mjs: a script is left in the 404 page');
if (!withoutScripts.includes('>Not found</h1>')) throw new Error('static-404.mjs: the SPA fallback page does not show the Not found page');
fs.writeFileSync(path.join(CLIENT_DIR, '404.html'), withoutScripts);
fs.rmSync(fallback);
console.log('static-404: build/client/404.html written (the Not found page, without scripts)');
