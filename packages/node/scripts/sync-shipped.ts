// Copies the contract's schemas, interfaces.yaml and errors.yaml, and catalogue/, from the repository into
// packages/node/shipped/, so the package carries them. Run after changing either; test/shipped.test.ts fails when
// the copy is out of date.
import { cpSync, existsSync, rmSync } from "node:fs";
import { join } from "node:path";
import { SHIPPED_ITEMS, repositoryRoot, shippedDir } from "../src/shipped.js";

for (const item of SHIPPED_ITEMS) {
  if (!existsSync(join(repositoryRoot(), item))) throw new Error(`${join(repositoryRoot(), item)} is missing`);
}
rmSync(shippedDir(), { recursive: true, force: true });
for (const item of SHIPPED_ITEMS) cpSync(join(repositoryRoot(), item), join(shippedDir(), item), { recursive: true });
console.log(`Copied ${SHIPPED_ITEMS.join(", ")} to packages/node/shipped`);
