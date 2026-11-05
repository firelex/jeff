// A search index built once (at build/prerender time) from the docs pages, and a plain-text search over it. Shared
// between the server (which builds the index) and the browser (which searches it), so it imports only the DocPage
// *type*, never any runtime code from a .server file.
import type { DocPage } from './docs.server';

export interface SearchEntry { title: string; section: string | null; path: string; group: string }

export function buildIndex(pages: DocPage[]): SearchEntry[] {
  return pages.flatMap((p) => {
    const path = `/docs/${p.tab}/${p.id}`;
    return [
      { title: p.title, section: null, path, group: p.group },
      ...p.headings.map((h) => ({ title: p.title, section: h.text, path: `${path}#${h.id}`, group: p.group })),
    ];
  });
}

export function search(index: SearchEntry[], query: string, limit: number): SearchEntry[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return [];
  const hits = index.filter((e) => {
    const text = `${e.title} ${e.section ?? ''} ${e.group}`.toLowerCase();
    return words.every((w) => text.includes(w));
  });
  const rank = (e: SearchEntry) => (e.section === null ? 0 : 1);
  return hits.sort((a, b) => rank(a) - rank(b) || a.title.length - b.title.length).slice(0, limit);
}
