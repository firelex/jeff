// A fenced code block as a small screen: a violet frame whose title bar carries the block cursor and the language
// on the left and a button to copy the text on the right, around a dark screen with light monospace code (11.3:1).
// Ported from the console's docs renderer (apps/server/ui/src/docs/Docs.tsx: CopyableCode), restyled for the hub.
//
// `bar` replaces the language label in the title bar (an Example puts its Python/curl tabs there), and `framed={false}`
// leaves out the frame itself, for a block inside a frame of its own (an Example's); `panel` makes the code the
// tabpanel those tabs control; `fill` stretches the block to its container's height (side-by-side blocks that
// should end level). The title bar never wraps: `bar` (ScreenTabs) shrinks and scrolls sideways inside its own box
// first (never the page), while Copy keeps its full width always, so it is never squeezed or pushed to its own row.
// `typed` shows the
// lines one after another, as if typed, starting `startMs` after load and `stepMs` apart (app.css: .type-line; all
// at once for prefers-reduced-motion); the text is the same selectable text either way. `wrap` wraps long lines
// inside the block instead of letting it scroll sideways (the home page's examples and the worked examples, whose
// long lines are mostly prose strings), each on its own line box (data-code-line) so a line that starts indented
// keeps that indent on a wrapped continuation too: padding-left holds the line in by its indent, and a matching
// negative text-indent cancels that for the line's own first (real) row, so its own leading spaces still render as
// spaces -- only a row the line wraps onto is held at the indent. What Copy copies is the code as written either way.
import { useState, type CSSProperties, type ReactNode } from 'react';
import { Check, Copy } from 'lucide-react';

export interface Typed { startMs: number; stepMs: number }

// `line`'s own hanging indent, in characters (monospace: 1ch per character); none for a line with no leading
// whitespace, which needs no correction.
function hangingIndent(line: string): CSSProperties {
  const indent = line.length - line.trimStart().length;
  return indent === 0 ? {} : { paddingLeft: `${indent}ch`, textIndent: `-${indent}ch` };
}

// `code` as one <span> per line: a blank line (the gap a statement's spacing leaves between two others) gets a
// non-breaking space so it still takes a line's height, as an empty block would not. `wrap` is whether the block
// wraps long lines (and so needs the hanging indent at all); `typed` is the same typed-reveal as before, now kept
// per line rather than needing its own component.
function Lines({ code, wrap, typed }: { code: string; wrap: boolean; typed?: Typed }) {
  const lines = code.split('\n');
  return (
    <>
      {lines.map((line, i) => {
        const style: CSSProperties = wrap ? hangingIndent(line) : {};
        if (typed !== undefined) (style as Record<string, string>)['--line-delay'] = `${typed.startMs + i * typed.stepMs}ms`;
        return (
          <span key={i} data-code-line="" className={`block${typed === undefined ? '' : ' type-line'}`} style={style}>
            {line === '' ? ' ' : line}
          </span>
        );
      })}
    </>
  );
}

export function CodeBlock({ code, language, bar, typed, framed = true, fill = false, wrap = false, className = 'my-5', panel }: {
  code: string;
  language: string;
  bar?: ReactNode;
  typed?: Typed;
  framed?: boolean;
  fill?: boolean;
  wrap?: boolean;
  className?: string;
  panel?: { id: string; labelledBy: string };
}) {
  const [copied, setCopied] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const copy = async () => {
    setFailure(null);
    try {
      await navigator.clipboard.writeText(code);
    } catch (error) {
      // The browser refused (no permission, an insecure page, no clipboard at all): say why. A DOMException is not
      // an Error subclass everywhere, so its message is read by shape.
      const message = typeof error === 'object' && error !== null && typeof (error as { message?: unknown }).message === 'string'
        ? (error as { message: string }).message
        : String(error);
      setFailure(message);
      return;
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };
  return (
    <div className={[framed ? 'on-screen border-2 border-ink bg-screen-frame' : '', fill ? 'flex h-full flex-col' : '', className].join(' ')}>
      <div className="flex min-h-9 flex-nowrap items-center justify-between gap-x-3 px-2.5 py-1">
        {bar ?? <span className="cursor-mark font-mono text-xs text-white">{language}</span>}
        <div className="flex shrink-0 items-center gap-2">
          {failure !== null && <span role="alert" className="max-w-[40vw] truncate text-xs text-white" title={failure}>Copy failed: {failure}</span>}
          <button
            type="button"
            className="inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap border border-white px-2 py-0.5 font-mono text-xs text-white transition-colors hover:bg-white hover:text-screen-frame"
            onClick={() => void copy()}
          >
            {copied ? <Check className="size-3.5" aria-hidden /> : <Copy className="size-3.5" aria-hidden />}
            {copied ? 'Copied' : 'Copy'}
          </button>
        </div>
      </div>
      {/* 20 px lines (leading-5) rather than leading-relaxed's 19.5 px, so a block's height is whole pixels and the
          pixel-font headings after it stay on whole pixels. */}
      <pre
        {...(panel === undefined ? {} : { role: 'tabpanel', id: panel.id, 'aria-labelledby': panel.labelledBy })}
        className={`${fill ? 'flex-1 ' : ''}${wrap ? 'whitespace-pre-wrap [overflow-wrap:anywhere] ' : 'overflow-x-auto '}mx-1.5 mb-1.5 bg-screen p-4 font-mono text-xs leading-5 text-screen-text`}>
        {wrap || typed !== undefined ? <Lines code={code} wrap={wrap} typed={typed} /> : code}
      </pre>
    </div>
  );
}
