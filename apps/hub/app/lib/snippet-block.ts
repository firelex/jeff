// What an SDK docs page shows for one snippet group (content/snippets/<name>.<ext>, one file per SDK language, and
// <name>.local.<ext> for the same code on the reader's machine): written into the page by the docs loader
// (docs.server.ts) as a fenced block of JSON in SNIPPET_FENCE, rendered by components/docs/Snippet.tsx with the
// site's language tabs and, when both forms exist, the choice of "On your machine (free)" or "Through the API
//". No runtime code from a .server file, so the browser bundle can import it.
import type { SdkLanguage } from './sdk-languages.js';

export const SNIPPET_FENCE = 'jeff-snippet';

// Each SDK language's snippet file extension.
export const SNIPPET_EXTENSIONS: Record<SdkLanguage, string> = { python: 'py', node: 'mjs', rust: 'rs', csharp: 'cs', java: 'java' };

export interface SnippetBlock {
  // The group's path under content/snippets, such as sdk/functions-1.
  id: string;
  // The code through the API (or code that needs neither, such as reading the catalogue), in each language shown, in
  // tab order; null when the group has only its local form.
  code: { language: SdkLanguage; code: string }[] | null;
  // The same on the reader's machine, in the same languages; null when the group has no local form.
  local: { language: SdkLanguage; code: string }[] | null;
}
