#!/usr/bin/env node
// The content the hub's function needs at run time (the playground's samples: open adapters' examples only, the SDK languages its code is shown
// in, the catalogue's open adapters for a sample's local call, and the hidden adapters the owner's page shows), as TypeScript modules of the parsed files:
// app/lib/function-content.generated.ts, and app/lib/owner-only/hidden-adapters.generated.ts for the hidden adapters
// (from content/owner-only alone, so a copy of the repository without that content needs only these two folders
// replaced). Vercel bundles a function from the files its imports reach, and a file read
// with fs is not one of them, so the function reads no content file and never imports catalogue.server.ts (whose
// private-markers check and manifest reads belong only to the build): it imports this module instead. The module is
// committed, so every checkout, test, typecheck and function bundle has it whatever the order of the build's steps;
// it must always equal what the files give: check-build.mjs and test/function-content.test.ts fail when it does not,
// naming this command.
//
// Usage (in apps/hub):  node scripts/function-content.mjs write   rewrites both modules from the files
//                       node scripts/function-content.mjs check   fails if a module differs from the files
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { isDeepStrictEqual } from 'node:util';
import { parse } from 'yaml';
import { tsImport } from 'tsx/esm/api';

const HUB_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const REPO_ROOT = path.join(HUB_ROOT, '..', '..');
export const GENERATED_FILE = 'app/lib/function-content.generated.ts';
export const HIDDEN_GENERATED_FILE = 'app/lib/owner-only/hidden-adapters.generated.ts';
const EXAMPLES_DIR = 'content/examples';
const SDK_LANGUAGES_FILE = 'content/sdk-languages.yaml';
const HIDDEN_TEXT_FILE = 'content/owner-only/catalogue-text.yaml';
const HIDDEN_CATALOGUE_EXAMPLES_FILE = 'content/owner-only/catalogue-examples.yaml';
const HIDDEN_EXAMPLES_DIR = 'content/owner-only/examples';
const HIDDEN_NUMBERS_FILE = 'content/owner-only/adapter-numbers.yaml';
const WITHOUT_NUMBERS_FILE = 'content/owner-only/without-numbers.yaml';
const RETIRED_FILE = 'content/owner-only/retired-adapters.yaml';
export const WRITE_COMMAND = 'node scripts/function-content.mjs write';

// One file's YAML as data JSON can hold exactly (a value it cannot, such as .nan, is an error naming the file).
function parsed(file) {
  const where = path.join(HUB_ROOT, file);
  const data = parse(fs.readFileSync(where, 'utf8'));
  if (!isDeepStrictEqual(JSON.parse(JSON.stringify(data)), data)) throw new Error(`${file}: holds a value JSON cannot hold exactly`);
  return data;
}

// Every catalogue adapter's local call info, by the name its functions start with: an open adapter's id and short
// name (localName), null for a proprietary one (catalogue-call.server.ts: localAdapters, from the full catalogue --
// this script runs in plain Node, never in the function, so loading catalogue.server.ts here is fine; the function
// itself never does). tsImport (tsx's resolver) loads these two .ts files with the same relative, extension-less
// imports they use everywhere else (catalogue.server.ts imports './private-markers'); Node's own bare type
// stripping, which check-build.mjs uses for simpler, import-light files, cannot resolve those on its own.
async function catalogues() {
  const { loadCatalogues } = await tsImport('../app/lib/catalogue.server.ts', import.meta.url);
  return loadCatalogues(
    path.join(REPO_ROOT, 'catalogue'),
    path.join(REPO_ROOT, 'results/v14-manifests'),
    path.join(HUB_ROOT, 'content/catalogue-text.yaml'),
    path.join(REPO_ROOT, 'contract/schemas/manifest.schema.json'),
    path.join(HUB_ROOT, HIDDEN_TEXT_FILE),
    path.join(HUB_ROOT, RETIRED_FILE),
  );
}

async function localAdaptersSetting() {
  const { localAdapters } = await tsImport('../app/lib/catalogue-call.server.ts', import.meta.url);
  return Object.fromEntries(localAdapters((await catalogues()).listed));
}

