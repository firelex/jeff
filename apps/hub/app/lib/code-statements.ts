// A generated code sample reads as a sequence of statements the reader follows: create the client, bind the
// function, call it, print the answer. `statements` lays them out with one blank line between each one, so they do
// not run together, while a multi-line statement (several output lines that are one Python/Node/Rust/C#/Java
// statement, such as a dict literal spanning lines) stays together with no blank line inside it.
//
// Each argument is one statement: a single line, several lines of the same statement (an array, joined with no
// blank between them), or `null` for a statement that this call does not have (an optional context or labels
// argument) -- skipped rather than leaving a stray blank where it would have been. Two arguments meant to end up
// glued together with no blank between them (a block's closing brace right after its last statement) are passed as
// one array.
export type Statement = string | string[] | null;

export function statements(...groups: Statement[]): string[] {
  const out: string[] = [];
  for (const group of groups) {
    if (group === null) continue;
    const lines = Array.isArray(group) ? group : [group];
    if (lines.length === 0) continue;
    if (out.length > 0) out.push('');
    out.push(...lines);
  }
  return out;
}

// `lines` indented by `indent`, a blank line staying truly empty rather than becoming trailing whitespace.
export function indentLines(lines: string[], indent: string): string[] {
  return lines.map((line) => (line === '' ? '' : `${indent}${line}`));
}
