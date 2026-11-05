// @vitest-environment node
//
// @jeff/ui is a component library: the app that uses it brings its own React. React and react-dom are therefore
// peer dependencies (so an app never ends up with a second copy of React, which breaks hooks), and are also dev
// dependencies so this package's own tests can render.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

const manifest = JSON.parse(
  readFileSync(path.join(path.dirname(path.dirname(fileURLToPath(import.meta.url))), 'package.json'), 'utf8'),
) as Record<string, Record<string, string> | undefined>;

describe('@jeff/ui package.json', () => {
  it.each(['react', 'react-dom'])('%s is a peer dependency (^19) and a dev dependency, not a dependency', (name) => {
    expect(manifest.peerDependencies?.[name]).toBe('^19');
    expect(manifest.devDependencies?.[name]).toMatch(/^19\.\d+\.\d+$/);
    expect(manifest.dependencies?.[name]).toBeUndefined();
  });
});
