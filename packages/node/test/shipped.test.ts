import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { expect, test } from "vitest";
import { SHIPPED_ITEMS, repositoryRoot, shippedDir } from "../src/shipped.js";

function files(path: string): string[] {
  if (statSync(path).isFile()) return [path];
  return readdirSync(path).sort().flatMap((name) => files(join(path, name)));
}

test("the shipped copy matches the repository (run: pnpm --filter jeff-sdk sync)", () => {
  for (const item of SHIPPED_ITEMS) {
    const source = join(repositoryRoot(), item);
    const copy = join(shippedDir(), item);
    const sourceFiles = files(source).map((path) => relative(source, path));
    expect(files(copy).map((path) => relative(copy, path))).toEqual(sourceFiles);
    for (const name of sourceFiles) {
      expect(readFileSync(join(copy, name)).equals(readFileSync(join(source, name))), `${item}/${name}`).toBe(true);
    }
  }
});
