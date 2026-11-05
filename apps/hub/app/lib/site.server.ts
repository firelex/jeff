import fs from 'node:fs';
import path from 'node:path';
import { parse } from 'yaml';
import { loadAdapterNumbers, type AdapterNumbers } from './adapter-numbers.server.js';
import { loadCatalogue, type CatalogueEntry } from './catalogue.server.js';
import { localAdapters } from './catalogue-call.server.js';
import { loadCatalogueExamples, type CatalogueExample } from './catalogue-examples.server.js';
import { checkLinks, loadDocs, type DocPage, type Sidebar } from './docs.server.js';
import { loadExamples } from './examples.server.js';
import { loadOperations, type Operation } from './openapi.server.js';
import { HUB_ROOT, REPO_ROOT } from './paths.server.js';
import { buildIndex, type SearchEntry } from './search.js';
import type { SdkLanguage } from './sdk-languages';
import { loadSdkLanguages, type ShownLanguage } from './sdk-languages.server.js';
import { priceText, publicSettings, tokensText } from './public-settings.server.js';
import { settings } from './settings.server.js';

let cachedSdkLanguages: ShownLanguage[] | null = null;

// The SDK languages the site shows code in, Python first, each with the package readers install
// (content/sdk-languages.yaml).
export function sdkLanguages(): ShownLanguage[] {
  if (cachedSdkLanguages === null) cachedSdkLanguages = loadSdkLanguages(path.join(HUB_ROOT, 'content/sdk-languages.yaml'));
  return cachedSdkLanguages;
}

export function shownLanguages(): SdkLanguage[] {
  return sdkLanguages().map((l) => l.language);
}

let cachedDocs: { pages: DocPage[]; sidebar: Sidebar } | null = null;

export function docs(): { pages: DocPage[]; sidebar: Sidebar } {
  if (cachedDocs === null) {
    const shown = publicSettings();
    cachedDocs = loadDocs(path.join(HUB_ROOT, 'content/docs'), settings().apiOrigin, shownLanguages(),
      { price: priceText(shown.priceUsd), allowance: tokensText(shown.allowanceInputTokens) }, localAdapters(catalogue()));
  }
  return cachedDocs;
}

let cachedSearchIndex: SearchEntry[] | null = null;

export function searchIndex(): SearchEntry[] {
  if (cachedSearchIndex === null) {
    cachedSearchIndex = [
      ...buildIndex(docs().pages),
      ...operations().map((op) => ({ title: op.summary, section: null, path: `/docs/api/${op.id}`, group: 'API reference' })),
      ...catalogue().map((entry) => ({ title: entry.title, section: null, path: `/catalogue/${entry.id}`, group: 'Catalogue' })),
    ];
  }
  return cachedSearchIndex;
}

let cachedOperations: Operation[] | null = null;

export function operations(): Operation[] {
  if (cachedOperations === null) {
    cachedOperations = loadOperations(path.join(REPO_ROOT, 'contract/openapi.yaml'), settings().apiOrigin);
  }
  return cachedOperations;
}

let cachedCatalogue: CatalogueEntry[] | null = null;

// Every adapter the hub lists: the open ones from catalogue/ (shipped with the SDK), the rest of what the API serves
// as proprietary.
export function catalogue(): CatalogueEntry[] {
  if (cachedCatalogue === null) {
    cachedCatalogue = loadCatalogue(
      path.join(REPO_ROOT, 'catalogue'),
      path.join(REPO_ROOT, 'results/v14-manifests'),
      path.join(HUB_ROOT, 'content/catalogue-text.yaml'),
      path.join(REPO_ROOT, 'contract/schemas/manifest.schema.json'),
      path.join(HUB_ROOT, 'content/owner-only/catalogue-text.yaml'),
      path.join(HUB_ROOT, 'content/owner-only/retired-adapters.yaml'),
    );
  }
  return cachedCatalogue;
}

let cachedPublishedWeights: Set<string> | null = null;

// The open adapters' weights public on Hugging Face today (content/published-weights.yaml), each as
// <repository>@<revision>. An entry that names no open adapter's weights is an error naming it, so the list cannot
// go stale silently; scripts/hub_examples.py --check-repos checks it against Hugging Face.
export function publishedWeights(): Set<string> {
  if (cachedPublishedWeights === null) {
    const file = path.join(HUB_ROOT, 'content/published-weights.yaml');
    const data: unknown = parse(fs.readFileSync(file, 'utf8'));
    if (!Array.isArray(data) || !data.every((d) => typeof d === 'string')) {
      throw new Error(`${file}: must be a list of <repository>@<revision> strings`);
    }
    const known = new Set(catalogue().flatMap((e) => (e.weights === null ? [] : [`${e.weights.repo}@${e.weights.revision}`])));
    const unknown = data.filter((d) => !known.has(d));
    if (unknown.length > 0) throw new Error(`${file}: names weights no open adapter in catalogue/ has: ${unknown.join(', ')}`);
    cachedPublishedWeights = new Set(data);
  }
  return cachedPublishedWeights;
}

