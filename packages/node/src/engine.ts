/** What the decision logic needs from an inference engine (port of jeff/engine.py). */

import type { Row } from "./binding.js";
import { EngineError } from "./errors.js";
import { pyRepr } from "./pyformat.js";

export const BF16 = "bf16";
export type BaseFormat = { readonly format: string; readonly format_id?: string };

/** null for a bfloat16 base (functions decide with `calibration`); for any other format its format_id. */
export function formatIdOf(baseFormat: BaseFormat): string | null {
  if (baseFormat.format === BF16) {
    if ("format_id" in baseFormat) throw new EngineError(`The engine's base format ${pyRepr(baseFormat)} is bfloat16 but carries a format_id`);
    return null;
  }
  if (!("format_id" in baseFormat) || baseFormat.format_id === undefined) {
    throw new EngineError(`The engine's base format ${pyRepr(baseFormat)} names no format_id; every base format other than `
      + `${BF16} is one exact set of weights, named by its format_id`);
  }
  return baseFormat.format_id;
}

export interface EngineResult {
  /** One list per row, over the row's options in order. */
  readonly probabilities: number[][];
  readonly timing: Record<string, unknown>;
  /** Each row's prompt length in tokens, when the engine reports it. */
  readonly inputTokens: number[] | null;
}

/** What the decision logic needs from an engine: the engine socket (SocketEngine), a Jeff server's listing
 * (ServedFunctions) or a test engine (FakeEngine). */
export interface Engine {
  /** The base's weight format, read once when the engine connects: {format: "bf16"} or an INT4 format and id. */
  readonly baseFormat: BaseFormat;
  /** The adapter IDs loaded; a cached copy for a remote engine, asked again with refresh. */
  installed(refresh?: boolean): Promise<Set<string>>;
  predict(adapterId: string, rows: Row[]): Promise<EngineResult>;
  close?(): Promise<void>;
}

/** Whether the engine serves the adapter: a cached list missing it is asked once more, with refresh. */
export async function serves(engine: Engine, adapterId: string): Promise<boolean> {
  return (await engine.installed()).has(adapterId) || (await engine.installed(true)).has(adapterId);
}
