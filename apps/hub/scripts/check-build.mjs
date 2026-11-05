#!/usr/bin/env node
// Runs after `react-router build` (see package.json's "build" script): fails the build if any built file (every file
// under build/client: the HTML pages, the .data files client navigation loads, the JS and CSS bundles, JSON, fonts and
// images), or any source file of this package the /api function is bundled from (functionSources()), leaks a name from PRIVATE_MARKERS (app/lib/private-markers.ts, the same list and matching the catalogue
// loader uses) or, in a built file, a hidden adapter's name or example (scripts/hidden-names.mjs) or an unresolved `{{origin}}`, `{{price}}` or `{{allowance}}` placeholder, if the number of built HTML files does not match what this build
// actually prerendered, if a built file holds the code of an SDK language content/sdk-languages.yaml has off (its
// markers, app/lib/sdk-languages.ts: SDK_CODE_MARKERS), or if the Jeff Pixel font is not shipped as checkJeffPixel()
// below requires; and, in a production build, if a built file still holds the draft banner of the terms or the privacy
// policy or an unanswered [OWNER: ...] question (scripts/legal-drafts.mjs).
import fs from 'node:fs';
import { isBuiltin } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { scriptPolicyProblem } from './csp.mjs';

const HUB_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const CLIENT_DIR = path.join(HUB_ROOT, 'build/client');

// This script loads app/lib/private-markers.ts directly below, relying on Node's built-in TypeScript type
// stripping being enabled by default (no --experimental-strip-types flag). That became the default in two
// places: v22.18.0 on the 22.x line, and v23.6.0 on the 23.x line onward (every later major, 24.x/25.x/..., forked
// after that landed, so it is always present there too) -- confirmed against nodejs.org/api/typescript.html's
// History table ("Type stripping is enabled by default" at v23.6.0, v22.18.0), since this repo carries no
// engines field or .nvmrc to pin a version and no local copy of the 22.x/23.x CHANGELOG files. On any older Node,
// the dynamic import below fails with a generic "Unknown file extension .ts" error that does not name the real
// cause, so check that first and say exactly what is missing.
// True when `version` ([major, minor, patch]) is at least `minimum`, compared numerically component by
// component (never as arrays or strings -- "23,10,0" < "23,6,0" lexicographically, which would be wrong).
function atLeast(version, minimum) {
  for (let i = 0; i < 3; i++) {
    if (version[i] !== minimum[i]) return version[i] > minimum[i];
  }
  return true;
}

function meetsMinimumNode() {
  const version = process.versions.node.split('.').map(Number);
  const [major] = version;
  if (major < 22) return false;
  if (major === 22) return atLeast(version, [22, 18, 0]);
  return atLeast(version, [23, 6, 0]); // major === 23 checked against 23.6.0; every later major always qualifies
}
if (!meetsMinimumNode()) {
  throw new Error(
    `check-build.mjs needs Node 22.18.0 or later on the 22.x line, or Node 23.6.0 or later on 23.x and ` +
      `newer, to load app/lib/private-markers.ts via built-in TypeScript type stripping without a flag. ` +
      `Found Node ${process.versions.node}. Upgrade Node, or add a TypeScript loader and change this import.`,
  );
}

// The type-stripping support is still marked experimental on some of the Node versions above (it stopped
// printing a warning only as of v24.3.0 on the 24.x line, and from v22.18.0 itself on the 22.x line), so on
// those versions Node prints an ExperimentalWarning for it unconditionally (adding a `process.on('warning', ...)`
// listener does not stop the default printing). That one specific warning is filtered out below, at the source,
// rather than passed everywhere with a blanket `--no-warnings` flag on the npm script. The match is on the
// warning's name and a substring of its message, both of which are Node-version-sensitive: if a future Node
// version changes either, this filter silently stops matching and the warning (harmless, cosmetic) reappears --
// it would not make the build fail.
const emitWarning = process.emitWarning.bind(process);
process.emitWarning = (warning, ...rest) => {
  const name = typeof rest[0] === 'string' ? rest[0] : rest[0]?.type;
  const message = typeof warning === 'string' ? warning : warning.message;
  if (name === 'ExperimentalWarning' && message.includes('Type Stripping')) return;
  emitWarning(warning, ...rest);
};
const { privateMarkersIn } = await import(path.join(HUB_ROOT, 'app/lib/private-markers.ts'));
const { SDK_CODE_MARKERS } = await import(path.join(HUB_ROOT, 'app/lib/sdk-languages.ts'));
const { parse } = await import('yaml');
const { default: ts } = await import('typescript');
const { testOnlyImports, extensionlessImports } = await import(path.join(HUB_ROOT, 'scripts/function-imports.ts'));
const { functionContentProblem } = await import(path.join(HUB_ROOT, 'scripts/function-content.mjs'));
const { hiddenNames, hiddenNamesIn } = await import(path.join(HUB_ROOT, 'scripts/hidden-names.mjs'));
const { draftProblems, DRAFT_HELP } = await import(path.join(HUB_ROOT, 'scripts/legal-drafts.mjs'));
const hidden = hiddenNames(HUB_ROOT);

