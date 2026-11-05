// A benchmark set's name in a results table (or in the text), as a button that opens a short description of the set
// (content/benchmarks.yaml): what it is, how many rows it has and what a score on it measures. The description is a
// small modal dialog, as the docs' Contents menu is (DocsLayout.tsx): focus moves to its Close button when it opens,
// Tab stays inside it, Escape, Close or a click outside it closes it, and focus then goes back to the name. It is
// drawn over the page, at the end of the body (a portal, only once it is opened, so never in the prerendered HTML): a
// table that scrolls sideways at phone width cannot clip it, and a name inside a paragraph holds no block inside it.
// No inline script: the page's content security policy allows none, so this is all React.
import { createContext, useContext, useEffect, useId, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import type { Benchmark } from '../../lib/benchmarks';
import { BUTTON_SECONDARY } from '../styles';

// The benchmark sets the page links, by id: what Markdown gives the links it renders (docs-page.tsx passes the page's).
export const BenchmarksContext = createContext<ReadonlyMap<string, Benchmark> | null>(null);

const ROWS = new Intl.NumberFormat('en-US');

export function BenchmarkLink({ id, children }: { id: string; children: ReactNode }) {
  const benchmarks = useContext(BenchmarksContext);
  if (benchmarks === null) throw new Error(`A link to benchmark ${id} on a page that was given no benchmarks`);
  const benchmark = benchmarks.get(id);
  if (benchmark === undefined) throw new Error(`A link to benchmark ${id}, which this page was not given`);
  return <BenchmarkTerm benchmark={benchmark}>{children}</BenchmarkTerm>;
}

export function BenchmarkTerm({ benchmark, children }: { benchmark: Benchmark; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const opener = useRef<HTMLButtonElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const id = useId();

  useEffect(() => {
    if (!open) return undefined;
    close.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    window.addEventListener('keydown', onKey);
    const button = opener.current;
    return () => {
      window.removeEventListener('keydown', onKey);
      button?.focus();
    };
  }, [open]);

  // The Close button is the dialog's only control, so Tab (either way) keeps focus on it.
  const keepFocus = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key !== 'Tab') return;
    event.preventDefault();
    close.current?.focus();
  };

  return (
    <>
      <button
        ref={opener}
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? `${id}-dialog` : undefined}
        title={`About the ${benchmark.name} set`}
        onClick={() => setOpen(true)}
        className="cursor-help font-mono text-[0.95em] text-primary underline decoration-dotted underline-offset-2 hover:text-primary-dark"
      >
        {children}
      </button>
      {open && createPortal(
        <div
          className="fixed inset-0 z-40 flex items-center justify-center bg-ink/40 p-4"
          onClick={(event) => { if (event.target === event.currentTarget) setOpen(false); }}
        >
          <div
            id={`${id}-dialog`}
            role="dialog"
            aria-modal="true"
            aria-labelledby={`${id}-title`}
            aria-describedby={`${id}-body`}
            onKeyDown={keepFocus}
            className="max-h-full w-full max-w-md overflow-y-auto border-2 border-ink bg-panel p-5 text-left shadow-block"
          >
            <p id={`${id}-title`} className="cursor-mark font-pixel text-pixel-20 text-text">{benchmark.name}</p>
            <div id={`${id}-body`}>
              <p className="mt-1 font-mono text-xs text-mute">{ROWS.format(benchmark.rows)} rows</p>
              <p className="mt-3 text-sm leading-6 text-soft">{benchmark.what}</p>
              <p className="mt-3 text-sm leading-6 text-soft"><strong className="font-semibold text-text">What it measures:</strong> {benchmark.measures}</p>
            </div>
            <button ref={close} type="button" className={`${BUTTON_SECONDARY} mt-5 px-3 py-1`} onClick={() => setOpen(false)}>Close</button>
          </div>
        </div>,
        document.body,
      )}
    </>
  );
}
