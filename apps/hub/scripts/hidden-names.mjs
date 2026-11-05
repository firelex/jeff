// The names of the hidden adapters, which no built file of the public site may contain. The hidden set follows the
// manifests, as the catalogue does (app/lib/catalogue.server.ts, isPublicAdapter; final review I2): an adapter is
// public exactly when its manifest is in catalogue/ with licence.kind open and no access field, and its copy in
// results/v14-manifests, when it has one, has no access field either, and every other
// adapter the hub knows of (catalogue/ and results/v14-manifests, retired ones too) is hidden, as is every adapter
// with text in content/owner-only. For each: its ID, its aliases that are more than one plain word
// (such as jeff.routing.router-open or router-open-v14; a one-word alias such as guard is an ordinary word), its title
// (content/owner-only/catalogue-text.yaml), and the id and name of each of its examples (content/owner-only/examples).
// check-build.mjs scans every built file with hiddenNamesIn.
import fs from 'node:fs';
import path from 'node:path';
import { parse } from 'yaml';

// Folders of results/v14-manifests that hold no adapter the hub knows of (catalogue.server.ts: NOT_ADAPTER_DIRS).
const NOT_ADAPTER_DIRS = new Set(['staged-not-served']);

// Every manifest of `dir` (one folder per adapter, its manifest.yaml), by ID. A folder without one is an error.
function manifestsIn(dir) {
  const out = new Map();
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (!entry.isDirectory() || NOT_ADAPTER_DIRS.has(entry.name)) continue;
    const file = path.join(dir, entry.name, 'manifest.yaml');
    if (!fs.existsSync(file)) throw new Error(`${path.join(dir, entry.name)}: a folder with no manifest.yaml`);
    const manifest = parse(fs.readFileSync(file, 'utf8'));
    if (manifest.id !== entry.name) throw new Error(`${file}: id ${manifest.id} differs from its folder name`);
    out.set(manifest.id, manifest);
  }
  return out;
}

// An ID, alias or example id counts only as a whole name: not inside a longer one (jeff.safety.guard in
// jeff.safety.guard-v2), but followed by a dot and a function key it does (jeff.safety.guard.attempt).
function wholeName(name) {
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp(`(?<![A-Za-z0-9_.-])${escaped}(?![A-Za-z0-9_-])`);
}

export function hiddenNames(hubRoot) {
  const repoRoot = path.join(hubRoot, '..', '..');
  const open = manifestsIn(path.join(repoRoot, 'catalogue'));
  const served = manifestsIn(path.join(repoRoot, 'results/v14-manifests'));
  // Every copy the repo has of an adapter must agree on access and on licence.kind (re-review R3), the same
  // disagreement the catalogue load refuses: a served copy whose licence.kind differs from catalogue/'s is an
  // error here too, named below, before anything is judged public or hidden from it.
  for (const [id, manifest] of open) {
    const copy = served.get(id);
    if (copy !== undefined && copy.licence.kind !== manifest.licence.kind) {
      throw new Error(`${id}: catalogue/ has licence.kind ${JSON.stringify(manifest.licence.kind)}; results/v14-manifests has licence.kind ${JSON.stringify(copy.licence.kind)}`);
    }
  }
  // Public only when no copy the repo has carries an access field (re-review R3): a served copy that has one hides
  // the adapter here, and the catalogue load refuses the disagreement.
  const isPublic = (id) => open.has(id) && open.get(id).licence.kind === 'open' && open.get(id).access === undefined
    && served.get(id)?.access === undefined;
  const text = parse(fs.readFileSync(path.join(hubRoot, 'content/owner-only/catalogue-text.yaml'), 'utf8'));
  const publicWithHiddenText = Object.keys(text).filter(isPublic);
  if (publicWithHiddenText.length > 0) {
    throw new Error(`content/owner-only/catalogue-text.yaml has text for open adapters: ${publicWithHiddenText.join(', ')}`);
  }
  const ids = [...new Set([...open.keys(), ...served.keys(), ...Object.keys(text)])].filter((id) => !isPublic(id)).sort();
  const names = [];
  for (const id of ids) {
    const manifest = open.get(id) ?? served.get(id);
    if (manifest === undefined) throw new Error(`content/owner-only/catalogue-text.yaml has text for ${id}, which has no manifest`);
    names.push({ name: id, pattern: wholeName(id) });
    for (const alias of manifest.aliases) if (/[-.]/.test(alias)) names.push({ name: alias, pattern: wholeName(alias) });
    if (text[id] !== undefined) names.push({ name: text[id].title, pattern: null });
  }
  const dir = path.join(hubRoot, 'content/owner-only/examples');
  for (const file of fs.readdirSync(dir).filter((name) => name.endsWith('.yaml'))) {
    for (const example of parse(fs.readFileSync(path.join(dir, file), 'utf8'))) {
      names.push({ name: example.id, pattern: wholeName(example.id) });
      if (example.name !== undefined) names.push({ name: example.name, pattern: null });
    }
  }
  // No name at all is right only for a copy of the repository with no hidden adapter (every manifest open, no text in
  // content/owner-only), such as the public one; each part above already fails on any disagreement between them.
  return names;
}

// The hidden names `text` contains, in hiddenNames order.
export function hiddenNamesIn(text, names) {
  return names.filter(({ name, pattern }) => (pattern === null ? text.includes(name) : pattern.test(text))).map(({ name }) => name);
}
