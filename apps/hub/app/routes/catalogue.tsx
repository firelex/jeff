// The catalogue list: every adapter the hub lists, searchable and filterable by interface and domain. The
// loader returns every entry (ssr:false prerenders this page once, with no query string); filtering runs in the
// browser from the URL's query, so /catalogue?domain=safety filters correctly once the prerendered page's JS runs,
// whatever query string the static page was requested with. The first client render ignores the query, so it matches
// the prerendered (unfiltered) markup exactly and React hydrates without a mismatch; the filter applies right after. The filtered adapters are shown under their topics
// (lib/topics.ts); a topic the filter leaves empty is not shown.
import { useEffect, useMemo, useState } from 'react';
import { Link, useLoaderData, useSearchParams } from 'react-router';
import { AdapterCard } from '../components/catalogue/AdapterCard';
import { Filters, filterEntries, type CatalogueFilter } from '../components/catalogue/Filters';
import { catalogue } from '../lib/site.server';
import { useOwnerView } from '../lib/owner-view';
import { groupByTopic } from '../lib/topics';
import { PAGE_TITLE, SECTION_HEADING, TEXT_LINK } from '../components/styles';

export function loader() {
  const entries = catalogue();
  // Fails the build, naming the adapter, if an adapter's domain has no topic.
  groupByTopic(entries);
  return { entries };
}

export function meta() {
  return [{ title: 'Catalogue | Jeff' }];
}

export default function Catalogue() {
  const { entries } = useLoaderData<typeof loader>();
  const [urlParams, setParams] = useSearchParams();
  const [hydrated, setHydrated] = useState(false);
  // The owner sees which adapters are open (AdapterCard); everyone else sees every card alike.
  const ownerView = useOwnerView();
  useEffect(() => setHydrated(true), []);
  const params = hydrated ? urlParams : new URLSearchParams();
  const filter: CatalogueFilter = {
    query: params.get('q') ?? '',
    interface: params.get('interface'),
    domain: params.get('domain'),
  };
  const filtered = useMemo(() => filterEntries(entries, filter), [entries, filter.query, filter.interface, filter.domain]);
  const groups = useMemo(() => groupByTopic(filtered), [filtered]);

  const onChange = (next: CatalogueFilter) => {
    const sp = new URLSearchParams();
    if (next.query !== '') sp.set('q', next.query);
    if (next.interface !== null) sp.set('interface', next.interface);
    if (next.domain !== null) sp.set('domain', next.domain);
    setParams(sp);
  };

  return (
    <main id="main" className="mx-auto max-w-7xl px-4 py-10">
      <h1 className={PAGE_TITLE}>Catalogue</h1>
      <p className="mt-2 max-w-[72ch] text-sm leading-7 text-soft">
        Every adapter is open: a trained model you can call through the API, or download and run yourself.
      </p>
      <p className="mt-2 max-w-[72ch] text-sm leading-7 text-soft">
        Need a decision that is not here? <Link to="/request-adapter" className={TEXT_LINK}>Request an adapter</Link>.
      </p>
      <div className="mt-6">
        <Filters entries={entries} filter={filter} onChange={onChange} />
      </div>
      <p className="mt-4 font-mono text-xs text-mute">
        {filtered.length} adapter{filtered.length === 1 ? '' : 's'}
      </p>
      {groups.map(({ topic, entries: inTopic }) => (
        <section key={topic.id} className="mt-10" aria-labelledby={`topic-${topic.id}`}>
          <h2 id={`topic-${topic.id}`} className={`border-b border-ink pb-2 ${SECTION_HEADING}`}>{topic.heading}</h2>
          <p className="mt-2 max-w-[72ch] text-sm leading-6 text-soft">{topic.description}</p>
          {/* Three columns of whole pixels (the first two rounded down, the third taking the rest), so the cards' pixel-font titles
              start on whole pixels; a plain third of the width would put every second and third column on a fraction. */}
          <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-[repeat(2,round(down,calc((100%_-_2rem)/3),1px))_minmax(0,1fr)]">
            {inTopic.map((entry) => (
              <AdapterCard key={entry.id} entry={entry} ownerView={ownerView} />
            ))}
          </div>
        </section>
      ))}
    </main>
  );
}
