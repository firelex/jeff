/** A value JSON can hold: what a caller passes as an input, a context value or a label description. */
export type JSONValue = string | number | boolean | null | JSONValue[] | { [key: string]: JSONValue };
