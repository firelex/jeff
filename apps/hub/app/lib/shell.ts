// A string wrapped for a POSIX single-quoted shell argument: every embedded `'` closes the quote, contributes an
// escaped quote, and reopens it (the standard '\'' trick), so the result is one shell word whatever characters the
// JSON body contains (a field's raw key, shown as a placeholder's hint, may carry an apostrophe).
export function singleQuoted(text: string): string {
  return `'${text.split("'").join(String.raw`'\''`)}'`;
}
