// The worked examples the site shows "On your machine" as well as through the API: every recorded example of an open
// adapter called through the SDK (examples.server.ts: localCallOf, exactly what a page would show). sdk-samples.ts
// and run-node-samples.ts import localExamples to build and run their local code; run with tsx from apps/hub
// (`pnpm exec tsx scripts/local-examples.ts`), it prints them as JSON for scripts/hub_examples.py --check-local:
// { "adapters": [the adapter ids the engine must serve], "examples": [{ "id", "adapter", "recorded", "python" }] }.
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { localAdapters } from '../app/lib/catalogue-call.server';
import { exampleLocalCode, loadExamples, localCallOf, type Example } from '../app/lib/examples.server';
import { HUB_ROOT } from '../app/lib/paths.server';
import type { SdkLanguage } from '../app/lib/sdk-languages';
import { catalogue } from '../app/lib/site.server';

export interface LocalExample {
  example: Example;
  // The adapter's id (what `jeff runtime serve --adapter` takes) and short name (what Jeff.local takes in Python).
  adapter: string;
  localName: string;
}

export function localExamples(): LocalExample[] {
  const adapters = localAdapters(catalogue());
  const out: LocalExample[] = [];
  for (const example of loadExamples(path.join(HUB_ROOT, 'content/examples')).values()) {
    const local = localCallOf(example, ['python'], adapters);
    if (local === null) continue;
    if (example.recorded === null) throw new Error(`example ${example.id} has no recorded answer (run scripts/hub_examples.py --record)`);
    const name = example.function.slice(0, example.function.lastIndexOf('.'));
    const entry = adapters.get(name);
    if (entry === undefined || entry === null) throw new Error(`example ${example.id}: ${name} is not an open adapter`);
    out.push({ example, adapter: local.adapter, localName: entry.localName });
  }
  if (out.length === 0) throw new Error('No worked example has a local call');
  return out;
}

// `l`'s local code in `language`.
export function localCode(l: LocalExample, language: SdkLanguage): string {
  return exampleLocalCode(l.example, language, l.localName);
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const examples = localExamples();
  console.log(JSON.stringify({
    adapters: [...new Set(examples.map((l) => l.adapter))].sort(),
    examples: examples.map((l) => ({ id: l.example.id, adapter: l.adapter, recorded: l.example.recorded, python: localCode(l, 'python') })),
  }));
}
