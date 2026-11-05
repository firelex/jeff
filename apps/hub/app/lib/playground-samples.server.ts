// The playground's samples: the worked examples of the hub's own example files (content/examples) of open adapters
// (public weights, catalogue/; owner, 4 November 2026: no hidden adapter is in the playground) that the API
// serves (not offline ones), at most SAMPLES_PER_ADAPTER per adapter, in file order. scripts/function-content.mjs
// keeps only those (PLAYGROUND_EXAMPLE_FILES), and this checks it against the open adapters (LOCAL_ADAPTERS_SETTING). Their recorded answers are never
// shown here: every answer comes from the live call. The hub's function reads no file at run time: the example files
// and content/sdk-languages.yaml reach it as function-content.generated.ts (scripts/function-content.mjs), checked
// against the files by every build and by the tests, and validated here as the pages validate the files.
import { examplesFromFiles, localAdaptersFrom, type Example } from './examples.server.js';
import { LOCAL_ADAPTERS_SETTING, PLAYGROUND_EXAMPLE_FILES, SDK_LANGUAGES_SETTING } from './function-content.generated.js';
import type { SdkLanguage } from './sdk-languages.js';
import { sdkLanguagesFrom } from './sdk-languages.server.js';

export const SAMPLES_PER_ADAPTER = 5;

// adapter: the name the example's function starts with, the adapter's name on the pages (CatalogueEntry.name: a
// router's unversioned ID, jeff.routing.router-open for jeff.routing.router-open-v14).
export interface Sample { id: string; adapter: string; example: Example }

function adapterOf(functionId: string): string {
  return functionId.slice(0, functionId.lastIndexOf('.'));
}

let cachedSamples: Sample[] | null = null;

export function playgroundSamples(): Sample[] {
  if (cachedSamples === null) {
    const counts = new Map<string, number>();
    const samples: Sample[] = [];
    const open = localAdaptersFrom(LOCAL_ADAPTERS_SETTING, 'LOCAL_ADAPTERS_SETTING');
    for (const example of examplesFromFiles(PLAYGROUND_EXAMPLE_FILES).values()) {
      const adapter = adapterOf(example.function);
      if (example.offline || open.get(adapter) == null) {
        throw new Error(`PLAYGROUND_EXAMPLE_FILES holds ${example.id}, which is not an open adapter's example the API serves; run node scripts/function-content.mjs write`);
      }
      // The count of an adapter not seen yet is zero (not a substitute value).
      const count = counts.get(adapter) ?? 0;
      if (count >= SAMPLES_PER_ADAPTER) continue;
      counts.set(adapter, count + 1);
      samples.push({ id: example.id, adapter, example });
    }
    if (samples.length === 0) throw new Error('No playground samples: content/examples has no example the API serves');
    cachedSamples = samples;
  }
  return cachedSamples;
}

export function sampleById(id: string): Sample | null {
  return playgroundSamples().find((sample) => sample.id === id) ?? null;
}

// The playground's page with `sample` chosen (the page reads the query after hydration, routes/playground.tsx).
export function playgroundPath(sample: Sample): string {
  return `/playground?${new URLSearchParams({ adapter: sample.adapter, sample: sample.id })}`;
}

// The playground's page for `adapter`'s first sample, or null when the playground has none of it (an adapter the API
// does not serve, or one beyond the examples it takes): an adapter page's "Open in playground".
export function adapterPlaygroundLink(adapter: string): string | null {
  const sample = playgroundSamples().find((s) => s.adapter === adapter);
  return sample === undefined ? null : playgroundPath(sample);
}

// A worked example's "Open in playground": the playground with the example itself chosen when it is a sample, else
// with its adapter's first sample; null when the playground has none of the adapter's examples.
export function playgroundLink(example: Example): string | null {
  const sample = sampleById(example.id);
  return sample === null ? adapterPlaygroundLink(adapterOf(example.function)) : playgroundPath(sample);
}

let cachedLanguages: SdkLanguage[] | null = null;

// The SDK languages the site shows code in (content/sdk-languages.yaml), Python first: the playground's code tabs,
// the same as every page's.
export function playgroundLanguages(): SdkLanguage[] {
  if (cachedLanguages === null) cachedLanguages = sdkLanguagesFrom(SDK_LANGUAGES_SETTING, 'content/sdk-languages.yaml').map((l) => l.language);
  return cachedLanguages;
}
