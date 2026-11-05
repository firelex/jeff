// Small helpers for JSON values the console reads from the server or shows to the visitor.

/** A plain JSON object: not null, not an array. */
export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** JSON text indented by two spaces, as the console shows request and response bodies. */
export function pretty(value: unknown): string {
  return JSON.stringify(value, null, 2);
}