// The markers of every language the setting has off (app/lib/sdk-languages.server.ts validates the file in full when
// the site is built; here only each language's `shown` is read).
const languageSetting = parse(fs.readFileSync(path.join(HUB_ROOT, 'content/sdk-languages.yaml'), 'utf8'));
const offMarkers = Object.entries(SDK_CODE_MARKERS).flatMap(([language, markers]) => {
  const shown = languageSetting[language]?.shown;
  if (typeof shown !== 'boolean') throw new Error(`content/sdk-languages.yaml: ${language}.shown must be true or false`);
  return shown ? [] : markers.map((marker) => ({ language, marker }));
});

if (!fs.existsSync(CLIENT_DIR)) throw new Error(`${CLIENT_DIR} does not exist -- run \`react-router build\` first`);

function filesUnder(dir) {
  const out = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...filesUnder(full));
    else out.push(full);
  }
  return out;
}

const builtFiles = filesUnder(CLIENT_DIR);
const htmlFiles = builtFiles.filter((file) => file.endsWith('.html'));

// Every file is read as Latin-1, one character per byte, so a binary file is scanned too and an ASCII marker matches
// in any file whatever its encoding.
const problems = [];
for (const file of builtFiles) {
  const text = fs.readFileSync(file, 'latin1');
  const where = path.relative(HUB_ROOT, file);
  for (const marker of privateMarkersIn(text)) problems.push(`${where}: contains "${marker}"`);
  // The hidden adapters reach only the owner, through the function (GET /api/admin/adapters), never a built file.
  for (const name of hiddenNamesIn(text, hidden)) problems.push(`${where}: names the hidden adapter or example "${name}"`);
  for (const placeholder of ['{{origin}}', '{{price}}', '{{allowance}}']) {
    if (text.includes(placeholder)) problems.push(`${where}: contains an unresolved ${placeholder}`);
  }
  for (const { language, marker } of offMarkers) {
    if (text.includes(marker)) problems.push(`${where}: contains ${language} code ("${marker}"), though content/sdk-languages.yaml has ${language} off`);
  }
}

// A production build may not ship the drafts of the terms and the privacy policy (scripts/legal-drafts.mjs).
const drafts = draftProblems(builtFiles.map((file) => ({ where: path.relative(HUB_ROOT, file), text: fs.readFileSync(file, 'latin1') })), process.env);
if (drafts.length > 0) throw new Error(`A production build may not ship draft legal pages. ${DRAFT_HELP}\n${drafts.join('\n')}`);

// The hub's function (api/index.ts) is bundled by Vercel from its source and every file of this package it imports,
// so the same scan runs over those sources: functionSources() follows every relative import from each entry in api/.
// (Packages from node_modules are bundled too; they are public code and are not scanned.)
const sources = functionSources();
for (const file of sources) {
  const where = path.relative(HUB_ROOT, file);
  const text = fs.readFileSync(file, 'utf8');
  for (const marker of privateMarkersIn(text)) problems.push(`${where} (in the function bundle): contains "${marker}"`);
  // PGlite is for tests only: an import Vercel's tracer can follow would put it in the production function
  // (scripts/function-imports.ts says why and how app/lib/db.server.ts loads it instead).
  for (const found of testOnlyImports(where, text)) problems.push(`${found}, a test-only package, in a way that puts it in the function bundle`);
  for (const found of extensionlessImports(where, text)) problems.push(`${found} with no file ending, which Vercel's Node cannot resolve: write it as .js`);
}
// The content the function needs reaches it as a committed module (scripts/function-content.mjs), which must equal
// the files it is made from; it is one of the function's sources, so the scan above covered it.
const contentProblem = await functionContentProblem();
if (contentProblem !== null) problems.push(contentProblem);
if (problems.length > 0) throw new Error(`Built files leak private names or unresolved text, or the function imports a test-only package:\n${problems.join('\n')}`);

// Vercel serves a static file before any function or rewrite, so a build/client/api folder would shadow /api.
if (fs.existsSync(path.join(CLIENT_DIR, 'api'))) throw new Error('build/client/api exists: static files there would shadow the /api function');

