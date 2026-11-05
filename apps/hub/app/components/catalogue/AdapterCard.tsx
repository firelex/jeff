// One catalogue list card (under its topic's h2, so the title is an h3): title, id, summary and the adapter's
// interfaces, with the usual ink border on the panel. Only the owner's view (`ownerView`: the owner is signed in,
// lib/owner-view.ts) tells open and API-only apart (owner, 4 November 2026): an open adapter's card then has a 2 px
// green border on a very light green fill (app.css: --open, --open-fill) and a green Open tag, an API-only one the
// purple API only tag; the tag says which in words as well, so the colour is never the only sign.
import { Link } from 'react-router';
import type { CatalogueEntry } from '../../lib/catalogue.server';
import { CARD_LIFT } from '../styles';
import { Tag } from '../Tag';

export function AdapterCard({ entry, ownerView }: { entry: CatalogueEntry; ownerView: boolean }) {
  const interfaces = [...new Set(entry.functions.map((fn) => fn.interface))].sort();
  return (
    <Link
      to={`/catalogue/${entry.id}`}
      className={`flex flex-col gap-2 p-4 ${ownerView && entry.kind === 'open' ? 'border-2 border-open bg-open-fill' : 'border border-ink bg-panel'} ${CARD_LIFT}`}
    >
      {/* The badge wraps under the title when a long word of it (COMMUNICATIONS at 20 px) leaves it no room. */}
      <div className="flex flex-wrap items-start justify-between gap-2">
        <h3 className="font-pixel text-pixel-20 text-text">{entry.title}</h3>
        {ownerView && <Tag tone={entry.kind === 'open' ? 'open' : 'accent'}>{entry.kind === 'open' ? 'Open' : 'API only'}</Tag>}
      </div>
      <p className="font-mono text-xs text-mute">{entry.name}</p>
      <p className="text-sm leading-6 text-soft">{entry.summary}</p>
      <div className="mt-auto flex flex-wrap gap-1.5 pt-2">
        {interfaces.map((i) => (
          <Tag key={i} tone="quiet">{i}</Tag>
        ))}
      </div>
    </Link>
  );
}
