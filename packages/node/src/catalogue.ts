/** The catalogue: every known adapter manifest, looked up by ID, alias, version or interface (port of
 * jeff/catalogue.py). */

import { existsSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { readYaml, toPlain } from "./contract.js";
import { ManifestError, UnknownFunction } from "./errors.js";
import { parseRef, versionMatches } from "./ids.js";
import { type FunctionSpec, type Manifest, loadManifest, manifestPath } from "./manifest.js";
import { pyRepr, pySorted } from "./pyformat.js";
import { shippedDir } from "./shipped.js";

export class Catalogue {
  private readonly byId = new Map<string, Manifest>();
  private readonly byAlias = new Map<string, string>();
  private readonly bases: Record<string, unknown>;

  constructor(manifests: Iterable<Manifest>, bases: Record<string, unknown>) {
    for (const manifest of manifests) {
      for (const name of [manifest.id, ...manifest.aliases]) {
        if (this.byId.has(name) || this.byAlias.has(name)) {
          throw new ManifestError(`${manifest.path}: the name ${name} is already used by another adapter`);
        }
      }
      this.byId.set(manifest.id, manifest);
      for (const alias of manifest.aliases) this.byAlias.set(alias, manifest.id);
    }
    this.bases = bases;
  }

  static fromDir(path: string): Catalogue {
    const basesFile = join(path, "bases.yaml");
    if (!existsSync(basesFile)) throw new ManifestError(`${basesFile} is missing`);
    const folders = pySorted(readdirSync(path)).filter((name) => existsSync(manifestPath(join(path, name))));
    return new Catalogue(folders.map((name) => loadManifest(manifestPath(join(path, name)))),
      toPlain(readYaml(basesFile)) as Record<string, unknown>);
  }

  static shipped(): Catalogue {
    return Catalogue.fromDir(join(shippedDir(), "catalogue"));
  }

  /** The same bases, holding `manifests` too, each in place of this catalogue's manifest of the same ID. */
  replacing(manifests: Iterable<Manifest>): Catalogue {
    const merged = new Map(this.byId);
    for (const manifest of manifests) merged.set(manifest.id, manifest);
    return new Catalogue(merged.values(), this.bases);
  }

  names(): Set<string> {
    return new Set([...this.byId.keys(), ...this.byAlias.keys()]);
  }

  manifests(): Manifest[] {
    return [...this.byId.values()];
  }

  adapter(ref: string): Manifest {
    const parsed = parseRef(ref);
    const name = this.byAlias.get(parsed.adapter) ?? parsed.adapter;
    const manifest = this.byId.get(name);
    if (manifest === undefined) throw new UnknownFunction(`No adapter ${pyRepr(parsed.adapter)} in the catalogue`);
    if (parsed.version && !versionMatches(parsed.version, manifest.version)) {
      throw new UnknownFunction(`${manifest.id}@${parsed.version} is not in the catalogue; it has ${manifest.version}`);
    }
    return manifest;
  }

  function(ref: string): FunctionSpec {
    const parsed = parseRef(ref);
    const manifest = this.adapter(ref);
    const keys = [...manifest.functions.keys()];
    if (parsed.key === null) {
      if (keys.length !== 1) {
        throw new UnknownFunction(`${manifest.id} has ${keys.length} functions (${keys.join(", ")}); name one, e.g. `
          + `${manifest.id}.${keys[0]}`);
      }
      return manifest.functions.get(keys[0] as string) as FunctionSpec;
    }
    const found = manifest.functions.get(parsed.key);
    if (found === undefined) {
      throw new UnknownFunction(`${manifest.id} has no function ${pyRepr(parsed.key)}; its functions are ${keys.join(", ")}`);
    }
    return found;
  }

  manifestOf(fn: FunctionSpec): Manifest {
    const manifest = this.byId.get(fn.adapterId);
    if (manifest === undefined) throw new UnknownFunction(`No adapter ${pyRepr(fn.adapterId)} in the catalogue`);
    return manifest;
  }

  implementing(iface: string): FunctionSpec[] {
    return this.manifests().filter((manifest) => !manifest.isRouter)
      .flatMap((manifest) => [...manifest.functions.values()].filter((fn) => fn.interface === iface));
  }

  baseVersions(): Array<[string, string]> {
    return Object.entries(this.bases).flatMap(([id, versions]) =>
      Object.keys(versions as object).map((version): [string, string] => [id, version]));
  }

  base(baseId: string, version: string): Record<string, unknown> {
    const versions = this.bases[baseId] as Record<string, Record<string, unknown>> | undefined;
    // Object.hasOwn, not `versions?.[version]`: a plain object's [] lookup also finds an inherited property such as
    // 'toString' or 'constructor', which would wrongly "find" a version that was never in bases.yaml (Python's
    // `version not in versions` has no such inherited-property hazard; this is its exact equivalent).
    if (versions === undefined || !Object.hasOwn(versions, version)) {
      throw new ManifestError(`Base ${baseId} ${version} is not in bases.yaml (known: ${pyRepr(this.bases)})`);
    }
    return { ...versions[version] };
  }
}