// The hidden adapters (app/lib/hidden-adapters.ts: HiddenAdapter), for the owner's page through GET
// /api/admin/adapters: each one's catalogue entry, numbers and examples from content/owner-only, validated as the
// public pages validate theirs (catalogue-examples.server.ts, adapter-numbers.server.ts). An example id also used in
// content/examples is an error, and so is a hidden adapter with no numbers that content/owner-only/without-numbers.yaml
// does not name (or one it names that is not hidden).
async function hiddenAdapters() {
  const { loadExamples, requestBody } = await tsImport('../app/lib/examples.server.ts', import.meta.url);
  const { loadCatalogueExamples } = await tsImport('../app/lib/catalogue-examples.server.ts', import.meta.url);
  const { loadAdapterNumbers } = await tsImport('../app/lib/adapter-numbers.server.ts', import.meta.url);
  const withoutNumbers = parsed(WITHOUT_NUMBERS_FILE);
  if (!Array.isArray(withoutNumbers) || !withoutNumbers.every((id) => typeof id === 'string')) {
    throw new Error(`${WITHOUT_NUMBERS_FILE}: must be a list of adapter IDs`);
  }
  const { hidden } = await catalogues();
  const hiddenIds = new Set(hidden.map((entry) => entry.id));
  const notHidden = withoutNumbers.filter((id) => !hiddenIds.has(id));
  if (notHidden.length > 0) throw new Error(`${WITHOUT_NUMBERS_FILE} names adapters that are not hidden: ${notHidden.join(', ')}`);
  const numbers = loadAdapterNumbers(path.join(HUB_ROOT, HIDDEN_NUMBERS_FILE), hidden.filter((entry) => !withoutNumbers.includes(entry.id)));
  const examples = loadExamples(path.join(HUB_ROOT, HIDDEN_EXAMPLES_DIR));
  const publicIds = new Set(loadExamples(path.join(HUB_ROOT, EXAMPLES_DIR)).keys());
  const twice = [...examples.keys()].filter((id) => publicIds.has(id));
  if (twice.length > 0) throw new Error(`${HIDDEN_EXAMPLES_DIR} and ${EXAMPLES_DIR} both have the example ids ${twice.join(', ')}`);
  const shown = loadCatalogueExamples(path.join(HUB_ROOT, HIDDEN_CATALOGUE_EXAMPLES_FILE), examples, hidden);
  return hidden.map((entry) => {
    const listed = shown.get(entry.id);
    if (listed === undefined || !('examples' in listed)) throw new Error(`${HIDDEN_CATALOGUE_EXAMPLES_FILE}: ${entry.id} has no examples`);
    const measured = withoutNumbers.includes(entry.id) ? null : numbers.get(entry.id);
    if (measured === undefined) throw new Error(`${HIDDEN_NUMBERS_FILE}: ${entry.id} has no numbers`);
    return { entry, numbers: measured, examples: listed.examples.map(({ example, shows }) => ({ shows, example, request: requestBody(example) })) };
  });
}

// The examples of one file the playground offers (owner, 4 November 2026: open adapters only): those of an open
// adapter (one with public weights, catalogue/) that the live API serves (not offline). An example of an adapter the
// catalogue does not list is an error naming it.
function playgroundExamples(file, local) {
  const data = parsed(file);
  if (!Array.isArray(data)) throw new Error(`${file}: must be a list of examples`);
  return data.filter((example) => {
    const adapter = example.function.slice(0, example.function.lastIndexOf('.'));
    if (!(adapter in local)) throw new Error(`${file}: example ${example.id} calls ${example.function}, whose adapter the catalogue does not list`);
    return local[adapter] !== null && example.offline !== true;
  });
}

