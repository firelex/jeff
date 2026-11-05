// The visitor's choice of where the code on the site runs: "On your machine (free)" (local) or "Through the API"
// (api), always in that order and with exactly these names wherever a visitor sees them (owner, 4 November 2026). Every
// worked example and adapter "Call it" block that has a local form (an open adapter's) reads and writes the same stored
// choice, so a visitor who picks local sees local everywhere, exactly as the language choice (lib/code-language.ts)
// works: choosing it on one block also switches the others on the page (an event on window), and a block shows the
// API form on its first render (the prerendered page) before reading the stored choice. A missing or blocked store
// just means the API form shows next time, never an error: the same one fallback code-language.ts explains (a
// convenience for a returning visitor, not a value a page needs to be correct).
export type CodeMode = 'api' | 'local';

// The order a visitor sees them in: local first.
export const CODE_MODES: readonly CodeMode[] = ['local', 'api'];
// Each label is its name and its price note in brackets (CODE_MODE_NAMES alone where the price is already clear).
export const CODE_MODE_NAMES: Record<CodeMode, string> = { local: 'On your machine', api: 'Through the API' };
// Only the local mode carries a price note (owner, 4 November 2026: no "(paid)" on the site).
export const CODE_MODE_PRICES: Record<CodeMode, string | null> = { local: '(free)', api: null };
export const CODE_MODE_LABELS: Record<CodeMode, string> = {
  local: `${CODE_MODE_NAMES.local} ${CODE_MODE_PRICES.local}`,
  api: CODE_MODE_NAMES.api,
};
// The choice a first-time visitor sees (and every prerendered page shows): the API, which every block can run --
// a proprietary adapter's block has no local form and would otherwise open on "Not available".
export const DEFAULT_CODE_MODE: CodeMode = 'api';

// Not prefixed jeff-hub: that is the name of a private Keychain item, which the build scan refuses on any page.
export const CODE_MODE_STORAGE_KEY = 'jeff:code-mode';
const CHANGE_EVENT = 'jeff:code-mode-change';

export function isCodeMode(value: string | null): value is CodeMode {
  return value === 'api' || value === 'local';
}

export function readStoredCodeMode(): CodeMode | null {
  try {
    const stored = window.localStorage.getItem(CODE_MODE_STORAGE_KEY);
    return isCodeMode(stored) ? stored : null;
  } catch {
    return null;
  }
}

export function storeCodeMode(mode: CodeMode): void {
  try {
    window.localStorage.setItem(CODE_MODE_STORAGE_KEY, mode);
  } catch {
    // Nothing to do: the choice just is not remembered for next time.
  }
  window.dispatchEvent(new CustomEvent<CodeMode>(CHANGE_EVENT, { detail: mode }));
}

// Calls `listener` whenever the choice changes on this page; returns the function that stops listening.
export function onCodeModeChange(listener: (mode: CodeMode) => void): () => void {
  const handler = (event: Event) => listener((event as CustomEvent<CodeMode>).detail);
  window.addEventListener(CHANGE_EVENT, handler);
  return () => window.removeEventListener(CHANGE_EVENT, handler);
}
