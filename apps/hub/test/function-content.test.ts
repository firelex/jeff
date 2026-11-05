// @vitest-environment node
// The function's content module (app/lib/function-content.generated.ts) is never stale: it equals what
// scripts/function-content.mjs makes from content/examples/*.yaml, content/sdk-languages.yaml and the catalogue now.
import { expect, it } from 'vitest';
// @ts-expect-error: a plain JavaScript script, with no type declarations.
import { functionContentProblem } from '../scripts/function-content.mjs';

it('the generated content module equals the content files and the catalogue', async () => {
  expect(await functionContentProblem()).toBeNull();
}, 20_000);
