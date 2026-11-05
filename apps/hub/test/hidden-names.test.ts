// @vitest-environment node
// The hidden adapters' names that check-build.mjs refuses in any built file (scripts/hidden-names.mjs): their IDs,
// aliases of more than one word, titles, and their examples' ids and names, an ID or alias only as a whole name.
// These tests build a small repository in a temporary place; test/owner-only/hidden-content.test.tsx checks the
// real hidden adapters.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { expect, it } from 'vitest';
// @ts-expect-error: a plain JavaScript script, with no type declarations.
import { hiddenNames, hiddenNamesIn } from '../scripts/hidden-names.mjs';

// A repository with the open guard in catalogue/, served too, and in results/v14-manifests an API-only adapter whose
// ID has a version part, a retired one, and the files of content/owner-only for the API-only one.
function repository(): string {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hidden-names-'));
  const write = (file: string, text: string) => {
    fs.mkdirSync(path.dirname(path.join(root, file)), { recursive: true });
    fs.writeFileSync(path.join(root, file), text);
  };
  const guard = 'id: jeff.safety.guard\naliases: [guard]\nlicence:\n  kind: open\n  adapter: Apache-2.0\n';
  write('catalogue/jeff.safety.guard/manifest.yaml', guard);
  write('results/v14-manifests/jeff.safety.guard/manifest.yaml', guard);
  write('results/v14-manifests/jeff.operations.sample-v14/manifest.yaml',
    'id: jeff.operations.sample-v14\naliases: [sample, jeff.operations.sample, sample-v14]\nlicence:\n  kind: proprietary\n  adapter: Example proprietary\n');
  write('results/v14-manifests/jeff.operations.sample-old/manifest.yaml',
    'id: jeff.operations.sample-old\naliases: [sample-old]\nlicence:\n  kind: proprietary\n  adapter: Example proprietary\n');
  write('apps/hub/content/owner-only/catalogue-text.yaml',
    'jeff.operations.sample-v14: {title: Operations query sample, summary: S, use_when: [], avoid_when: []}\n');
  write('apps/hub/content/owner-only/examples/samples.yaml',
    '- {id: api-sample, name: A query about a late payment, function: jeff.operations.sample.route, input: x}\n');
  return root;
}

it('lists a hidden adapter by ID, alias of more than one word, title and example, and the retired one too', () => {
  const all = hiddenNames(path.join(repository(), 'apps/hub')).map((n: { name: string }) => n.name);
  for (const name of ['jeff.operations.sample-v14', 'jeff.operations.sample', 'sample-v14', 'Operations query sample', 'api-sample',
    'A query about a late payment', 'jeff.operations.sample-old', 'sample-old']) expect(all).toContain(name);
  expect(all).not.toContain('sample');
  expect(all).not.toContain('jeff.safety.guard');
});

it('finds a hidden name only as a whole name, also before a function key', () => {
  const names = hiddenNames(path.join(repository(), 'apps/hub'));
  expect(hiddenNamesIn('call jeff.operations.sample.route now', names)).toEqual(['jeff.operations.sample']);
  expect(hiddenNamesIn('"jeff.operations.sample-open.route" and jeff.operations.sample-open-v14', names)).toEqual([]);
  expect(hiddenNamesIn('a sample of the work', names)).toEqual([]);
  expect(hiddenNamesIn('<h2>Operations query sample</h2>', names)).toEqual(['Operations query sample']);
});

it('has no name when every manifest is open and content/owner-only names no adapter', () => {
  const root = repository();
  fs.rmSync(path.join(root, 'results/v14-manifests/jeff.operations.sample-v14'), { recursive: true });
  fs.rmSync(path.join(root, 'results/v14-manifests/jeff.operations.sample-old'), { recursive: true });
  fs.writeFileSync(path.join(root, 'apps/hub/content/owner-only/catalogue-text.yaml'), '{}\n');
  fs.rmSync(path.join(root, 'apps/hub/content/owner-only/examples/samples.yaml'));
  expect(hiddenNames(path.join(root, 'apps/hub'))).toEqual([]);
});

it('refuses text in content/owner-only for an adapter with no manifest, naming it', () => {
  const root = repository();
  fs.rmSync(path.join(root, 'results/v14-manifests/jeff.operations.sample-v14'), { recursive: true });
  expect(() => hiddenNames(path.join(root, 'apps/hub'))).toThrow(/text for jeff\.operations\.sample-v14, which has no manifest/);
});

it('refuses an adapter whose catalogue/ copy and results/v14-manifests copy disagree on licence.kind (re-review R3)', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hidden-names-'));
  fs.mkdirSync(path.join(root, 'catalogue/jeff.test.mismatch'), { recursive: true });
  fs.writeFileSync(path.join(root, 'catalogue/jeff.test.mismatch/manifest.yaml'),
    'id: jeff.test.mismatch\naliases: []\nlicence:\n  kind: open\n  adapter: Apache-2.0\n');
  fs.mkdirSync(path.join(root, 'results/v14-manifests/jeff.test.mismatch'), { recursive: true });
  fs.writeFileSync(path.join(root, 'results/v14-manifests/jeff.test.mismatch/manifest.yaml'),
    'id: jeff.test.mismatch\naliases: []\nlicence:\n  kind: proprietary\n  adapter: Example proprietary\n');
  expect(() => hiddenNames(path.join(root, 'apps/hub')))
    .toThrow(/jeff\.test\.mismatch: catalogue\/ has licence\.kind "open"; results\/v14-manifests has licence\.kind "proprietary"/);
});