// react-router.config.ts's prerender() writes the number of paths it returned (allPaths().length) here, right
// after computing it -- check-build.mjs runs in plain Node after the build and cannot load site.server.ts itself
// (its relative imports omit extensions, which only a TypeScript-aware loader such as Vite resolves), so the count
// is recorded once, at the point it is already known, instead of being recomputed a second way.
const countFile = path.join(HUB_ROOT, 'build/prerendered-path-count.json');
if (!fs.existsSync(countFile)) throw new Error(`${countFile} is missing -- react-router.config.ts's prerender() did not run`);
const { count } = JSON.parse(fs.readFileSync(countFile, 'utf8'));

// With ssr:false, react-router always adds one static page beyond every path prerender() returns: the SPA fallback,
// which scripts/static-404.mjs turns into 404.html, the page the host (vercel.json) serves, with status 404, for an
// address with no file. It renders root.tsx's HydrateFallback: the Not found page.
const expected = count + 1;
if (htmlFiles.length !== expected) {
  throw new Error(
    `Expected ${expected} built HTML files (${count} prerendered paths + 404.html, the SPA fallback page), found ${htmlFiles.length}`,
  );
}
if (fs.existsSync(path.join(CLIENT_DIR, '__spa-fallback.html'))) throw new Error('build/client/__spa-fallback.html is still there: scripts/static-404.mjs turns it into 404.html');
const notFoundFile = path.join(CLIENT_DIR, '404.html');
if (!fs.existsSync(notFoundFile)) throw new Error('build/client/404.html is missing: scripts/static-404.mjs writes it');
const notFoundHtml = fs.readFileSync(notFoundFile, 'utf8');
if (!notFoundHtml.includes('>Not found</h1>')) throw new Error('build/client/404.html does not show the Not found page');
if (/<script\b/.test(notFoundHtml)) throw new Error('build/client/404.html has a script: it must run none');

checkJeffPixel();
checkSecurityPolicy();

console.log(`check-build: ${htmlFiles.length} HTML pages built (${count} prerendered + 404.html), no private markers or hidden adapters in any of the ${builtFiles.length} built files or the ${sources.length} source files of the /api function, no test-only package (PGlite) imported by the function, its content module (function-content.generated.ts) equal to the files, no unresolved {{origin}}, {{price}} or {{allowance}}, no code of an SDK language that is off, Jeff Pixel shipped and preloaded, every pixel-font character in the font, every page's script policy and the security headers in place.`);

// Every source file of this package that goes into a function's bundle: each file directly in api/ (Vercel makes one
// function per file there) and every file reached from one by an import. TypeScript itself lists each file's imports
// (preProcessFile: import and export ... from, export * from, bare imports, import() and require()) and resolves them
// the way the hub's tsconfig does (a specifier ending in .js names the .ts file beside it). A package from
// node_modules or Node's own modules (node:fs) is not followed; an import of this package's own code that resolves to no file stops the check.
function functionSources() {
  const apiDir = path.join(HUB_ROOT, 'api');
  const options = { module: ts.ModuleKind.ESNext, moduleResolution: ts.ModuleResolutionKind.Bundler, allowJs: true, resolveJsonModule: true };
  const seen = new Set();
  const queue = fs.readdirSync(apiDir).map((name) => path.join(apiDir, name));
  while (queue.length > 0) {
    const file = queue.pop();
    if (seen.has(file)) continue;
    seen.add(file);
    const { importedFiles } = ts.preProcessFile(fs.readFileSync(file, 'utf8'), true, true);
    for (const { fileName: specifier } of importedFiles) {
      // Node's own modules (node:fs and the like) are part of the runtime, not files to scan.
      if (isBuiltin(specifier)) continue;
      const { resolvedModule } = ts.resolveModuleName(specifier, file, options, ts.sys);
      if (resolvedModule !== undefined && resolvedModule.isExternalLibraryImport === true) continue;
      if (resolvedModule === undefined) {
        if (!specifier.startsWith('.')) throw new Error(`check-build: ${path.relative(HUB_ROOT, file)} imports ${specifier}, which is not an installed package`);
        throw new Error(`check-build: ${path.relative(HUB_ROOT, file)} imports ${specifier}, which resolves to no file`);
      }
      if (!resolvedModule.resolvedFileName.startsWith(`${HUB_ROOT}${path.sep}`)) {
        throw new Error(`check-build: ${path.relative(HUB_ROOT, file)} imports ${specifier} from outside the hub (${resolvedModule.resolvedFileName}); scan it here too before allowing it`);
      }
      queue.push(resolvedModule.resolvedFileName);
    }
  }
  return [...seen].sort();
}

