import { readFileSync } from "node:fs";
import { join } from "node:path";
import { Catalogue } from "../../src/catalogue.js";
import { JeffError } from "../../src/errors.js";
import { manifestFromDict } from "../../src/manifest.js";
import { repositoryRoot } from "../../src/shipped.js";

export const ELAPSED = "<elapsed>";
const loaded = new Map<string, unknown>();

/** A file of contract/conformance/, read once. */
export function fixture<T>(name: string): T {
  if (!loaded.has(name)) {
    loaded.set(name, JSON.parse(readFileSync(join(repositoryRoot(), "contract", "conformance", name), "utf8")));
  }
  return loaded.get(name) as T;
}

interface CataloguesFile { variants: Record<string, Array<Record<string, unknown>>> }
const catalogues = new Map<string, Catalogue>();

/** A case's catalogue: the shipped one, or the shipped one with a variant's manifests in place (catalogues.json). */
export function catalogueFor(name: string): Catalogue {
  const cached = catalogues.get(name);
  if (cached !== undefined) return cached;
  const shipped = Catalogue.shipped();
  let made = shipped;
  if (name !== "shipped") {
    const variant = fixture<CataloguesFile>("catalogues.json").variants[name];
    if (variant === undefined) throw new Error(`catalogues.json has no variant ${name}`);
    made = shipped.replacing(variant.map((raw) => manifestFromDict(structuredClone(raw), `conformance/${name}/${String(raw.id)}`)));
  }
  catalogues.set(name, made);
  return made;
}

export type FixtureError = { type: string; message: string; details: Record<string, unknown> };

/** A thrown error as the fixtures write it; anything but a JeffError fails the test with the error itself. */
export function wireOf(error: unknown): FixtureError {
  if (!(error instanceof JeffError)) throw error;
  const details = { ...error.details };
  delete details.request_id;
  return normalised({ type: error.type, message: error.message, details });
}

/** The value with timing.ms of every decision an llm or human step made written ELAPSED, as the recorder writes it. */
export function normalised<T>(value: T): T {
  if (Array.isArray(value)) return value.map(normalised) as T;
  if (value === null || typeof value !== "object") return value;
  const out: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value)) out[key] = normalised(item);
  const timing = out.timing as Record<string, unknown> | undefined;
  if ((out.decided_by === "llm" || out.decided_by === "human") && timing !== undefined && "ms" in timing) {
    out.timing = { ...timing, ms: ELAPSED };
  }
  return out as T;
}

export function fromBase64(text: string): Uint8Array {
  return new Uint8Array(Buffer.from(text, "base64"));
}

export function toBase64(bytes: Uint8Array): string {
  return Buffer.from(bytes).toString("base64");
}
