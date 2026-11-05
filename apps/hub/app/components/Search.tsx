// Search over the docs, built at build time from the index the root loader serialises into the page. On a wide
// screen it is a text input right in the header, with results dropping down below it. At phone width it collapses
// to an icon button that opens a full-width panel, so the header never scrolls sideways.
import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { Search as SearchIcon, X } from 'lucide-react';
import { Link } from 'react-router';
import { search, type SearchEntry } from '../lib/search';

const LIMIT = 8;
const WIDE_SCREEN = '(min-width: 640px)';
// @jeff/ui's Input does not type a `ref` (it is not wrapped in forwardRef), and this component needs one to move
// focus on Ctrl+K/Cmd+K; a plain input with the same look avoids changing that shared component for this one use.
const INPUT = 'w-full border border-ink bg-panel px-3 py-1.5 font-mono text-sm text-text placeholder:text-faint '
  + 'focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary/30';

function isWideScreen(): boolean {
  return typeof window !== 'undefined' && window.matchMedia(WIDE_SCREEN).matches;
}

function Results({ results, onNavigate }: { results: SearchEntry[]; onNavigate: () => void }) {
  if (results.length === 0) return <p className="px-3 py-4 text-sm text-mute">No results</p>;
  return (
    <ul className="divide-y divide-rule">
      {results.map((r) => (
        <li key={r.path}>
          <Link to={r.path} onClick={onNavigate} className="block px-3 py-2 text-sm hover:bg-primary-light">
            <span className="block truncate font-mono text-text">{r.section ?? r.title}</span>
            <span className="block truncate text-xs text-mute">{r.section !== null ? `${r.title} · ${r.group}` : r.group}</span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

export function Search({ index }: { index: SearchEntry[] }) {
  const [query, setQuery] = useState('');
  const [dropdownClosed, setDropdownClosed] = useState(true);
  const [panelOpen, setPanelOpen] = useState(false);
  const desktopRef = useRef<HTMLDivElement>(null);
  const desktopInputRef = useRef<HTMLInputElement>(null);
  const panelInputRef = useRef<HTMLInputElement>(null);

  // The shortcut's label: Ctrl K, or ⌘K on a Mac. Decided after hydration, from this browser: the prerendered page
  // cannot know the visitor's platform, and shows Ctrl K until then.
  const [shortcut, setShortcut] = useState('Ctrl K');
  useEffect(() => {
    if (/Mac|iPhone|iPad/.test(navigator.platform)) setShortcut('⌘K');
  }, []);

  useEffect(() => {
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        if (isWideScreen()) {
          setDropdownClosed(false);
          desktopInputRef.current?.focus();
        } else {
          setPanelOpen(true);
        }
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);

  useEffect(() => {
    const onPointerDown = (event: PointerEvent) => {
      if (desktopRef.current !== null && !desktopRef.current.contains(event.target as Node)) setDropdownClosed(true);
    };
    document.addEventListener('pointerdown', onPointerDown);
    return () => document.removeEventListener('pointerdown', onPointerDown);
  }, []);

  useEffect(() => {
    if (panelOpen) panelInputRef.current?.focus();
  }, [panelOpen]);

  const results = query.trim() === '' ? [] : search(index, query, LIMIT);

  const closePanel = () => setPanelOpen(false);

  const handleKeyDown = (closeOnEscape: () => void) => (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Escape') closeOnEscape();
  };

  return (
    <>
      <div ref={desktopRef} className="relative hidden w-44 shrink-0 sm:block">
        <input
          ref={desktopInputRef}
          type="search"
          placeholder="Search"
          aria-label="Search"
          value={query}
          onChange={(e) => { setQuery(e.target.value); setDropdownClosed(false); }}
          onFocus={() => setDropdownClosed(false)}
          onKeyDown={handleKeyDown(() => { setQuery(''); setDropdownClosed(true); desktopInputRef.current?.blur(); })}
          className={`${INPUT} pr-16`}
        />
        <kbd className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 border border-rule px-1.5 py-0.5 text-[11px] text-mute">
          {shortcut}
        </kbd>
        {!dropdownClosed && query.trim() !== '' && (
          <div className="absolute left-0 top-full z-30 mt-1 w-full min-w-72 border border-ink bg-panel shadow-modal">
            <Results results={results} onNavigate={() => setDropdownClosed(true)} />
          </div>
        )}
      </div>

      <button
        type="button"
        aria-label="Search"
        onClick={() => setPanelOpen(true)}
        className="p-2 text-soft hover:text-text sm:hidden"
      >
        <SearchIcon className="size-4" aria-hidden />
      </button>

      {panelOpen && (
        <div role="dialog" aria-label="Search" className="fixed inset-0 z-30 overflow-y-auto bg-panel p-4 sm:hidden">
          <div className="flex items-center gap-2">
            <input
              ref={panelInputRef}
              type="search"
              placeholder="Search"
              aria-label="Search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={handleKeyDown(closePanel)}
              className={`${INPUT} flex-1`}
            />
            <button type="button" onClick={closePanel} aria-label="Close search" className="shrink-0 text-soft hover:text-text">
              <X className="size-5" aria-hidden />
            </button>
          </div>
          <div className="mt-3">
            <Results results={results} onNavigate={closePanel} />
          </div>
        </div>
      )}
    </>
  );
}
