/** References to adapters and functions: jeff.<domain>.<name>[.<function>][@<version>], or <alias>[.<function>]. */

import { InvalidReference } from "./errors.js";
import { pyRepr } from "./pyformat.js";

export interface FunctionRef {
  readonly adapter: string;
  readonly key: string | null;
  readonly version: string | null;
}

export function parseRef(ref: string): FunctionRef {
  const at = ref.indexOf("@");
  const text = at < 0 ? ref : ref.slice(0, at);
  const version = at < 0 ? "" : ref.slice(at + 1);
  const parts = text.split(".");
  if (!text || parts.some((part) => !part)) {
    throw new InvalidReference(`Malformed reference ${pyRepr(ref)}: use jeff.<domain>.<name>[.<function>] or `
      + "<alias>[.<function>], each part non-empty");
  }
  let adapter: string;
  let key: string | null;
  if (parts[0] === "jeff") {
    if (parts.length < 3) throw new InvalidReference(`Malformed reference ${pyRepr(ref)}: use jeff.<domain>.<name>[.<function>]`);
    [adapter, key] = parts.length === 4 ? [parts.slice(0, 3).join("."), parts[3] as string] : [text, null];
    if (parts.length > 4) throw new InvalidReference(`Malformed reference ${pyRepr(ref)}: too many parts`);
  } else {
    if (parts.length > 2) {
      throw new InvalidReference(`Malformed reference ${pyRepr(ref)}: use <alias>[.<function>] (aliases have no dots)`);
    }
    [adapter, key] = parts.length === 2 ? [parts[0] as string, parts[1] as string] : [parts[0] as string, null];
  }
  return { adapter, key, version: version || null };
}

export function versionMatches(wanted: string, actual: string): boolean {
  const want = wanted.split(".");
  const have = actual.split(".").slice(0, want.length);
  return have.length === want.length && have.every((part, index) => part === want[index]);
}
