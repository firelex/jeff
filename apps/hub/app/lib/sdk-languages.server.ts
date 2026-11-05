// content/sdk-languages.yaml: which SDK languages the site shows code in, and the package each one installs. Every
// language of SDK_LANGUAGES must be listed, with exactly `shown` (true or false) and `package` (a name, or null while
// it is not decided); a shown language with no package, or Python not shown (an offline example runs only in Python),
// is an error naming it.
import fs from 'node:fs';
import { parse } from 'yaml';
import { SDK_LANGUAGES, type SdkLanguage } from './sdk-languages.js';

export interface ShownLanguage { language: SdkLanguage; package: string }

export function loadSdkLanguages(file: string): ShownLanguage[] {
  let data: unknown;
  try {
    data = parse(fs.readFileSync(file, 'utf8'));
  } catch (error) {
    throw new Error(`${file}: ${(error as Error).message}`, { cause: error });
  }
  return sdkLanguagesFrom(data, file);
}

// The same, from the file already parsed (`file` names it in the errors): what the hub's function gets from
// function-content.generated.ts.
export function sdkLanguagesFrom(data: unknown, file: string): ShownLanguage[] {
  if (data === null || typeof data !== 'object' || Array.isArray(data)) throw new Error(`${file}: must be a map of language to settings`);
  const map = data as Record<string, unknown>;
  const unknown = Object.keys(map).filter((k) => !(SDK_LANGUAGES as readonly string[]).includes(k));
  if (unknown.length > 0) throw new Error(`${file}: unknown languages ${unknown.join(', ')} (known: ${SDK_LANGUAGES.join(', ')})`);
  const shown: ShownLanguage[] = [];
  for (const language of SDK_LANGUAGES) {
    const entry = map[language];
    if (entry === undefined) throw new Error(`${file}: no entry for ${language}`);
    if (entry === null || typeof entry !== 'object' || Array.isArray(entry) || Object.keys(entry).sort().join(',') !== 'package,shown') {
      throw new Error(`${file}: ${language} must have exactly shown and package`);
    }
    const { shown: on, package: name } = entry as { shown: unknown; package: unknown };
    if (typeof on !== 'boolean') throw new Error(`${file}: ${language}.shown must be true or false`);
    if (name !== null && (typeof name !== 'string' || name === '')) throw new Error(`${file}: ${language}.package must be a name or null`);
    if (!on) continue;
    if (name === null) throw new Error(`${file}: ${language} is shown but has no package (set ${language}.package)`);
    shown.push({ language, package: name });
  }
  if (shown[0]?.language !== 'python') throw new Error(`${file}: python must be shown (an offline example runs only in Python)`);
  return shown;
}
