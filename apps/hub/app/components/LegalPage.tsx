// /terms and /privacy: a page of plain text from content/legal/ (app/lib/legal.server.ts), in the docs' Markdown
// style. A draft says so in a banner above the title, whose text comes from the loader (app/lib/legal.server.ts), so
// the words are in a built file only while a page is a draft: check-build.mjs refuses a production build while they are.
import { Markdown } from './docs/Markdown.js';
import { PAGE_TITLE } from './styles.js';

export function LegalPage({ title, banner, body }: { title: string; banner: string | null; body: string }) {
  return (
    <main id="main" className="mx-auto max-w-3xl px-4 py-12 sm:py-16">
      {banner !== null && (
        <p role="note" className="mb-8 border-2 border-warning bg-panel px-4 py-3 font-mono text-sm text-text">{banner}</p>
      )}
      <h1 className={PAGE_TITLE}>{title}</h1>
      <div className="break-words">
        <Markdown body={body} />
      </div>
    </main>
  );
}