// The main module's text for the files as they are now.
export async function functionContentText() {
  const local = await localAdaptersSetting();
  const exampleFiles = fs.readdirSync(path.join(HUB_ROOT, EXAMPLES_DIR)).filter((name) => name.endsWith('.yaml')).sort()
    .map((name) => ({ file: `${EXAMPLES_DIR}/${name}`, data: playgroundExamples(`${EXAMPLES_DIR}/${name}`, local) }));
  if (exampleFiles.length === 0) throw new Error(`${EXAMPLES_DIR} has no example files`);
  return [
    `// Generated by scripts/function-content.mjs from ${EXAMPLES_DIR}/*.yaml, ${SDK_LANGUAGES_FILE}, the`,
    '// catalogue and content/owner-only; do not edit.',
    `// After changing those files, run \`${WRITE_COMMAND}\` in apps/hub (check-build.mjs and`,
    '// test/function-content.test.ts fail while this module differs from them).',
    '',
    '// Each example file, by its path in apps/hub, with the examples of it the playground offers as data (an open',
    '// adapter\'s, served by the API; examples.server.ts: examplesFromFiles).',
    `export const PLAYGROUND_EXAMPLE_FILES: readonly { file: string; data: unknown }[] = ${JSON.stringify(exampleFiles, null, 2)};`,
    '',
    '// content/sdk-languages.yaml as data (sdk-languages.server.ts: sdkLanguagesFrom).',
    `export const SDK_LANGUAGES_SETTING: unknown = ${JSON.stringify(parsed(SDK_LANGUAGES_FILE), null, 2)};`,
    '',
    '// Every catalogue adapter\'s local call info, by the name its functions start with: an open adapter\'s id and',
    '// short name, null for a proprietary one (examples.server.ts: localAdaptersFrom, LocalAdapters).',
    `export const LOCAL_ADAPTERS_SETTING: unknown = ${JSON.stringify(await localAdaptersSetting(), null, 2)};`,
    '',
  ].join('\n');
}

// The hidden adapters' module's text for the files as they are now.
export async function hiddenContentText() {
  return [
    '// Generated by scripts/function-content.mjs from the catalogue and content/owner-only; do not edit.',
    `// After changing those files, run \`${WRITE_COMMAND}\` in apps/hub.`,
    '',
    `// The hidden adapters and their examples (${HIDDEN_TEXT_FILE}, ${HIDDEN_CATALOGUE_EXAMPLES_FILE},`,
    `// ${HIDDEN_EXAMPLES_DIR}): only GET /api/admin/adapters returns them, to the owner. Each is a HiddenAdapter`,
    '// (../hidden-adapters.ts); typed unknown here so the function never imports the catalogue loader\'s types.',
    `export const HIDDEN_ADAPTERS: readonly unknown[] = ${JSON.stringify(await hiddenAdapters(), null, 2)};`,
    '',
  ].join('\n');
}

const MODULES = [[GENERATED_FILE, functionContentText], [HIDDEN_GENERATED_FILE, hiddenContentText]];

// Null when the committed module equals what the files give, else what is wrong and how to fix it.
export async function functionContentProblem() {
  for (const [name, textOf] of MODULES) {
    const file = path.join(HUB_ROOT, name);
    if (!fs.existsSync(file)) return `${name} is missing; run \`${WRITE_COMMAND}\` in apps/hub`;
    if (fs.readFileSync(file, 'utf8') !== await textOf()) {
      return `${name} differs from ${EXAMPLES_DIR}/*.yaml, ${SDK_LANGUAGES_FILE}, the catalogue and content/owner-only; run \`${WRITE_COMMAND}\` in apps/hub and commit it`;
    }
  }
  return null;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const command = process.argv[2];
  if (command === 'write') {
    for (const [name, textOf] of MODULES) {
      fs.mkdirSync(path.dirname(path.join(HUB_ROOT, name)), { recursive: true });
      fs.writeFileSync(path.join(HUB_ROOT, name), await textOf());
      console.log(`function-content: wrote ${name}`);
    }
  } else if (command === 'check') {
    const problem = await functionContentProblem();
    if (problem !== null) throw new Error(problem);
    console.log(`function-content: ${GENERATED_FILE} and ${HIDDEN_GENERATED_FILE} match the files`);
  } else {
    throw new Error('Usage: node scripts/function-content.mjs write|check');
  }
}
