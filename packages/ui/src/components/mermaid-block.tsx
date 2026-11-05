import { useEffect, useRef, useState } from 'react';

export interface MermaidBlockProps {
  code: string;
}

// `mermaid` is sizeable and only the documentation ever needs it, so it is imported lazily, the first time a block
// mounts, rather than at the top of this module -- that keeps it out of every bundle that does not render a diagram
// and lets it land in a chunk of its own. The import and the one-time `initialize()` call are cached at module
// scope in a single promise, so mounting several diagrams at once (a page with more than one ```mermaid block)
// still imports and initialises the library exactly once, however many blocks ask for it before that first import
// settles. `securityLevel: 'strict'` keeps it from ever injecting tags or scripts from a diagram's own text, since
// diagram source ultimately comes from Markdown content, not code this package controls.
let mermaidOnce: Promise<typeof import('mermaid').default> | null = null;
let nextId = 0;

function loadMermaid(): Promise<typeof import('mermaid').default> {
  if (!mermaidOnce) {
    mermaidOnce = import('mermaid').then((module) => {
      const mermaid = module.default;
      mermaid.initialize({ startOnLoad: false, theme: 'neutral', securityLevel: 'strict' });
      return mermaid;
    });
  }
  return mermaidOnce;
}

/** Renders one Mermaid diagram from its source text. Used by the documentation viewer's ```mermaid code blocks. */
export function MermaidBlock({ code }: MermaidBlockProps) {
  const host = useRef<HTMLDivElement | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let current = true;
    const id = `mermaid-${++nextId}`;
    setError(null);
    loadMermaid()
      .then((mermaid) => mermaid.render(id, code))
      .then(({ svg }) => {
        if (!current) return;
        if (host.current) host.current.innerHTML = svg;
        setError(null);
      })
      .catch((failure: unknown) => {
        if (current) setError(failure instanceof Error ? failure.message : String(failure));
      });
    return () => {
      current = false;
    };
  }, [code]);

  if (error) {
    return (
      <div className="w-full rounded-card border border-danger/30 bg-panel p-3 text-left">
        <p className="m-0 text-xs font-semibold text-danger">This diagram failed to render</p>
        <p className="m-0 mt-1 text-xs text-danger">{error}</p>
        <pre className="m-0 mt-2 overflow-x-auto rounded-card bg-background p-2 font-mono text-xs text-text">{code}</pre>
      </div>
    );
  }

  return <div ref={host} data-slot="mermaid-block" className="[&_svg]:h-auto [&_svg]:max-w-full" />;
}