// Every page carries the script policy its own inline scripts need (scripts/csp.mjs), and vercel.json gives every
// page and every /api answer the security headers (frame-ancestors 'none' and the rest).
function checkSecurityPolicy() {
  const problems = [];
  for (const file of htmlFiles) {
    const problem = scriptPolicyProblem(fs.readFileSync(file, 'utf8'));
    if (problem !== null) problems.push(`${path.relative(CLIENT_DIR, file)} ${problem}`);
  }
  const config = JSON.parse(fs.readFileSync(path.join(HUB_ROOT, 'vercel.json'), 'utf8'));
  const headersFor = (source) => {
    const entry = (config.headers ?? []).find((h) => h.source === source);
    if (entry === undefined) { problems.push(`vercel.json has no headers for ${source}`); return {}; }
    return Object.fromEntries(entry.headers.map((h) => [h.key.toLowerCase(), h.value]));
  };
  const every = headersFor('/(.*)');
  for (const [name, value] of [['x-frame-options', 'DENY'], ['x-content-type-options', 'nosniff'], ['referrer-policy', 'strict-origin-when-cross-origin']]) {
    if (every[name] !== value) problems.push(`vercel.json: ${name} for every path must be ${value}, not ${every[name]}`);
  }
  for (const source of ['/((?!api(?:/|$)).*)', '/api', '/api/(.*)']) {
    const policy = headersFor(source)['content-security-policy'];
    if (policy === undefined || !policy.includes("frame-ancestors 'none'")) problems.push(`vercel.json: the Content-Security-Policy for ${source} must have frame-ancestors 'none'`);
  }
  if (problems.length > 0) throw new Error(`Security headers or script policies are wrong:\n${problems.join('\n')}`);
}

// Jeff Pixel (packages/brand/fonts/jeff-pixel) must ship as exactly one fingerprinted file whose bytes are the
// source's, which every CSS file that names it points at and every page (but a redirect) preloads; Silkscreen, the font it replaced,
// must be gone; and every character of every element set in the pixel font (class font-pixel, at any breakpoint) must
// be one the font draws -- a missing one would silently show in another face.
function checkJeffPixel() {
  const fontDir = path.join(HUB_ROOT, '../../packages/brand/fonts/jeff-pixel');
  const assetsDir = path.join(CLIENT_DIR, 'assets');
  const shipped = fs.readdirSync(assetsDir).filter((name) => /^JeffPixel-[\w-]+\.woff2$/.test(name));
  if (shipped.length !== 1) throw new Error(`Expected one JeffPixel-*.woff2 in ${assetsDir}, found ${shipped.length}: ${shipped.join(', ')}`);
  const url = `/assets/${shipped[0]}`;
  if (!fs.readFileSync(path.join(assetsDir, shipped[0])).equals(fs.readFileSync(path.join(fontDir, 'JeffPixel.woff2')))) {
    throw new Error(`${url} differs from packages/brand/fonts/jeff-pixel/JeffPixel.woff2`);
  }

  const cssFiles = fs.readdirSync(assetsDir).filter((name) => name.endsWith('.css'));
  const naming = cssFiles.filter((name) => fs.readFileSync(path.join(assetsDir, name), 'utf8').includes('Jeff Pixel'));
  if (naming.length === 0) throw new Error('No built CSS file declares the Jeff Pixel @font-face');
  const fontProblems = [];
  for (const name of cssFiles) {
    const css = fs.readFileSync(path.join(assetsDir, name), 'utf8');
    if (/silkscreen/i.test(css)) fontProblems.push(`assets/${name}: still mentions Silkscreen`);
    if (naming.includes(name) && !css.includes(url)) fontProblems.push(`assets/${name}: names Jeff Pixel but does not point at ${url}`);
    if (/url\(\s*["']?data:font/.test(css)) fontProblems.push(`assets/${name}: inlines a font as a data URL`);
  }

  const drawn = cmapOf(fs.readFileSync(path.join(fontDir, 'JeffPixel.ttf')));
  const preload = `<link rel="preload" href="${url}" as="font" type="font/woff2" crossorigin="anonymous"/>`;
  for (const file of htmlFiles) {
    const html = fs.readFileSync(file, 'utf8');
    const where = path.relative(HUB_ROOT, file);
    // A redirect page (react-router's prerendered redirect, e.g. /docs/) renders nothing and needs no font.
    const isRedirect = html.includes('<meta http-equiv="refresh"');
    if (!isRedirect && !html.includes(preload)) fontProblems.push(`${where}: does not preload ${url}`);
    if (/silkscreen/i.test(html)) fontProblems.push(`${where}: still mentions Silkscreen`);
    for (const text of pixelFontTexts(html)) {
      const missing = [...new Set([...text].filter((ch) => !/[\n\r\t]/.test(ch) && !drawn.has(ch.codePointAt(0))))];
      if (missing.length > 0) {
        fontProblems.push(`${where}: "${text.trim()}" has characters Jeff Pixel does not draw: ${missing.map((ch) => JSON.stringify(ch)).join(' ')}`);
      }
    }
  }
  if (fontProblems.length > 0) throw new Error(`Jeff Pixel is not shipped as required:\n${fontProblems.join('\n')}`);
}

// The text of every element whose class list has font-pixel (or a breakpoint's sm:font-pixel and the like), tags,
// comments and entities resolved. The prerendered HTML is React's own output, so a plain scan for the matching
// closing tag is enough.
function pixelFontTexts(html) {
  const texts = [];
  const opening = /<([a-z][a-z0-9]*)\b[^>]*\bclass="([^"]*)"[^>]*>/g;
  for (const match of html.matchAll(opening)) {
    if (!match[2].split(/\s+/).some((cls) => cls === 'font-pixel' || cls.endsWith(':font-pixel'))) continue;
    const tag = match[1];
    const tagPattern = new RegExp(`<(/?)${tag}\\b[^>]*?(/?)>`, 'g');
    tagPattern.lastIndex = match.index + match[0].length;
    let depth = 1;
    let end = -1;
    for (let m = tagPattern.exec(html); m !== null; m = tagPattern.exec(html)) {
      if (m[1] === '/') depth -= 1;
      else if (m[2] !== '/') depth += 1;
      if (depth === 0) { end = m.index; break; }
    }
    if (end === -1) throw new Error(`check-build: no closing </${tag}> for ${match[0]}`);
    texts.push(decodeEntities(html.slice(match.index + match[0].length, end).replace(/<!--[\s\S]*?-->/g, '').replace(/<[^>]+>/g, '')));
  }
  return texts;
}

function decodeEntities(text) {
  const named = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: '\u00a0' };
  return text.replace(/&(#x[0-9a-f]+|#[0-9]+|[a-z]+);/gi, (whole, body) => {
    if (body[0] === '#') return String.fromCodePoint(body[1].toLowerCase() === 'x' ? parseInt(body.slice(2), 16) : parseInt(body.slice(1), 10));
    const ch = named[body.toLowerCase()];
    if (ch === undefined) throw new Error(`check-build: unknown HTML entity ${whole}`);
    return ch;
  });
}

