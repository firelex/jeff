// Markdown rendering shared by the console's own pages that show Markdown (the factory's "preview" of a generated
// adapter description). Copyable code blocks, Mermaid diagrams rendered in place, and external links that open in
// a new tab. No rehype-raw, so raw HTML written into the Markdown (there should never be any, but it is still
// content, not code) is shown as text rather than parsed into real elements.
import { isValidElement, useState, type ReactNode } from 'react';
import type { Components } from 'react-markdown';
import { Check, Copy } from 'lucide-react';
import { Button, MermaidBlock } from '@jeff/ui';

function CopyableCode({ code, language }: { code: string; language: string }) {
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
    <div className="my-5 overflow-hidden rounded-card border border-line bg-panel">
      <div className="flex h-9 items-center justify-between gap-3 border-b border-line bg-background px-4">
        <span className="font-mono text-xs text-mute">{language}</span>
        <div className="flex min-w-0 items-center gap-2">
          {failure !== null && <span role="alert" className="min-w-0 truncate text-xs text-danger" title={failure}>Copy failed: {failure}</span>}
          <Button variant="ghost" size="sm" onClick={() => void copy()}>
            {copied ? <Check className="size-3.5" aria-hidden /> : <Copy className="size-3.5" aria-hidden />}
            {copied ? 'Copied' : 'Copy'}
          </Button>
        </div>
      </div>
      <pre className="overflow-x-auto p-4 font-mono text-xs leading-relaxed text-text">{code}</pre>
    </div>
  );
}

// Headings get ids from their text, so a page with its own "On this page" list can scroll to them.
function textOf(children: ReactNode): string {
  if (typeof children === 'string' || typeof children === 'number') return String(children);
  if (Array.isArray(children)) return children.map(textOf).join('');
  if (isValidElement<{ children?: ReactNode }>(children)) return textOf(children.props.children);
  return '';
}

function slug(text: string): string {
  return `section-${text.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')}`;
}

export const markdownComponents: Components = {
  h1: ({ children }) => <h1 className="text-3xl font-semibold tracking-tight text-text">{children}</h1>,
  h2: ({ children }) => <h2 id={slug(textOf(children))} className="mt-12 scroll-mt-6 border-b border-line pb-2 text-xl font-semibold text-text">{children}</h2>,
  h3: ({ children }) => <h3 className="mt-8 text-base font-semibold text-text">{children}</h3>,
  p: ({ children }) => <p className="mt-4 text-sm leading-7 text-soft">{children}</p>,
  ul: ({ children }) => <ul className="mt-4 list-disc space-y-1.5 pl-6 text-sm leading-7 text-soft">{children}</ul>,
  ol: ({ children }) => <ol className="mt-4 list-decimal space-y-1.5 pl-6 text-sm leading-7 text-soft">{children}</ol>,
  a: ({ href, children }) => {
    const external = href !== undefined && /^https?:/.test(href);
    return (
      <a href={href} className="font-medium text-primary-dark underline underline-offset-2 hover:text-primary" {...(external ? { target: '_blank', rel: 'noopener noreferrer' } : {})}>
        {children}
      </a>
    );
  },
  blockquote: ({ children }) => <div className="mt-5 rounded-card border border-primary-border bg-primary-light px-4 py-1 text-sm [&_p]:mt-2 [&_p:last-child]:mb-2">{children}</div>,
  table: ({ children }) => <div className="mt-5 overflow-x-auto rounded-card border border-line bg-panel"><table className="w-full text-left text-sm">{children}</table></div>,
  th: ({ children }) => <th className="border-b border-line bg-background px-4 py-2 text-xs font-semibold text-mute">{children}</th>,
  td: ({ children }) => <td className="border-b border-line px-4 py-2.5 align-top text-sm text-soft [tr:last-child_&]:border-b-0">{children}</td>,
  hr: () => <hr className="my-10 border-line" />,
  code: ({ className, children }) => className
    ? <code className={className}>{children}</code>
    : <code className="rounded bg-background px-1.5 py-0.5 font-mono text-[0.8em] text-text">{children}</code>,
  pre: ({ children }) => {
    const child = Array.isArray(children) ? children[0] : children;
    if (!isValidElement<{ className?: string; children?: ReactNode }>(child)) throw new Error('A code block without code');
    const language = /language-([\w-]+)/.exec(child.props.className ?? '')?.[1] ?? 'text';
    const code = String(child.props.children ?? '').replace(/\n$/, '');
    if (language === 'mermaid') return <div className="my-6 flex justify-center rounded-card border border-line bg-panel p-4"><MermaidBlock code={code} /></div>;
    return <CopyableCode code={code} language={language} />;
  },
};
