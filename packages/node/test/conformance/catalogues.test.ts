import { join } from "node:path";
import { expect, test } from "vitest";
import { readYaml, toPlain } from "../../src/contract.js";
import { shippedDir } from "../../src/shipped.js";
import { catalogueFor, fixture } from "./fixtures.js";

interface CataloguesFile {
  shipped: { bases: unknown; manifests: Record<string, unknown> };
  variants: Record<string, unknown[]>;
}

const file = fixture<CataloguesFile>("catalogues.json");

test("the shipped manifests read as the Python SDK reads them", () => {
  const shipped = catalogueFor("shipped");
  expect(shipped.manifests().map((manifest) => manifest.id).sort()).toEqual(Object.keys(file.shipped.manifests).sort());
  for (const manifest of shipped.manifests()) expect(manifest.raw).toEqual(file.shipped.manifests[manifest.id]);
  expect(toPlain(readYaml(join(shippedDir(), "catalogue", "bases.yaml")))).toEqual(file.shipped.bases);
});

test.each(Object.keys(file.variants))("variant %s loads", (name) => {
  const catalogue = catalogueFor(name);
  for (const raw of file.variants[name] as Array<{ id: string }>) expect(catalogue.adapter(raw.id).raw).toEqual(raw);
});