// The code points a TrueType font maps to glyphs, from its cmap table's format 4 subtable (the Basic Multilingual
// Plane, all Jeff Pixel covers). JeffPixel.ttf and the shipped .woff2 are built from the same bitmaps and checked
// against each other by packages/brand/fonts/jeff-pixel/build.py.
function cmapOf(ttf) {
  const tableCount = ttf.readUInt16BE(4);
  let cmap = -1;
  for (let i = 0; i < tableCount; i++) {
    const rec = 12 + i * 16;
    if (ttf.toString('latin1', rec, rec + 4) === 'cmap') cmap = ttf.readUInt32BE(rec + 8);
  }
  if (cmap === -1) throw new Error('JeffPixel.ttf has no cmap table');
  const subtables = ttf.readUInt16BE(cmap + 2);
  for (let i = 0; i < subtables; i++) {
    const offset = cmap + ttf.readUInt32BE(cmap + 4 + i * 8 + 4);
    if (ttf.readUInt16BE(offset) !== 4) continue;
    const segments = ttf.readUInt16BE(offset + 6) / 2;
    const ends = offset + 14;
    const starts = ends + segments * 2 + 2;
    const deltas = starts + segments * 2;
    const rangeOffsets = deltas + segments * 2;
    const codes = new Set();
    for (let s = 0; s < segments; s++) {
      const start = ttf.readUInt16BE(starts + s * 2);
      const endCode = ttf.readUInt16BE(ends + s * 2);
      const delta = ttf.readInt16BE(deltas + s * 2);
      const rangeOffset = ttf.readUInt16BE(rangeOffsets + s * 2);
      for (let c = start; c <= endCode && c !== 0xffff; c++) {
        const glyph = rangeOffset === 0
          ? (c + delta) & 0xffff
          : ttf.readUInt16BE(rangeOffsets + s * 2 + rangeOffset + (c - start) * 2);
        if (glyph !== 0) codes.add(c);
      }
    }
    return codes;
  }
  throw new Error('JeffPixel.ttf has no format 4 cmap subtable');
}
