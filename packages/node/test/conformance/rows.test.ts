import { expect, test } from "vitest";
import { bind, buildRow, rowToJSON } from "../../src/binding.js";
import { formatIdOf } from "../../src/engine.js";
import { calibrationFor } from "../../src/manifest.js";
import { encode } from "../../src/msgpack.js";
import { type FixtureError, catalogueFor, fixture, toBase64, wireOf } from "./fixtures.js";

interface RowCase {
  name: string;
  catalogue: string;
  base_format: { format: string; format_id?: string };
  call: { function: string; input: any; context?: any; labels?: any };
  expect: { adapter: string; row: unknown; row_msgpack: string } | { error: FixtureError };
}

const cases = fixture<{ cases: RowCase[] }>("rows.json").cases;

test.each(cases.map((item) => [item.name, item] as const))("%s", (_name, item) => {
  const catalogue = catalogueFor(item.catalogue);
  let got: unknown;
  try {
    const fn = catalogue.function(item.call.function);
    const manifest = catalogue.manifestOf(fn);
    const calibration = calibrationFor(fn, formatIdOf(item.base_format));
    const row = buildRow(bind(manifest, fn, item.call.context, item.call.labels, calibration), item.call.input);
    got = { adapter: manifest.id, row: rowToJSON(row), row_msgpack: toBase64(encode(row)) };
  } catch (error) {
    got = { error: wireOf(error) };
  }
  expect(got).toEqual(item.expect);
});
