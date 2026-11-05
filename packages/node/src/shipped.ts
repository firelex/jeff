import { fileURLToPath } from "node:url";

/** What the package carries from the repository (paths relative to the repository root and to shipped/), copied by
 * scripts/sync-shipped.ts. */
export const SHIPPED_ITEMS: readonly string[] = [
  "contract/schemas", "contract/interfaces.yaml", "contract/errors.yaml", "catalogue",
];

/** The repository root, for the sync script and the tests (an installed package never uses it). */
export function repositoryRoot(): string {
  return fileURLToPath(new URL("../../../", import.meta.url));
}

/** packages/node/shipped/: a sibling of src/ and dist/, so the same relative path works from both. */
export function shippedDir(): string {
  return fileURLToPath(new URL("../shipped/", import.meta.url));
}
