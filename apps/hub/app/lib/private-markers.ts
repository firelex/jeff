// Text that must never appear on the public site: private storage, internal machines, folders and hosts, internal
// names of people, teams and tools, the private source repository (jeff-dev; the public one is firelex/jeff), the
// names of private Keychain items, and client names. The catalogue loader checks
// every catalogue string with privateMarkersIn; the build scan (scripts/check-build.mjs) checks every built file.
export const PRIVATE_MARKERS = [
  'teamsuzie', '/mnt/', 'datigator', 'casdgx01', '/raid/', 'Delta', 'Delta+word', 'Scissero', 'Suzie', 'jeff-desk', 'jeff-dev', 'sslip', 'jeff-hub', 'jeff-v14-api',
];

// Delta is the first word of a client's name, and also a word in vendor code: KaTeX's \Delta symbol and identifiers
// such as cytoscape's dragDelta or DeltaX. 'Delta' counts only as a word of its own, not after a backslash or a letter,
// and not before a letter; but it does count right after an escaped newline, return or tab (\n, \r, \t), as text
// inside JSON or a JS string has it. So the client's full name, written with a space, is always caught by 'Delta'.
// 'Delta+word' is the name written as one word: Delta joined to a capitalised word of three or more letters, at the
// start of a word (not after a backslash or a letter).
const DELTA = /(?:(?<![\\A-Za-z])|(?<=\\[nrt]))Delta(?![A-Za-z])/;
const DELTA_JOINED = /(?:(?<![\\A-Za-z])|(?<=\\[nrt]))Delta[A-Z][a-z]{2,}/;
const PATTERNS: Record<string, RegExp> = { Delta: DELTA, 'Delta+word': DELTA_JOINED };

// The markers `text` contains, in PRIVATE_MARKERS order.
export function privateMarkersIn(text: string): string[] {
  return PRIVATE_MARKERS.filter((marker) => (marker in PATTERNS ? PATTERNS[marker].test(text) : text.includes(marker)));
}
