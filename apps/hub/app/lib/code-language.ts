// The visitor's choice of SDK language for code on the site (Python, Node, ... : lib/sdk-languages.ts): the home
// page's switch and every worked example's tabs read and write the same stored choice, so a visitor who picks Node
// sees Node everywhere. A stored language the page does not show (one turned off since) is ignored where it is read.
// Choosing it on one example also switches the others on the same page (an event on window). A missing or blocked store
// (private browsing, a locked-down browser) just means Python shows next time, never an error: this is the one
// fallback this codebase allows (every other missing value is an error), because it is a convenience for a returning visitor, not a value a
// page needs to be correct.
import { isSdkLanguage, type SdkLanguage } from './sdk-languages.js';

export type CodeLanguage = SdkLanguage;

// Not prefixed jeff-hub: that is the name of a private Keychain item, which the build scan refuses on any page.
export const CODE_LANGUAGE_STORAGE_KEY = 'jeff:code-language';
const CHANGE_EVENT = 'jeff:code-language-change';

export function isCodeLanguage(value: string | null): value is CodeLanguage {
  return isSdkLanguage(value);
}

export function readStoredCodeLanguage(): CodeLanguage | null {
  try {
    const stored = window.localStorage.getItem(CODE_LANGUAGE_STORAGE_KEY);
    return isCodeLanguage(stored) ? stored : null;
  } catch {
    return null;
  }
}

export function storeCodeLanguage(language: CodeLanguage): void {
  try {
    window.localStorage.setItem(CODE_LANGUAGE_STORAGE_KEY, language);
  } catch {
    // Nothing to do: the choice just is not remembered for next time.
  }
  window.dispatchEvent(new CustomEvent<CodeLanguage>(CHANGE_EVENT, { detail: language }));
}

// Calls `listener` whenever the choice changes on this page; returns the function that stops listening.
export function onCodeLanguageChange(listener: (language: CodeLanguage) => void): () => void {
  const handler = (event: Event) => listener((event as CustomEvent<CodeLanguage>).detail);
  window.addEventListener(CHANGE_EVENT, handler);
  return () => window.removeEventListener(CHANGE_EVENT, handler);
}
