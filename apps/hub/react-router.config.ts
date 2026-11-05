import fs from 'node:fs';
import path from 'node:path';
import type { Config } from '@react-router/dev/config';
import { allPaths, checkSite } from './app/lib/site.server';
import { HUB_ROOT } from './app/lib/paths.server';

// ssr:false requires every path a route's loader can serve to be listed here, so the build can render it to
// static HTML. allPaths() (site.server.ts) is the single list, built from the same memoised docs()/operations()/
// catalogue() the routes themselves read, so the docs are loaded once per build and the prerendered set can never
// drift from what the routes actually serve. checkSite() runs first so a broken docs link stops the build before
// any page is rendered, rather than surfacing as a 404 after a page ships.
//
// scripts/check-build.mjs runs after the build, in plain Node, and cannot load site.server.ts itself to recompute
// allPaths().length (its relative imports omit extensions, which only a TypeScript-aware loader such as Vite
// resolves) -- so the count is written here, once, right after it is computed, for that script to read.
const PATH_COUNT_FILE = path.join(HUB_ROOT, 'build/prerendered-path-count.json');

export default {
  ssr: false,
  async prerender() {
    checkSite();
    const paths = allPaths();
    fs.mkdirSync(path.dirname(PATH_COUNT_FILE), { recursive: true });
    fs.writeFileSync(PATH_COUNT_FILE, JSON.stringify({ count: paths.length }));
    return paths;
  },
} satisfies Config;
