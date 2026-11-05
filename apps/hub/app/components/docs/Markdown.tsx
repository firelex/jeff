// Renders a docs page's Markdown body. Ported from the console's renderer (apps/server/ui/src/docs/Docs.tsx), with
// three changes: heading ids come from the shared `headingId` (so they agree with the loader's "On this page" and
// link-checking, for both ## and ### headings, not just ##); a link to another page on this site (an `href`
// starting with `/`) renders as a React Router `<Link>` instead of a full page load; and there is still no
// rehype-raw, so raw HTML in a page's Markdown (there should never be any, but a page is still content, not code)
// is shown as text rather than parsed into real elements. A link to `#benchmark-<id>` is a benchmark set's name: it
// renders as the button that opens the set's description (BenchmarkTerm.tsx), from the page's `benchmarks`.
import { isValidElement, type ReactNode } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Link } from 'react-router';
import { MermaidBlock } from '@jeff/ui';
import { headingId } from '../../lib/slug';
import { EXAMPLE_LANGUAGE, type ExampleBlock } from '../../lib/example-block';
import { BENCHMARK_LINK_PREFIX, type Benchmark } from '../../lib/benchmarks';
import { BenchmarkLink, BenchmarksContext } from './BenchmarkTerm';
import { CodeBlock } from './CodeBlock';
import { PAGE_TITLE, TABLE_FRAME, TD, TEXT_LINK, TH } from '../styles';
import { Example } from './Example';
import { Snippet } from './Snippet';
import { SNIPPET_FENCE, type SnippetBlock } from '../../lib/snippet-block';

// Headings get ids from their text, so the "On this page" list (and links from other pages) can jump to them.
function textOf(children: ReactNode): string {
  if (typeof children === 'string' || typeof children === 'number') return String(children);
  if (Array.isArray(children)) return children.map(textOf).join('');
  if (isValidElement<{ children?: ReactNode }>(children)) return textOf(children.props.children);
  return '';
}

const components: Components = {
  h1: ({ children }) => <h1 className={PAGE_TITLE}>{children}</h1>,
  // Section headings are led by the block cursor (app.css: .cursor-mark) and ruled off underneath.
  h2: ({ children }) => <h2 id={headingId(textOf(children))} className="cursor-mark mt-12 scroll-mt-28 md:scroll-mt-20 border-b border-ink pb-2 font-pixel text-pixel-20 leading-[32px] text-text">{children}</h2>,
  h3: ({ children }) => <h3 id={headingId(textOf(children))} className="mt-8 scroll-mt-28 md:scroll-mt-20 font-pixel text-pixel-20 text-text">{children}</h3>,
  p: ({ children }) => <p className="mt-4 text-sm leading-7 text-soft">{children}</p>,
  ul: ({ children }) => <ul className="mt-4 list-disc space-y-1.5 pl-6 text-sm leading-7 text-soft">{children}</ul>,
  ol: ({ children }) => <ol className="mt-4 list-decimal space-y-1.5 pl-6 text-sm leading-7 text-soft">{children}</ol>,
  a: ({ href, children }) => {
    if (href !== undefined && href.startsWith(BENCHMARK_LINK_PREFIX)) {
      return <BenchmarkLink id={href.slice(BENCHMARK_LINK_PREFIX.length)}>{children}</BenchmarkLink>;
    }
    if (href !== undefined && href.startsWith('/')) {
      return <Link to={href} className={TEXT_LINK}>{children}</Link>;
    }
    const external = href !== undefined && /^https?:/.test(href);
    return (
      <a href={href} className={TEXT_LINK} {...(external ? { target: '_blank', rel: 'noopener noreferrer' } : {})}>
        {children}
      </a>
    );
  },
  // Links in a note are the darker violet: the usual one is under 4.5:1 on the note's pale violet.
  blockquote: ({ children }) => <div className="mt-5 border-l-4 border-primary bg-primary-light px-4 py-1 text-sm [&_p]:mt-2 [&_p:last-child]:mb-2 [&_a]:text-primary-dark">{children}</div>,
  table: ({ children }) => <div className={`${TABLE_FRAME} mt-5`}><table className="w-full text-left text-sm">{children}</table></div>,
  th: ({ children }) => <th className={TH}>{children}</th>,
  td: ({ children }) => <td className={`${TD} text-sm text-soft`}>{children}</td>,
  hr: () => <hr className="my-10 border-rule" />,
  code: ({ className, children }) => className
    ? <code className={className}>{children}</code>
    : <code className="border border-rule bg-panel px-1 py-px font-mono text-[0.8em] text-text">{children}</code>,
  pre: ({ children }) => {
    const child = Array.isArray(children) ? children[0] : children;
    if (!isValidElement<{ className?: string; children?: ReactNode }>(child)) throw new Error('A code block without code');
    const language = /language-([\w-]+)/.exec(child.props.className ?? '')?.[1] ?? 'text';
    const code = String(child.props.children ?? '').replace(/\n$/, '');
    // A worked example the docs loader expanded from an `<!-- example: <id> -->` marker (docs.server.ts).
    if (language === EXAMPLE_LANGUAGE) return <Example block={JSON.parse(code) as ExampleBlock} />;
    // A snippet group in every SDK language, expanded from an `<!-- snippet: <name> -->` marker (docs.server.ts).
    if (language === SNIPPET_FENCE) return <Snippet block={JSON.parse(code) as SnippetBlock} />;
    if (language === 'mermaid') return <div className="my-6 flex justify-center border border-ink bg-panel p-4"><MermaidBlock code={code} /></div>;
    return <CodeBlock code={code} language={language} />;
  },
};

// `benchmarks`: the benchmark sets the body links by name (DocPage.benchmarks); a body with such a link and no
// benchmarks is an error (BenchmarkLink).
export function Markdown({ body, benchmarks }: { body: string; benchmarks?: readonly Benchmark[] }) {
  const markdown = <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>{body}</ReactMarkdown>;
  if (benchmarks === undefined) return markdown;
  return <BenchmarksContext.Provider value={new Map(benchmarks.map((b) => [b.id, b]))}>{markdown}</BenchmarksContext.Provider>;
}