let cachedAdapterNumbers: Map<string, AdapterNumbers> | null = null;

// How each catalogue adapter was measured (content/adapter-numbers.yaml), by adapter id: every listed adapter has an
// entry, and every entry names a listed adapter (adapter-numbers.server.ts).
export function adapterNumbers(): Map<string, AdapterNumbers> {
  if (cachedAdapterNumbers === null) {
    cachedAdapterNumbers = loadAdapterNumbers(path.join(HUB_ROOT, 'content/adapter-numbers.yaml'), catalogue());
  }
  return cachedAdapterNumbers;
}

let cachedCatalogueExamples: Map<string, CatalogueExample> | null = null;

// The worked example each adapter's catalogue page shows (content/catalogue-examples.yaml), by adapter id.
export function catalogueExamples(): Map<string, CatalogueExample> {
  if (cachedCatalogueExamples === null) {
    cachedCatalogueExamples = loadCatalogueExamples(
      path.join(HUB_ROOT, 'content/catalogue-examples.yaml'),
      loadExamples(path.join(HUB_ROOT, 'content/examples')),
      catalogue(),
    );
  }
  return cachedCatalogueExamples;
}

// The home page's example files for each SDK language: content/home-example-local.<ext> and home-example.<ext>.
export const HOME_EXAMPLE_EXTENSIONS: Record<SdkLanguage, string> = { python: 'py', node: 'mjs', rust: 'rs', csharp: 'cs', java: 'java' };

export interface HomeExample { language: SdkLanguage; package: string; local: string; remote: string }
let cachedHomeExample: HomeExample[] | null = null;

// The home page's runnable example in each shown language, side by side: the same guard call made locally (Jeff.local)
// and through the API (Jeff.remote). Only the remote files have {{origin}}, replaced by the configured API origin --
// the same substitution docs() makes, so it always shows a reader a real, working URL. A language that is not shown
// is not read, so its code never reaches the page.
export function homeExample(): HomeExample[] {
  if (cachedHomeExample === null) {
    const origin = settings().apiOrigin;
    const read = (file: string) => fs.readFileSync(path.join(HUB_ROOT, 'content', file), 'utf8');
    cachedHomeExample = sdkLanguages().map(({ language, package: name }) => {
      const ext = HOME_EXAMPLE_EXTENSIONS[language];
      return {
        language,
        package: name,
        local: read(`home-example-local.${ext}`),
        remote: read(`home-example.${ext}`).replaceAll('{{origin}}', origin),
      };
    });
  }
  return cachedHomeExample;
}

// The API tab's sidebar: operations grouped the same way `operations()` groups them, in the order they first
// appear, with the method and path as the link text.
export function apiSidebar(): { group: string; pages: { id: string; title: string }[] }[] {
  const groups: { group: string; pages: { id: string; title: string }[] }[] = [];
  for (const op of operations()) {
    let g = groups.find((candidate) => candidate.group === op.group);
    if (g === undefined) {
      g = { group: op.group, pages: [] };
      groups.push(g);
    }
    g.pages.push({ id: op.id, title: `${op.method} ${op.path}` });
  }
  return groups;
}

// Every path the prerendered build serves: the home page, the docs redirect, every docs page, the API reference
// index and every operation page, the catalogue index and every adapter page, pricing, the playground, the sign-in and
// sign-up pages, the account pages, the adapter request form, the owner's list of requests and the owner's view of
// the hidden adapters.
export function allPaths(): string[] {
  const { pages } = docs();
  return [
    '/', '/docs', '/docs/api', '/catalogue', '/pricing', '/playground', '/signin', '/signup', '/account', '/account/return',
    '/request-adapter', '/admin/requests', '/admin/adapters', '/terms', '/privacy',
    ...pages.map((page) => `/docs/${page.tab}/${page.id}`),
    ...operations().map((operation) => `/docs/api/${operation.id}`),
    ...catalogue().map((entry) => `/catalogue/${entry.id}`),
  ];
}

// Fails the build with every broken link named, before a single page is rendered, if any docs page links
// somewhere that is not one of this build's own paths.
export function checkSite(): void {
  checkLinks(docs().pages, new Set(allPaths()));
}
