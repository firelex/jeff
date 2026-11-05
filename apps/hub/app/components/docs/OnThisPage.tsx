// The current page's own headings, as in-page jump links. depth === 3 headings are indented under their depth === 2
// parent.
import type { Heading } from '../../lib/docs.server';

export function OnThisPage({ headings }: { headings: Heading[] }) {
  return (
    <nav aria-label="On this page">
      <p className="font-mono text-xs font-medium uppercase tracking-wide text-mute">On this page</p>
      <ul className="mt-3 space-y-1 border-l border-rule">
        {headings.map((heading) => (
          <li key={heading.id}>
            <a
              href={`#${heading.id}`}
              className={`-ml-px block border-l border-transparent py-1 text-sm text-mute transition hover:border-primary hover:text-text ${heading.depth === 3 ? 'pl-6' : 'pl-3'}`}
            >
              {heading.text}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}
