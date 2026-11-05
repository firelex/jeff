// The Adapters page: every catalogue adapter trained for the base the engines serve (GET /v1/adapters), served or
// not, grouped by area, each with its one-line description and its functions -- a served one's from GET
// /v1/functions, with their question types and its licence; an unserved one's from GET /v1/adapters, with their
// interfaces (GET /v1/functions lists only what is served, so its licence is unknown until it is switched on). The served set is owner-only (PUT /v1/adapters/served): the API key
// or a console session may change it; any other caller (a hub token, or an "open" server with no credentials at
// all) sees the switches disabled with a note, and a refusal -- 403 NotLicensed from a hub token, 422 InvalidRequest
// on a public server -- is shown with the server's own message, never swallowed.
import { useCallback, useEffect, useRef, useState } from 'react';
import { Boxes } from 'lucide-react';
import { Badge, EmptyState, Spinner, Switch } from '@jeff/ui';
import { shownError, type Api, type AdapterEntry, type FunctionListing, type Me, type ShownError } from '../api';
import { adapterArea, adapterOf, areaTitle } from '../areas';
import { ErrorAlert } from '../components/ErrorAlert';
import { Card, Page } from '../components/Page';

function byAdapterArea(adapters: AdapterEntry[]): [string, AdapterEntry[]][] {
  const groups = new Map<string, AdapterEntry[]>();
  for (const adapter of [...adapters].sort((a, b) => a.id.localeCompare(b.id))) {
    const area = adapterArea(adapter.id);
    groups.set(area, [...(groups.get(area) ?? []), adapter]);
  }
  return [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]));
}

/**
 * Whether this caller may change the served set. Matches jeff_server.operations.owner: the API key, a console
 * session, and an "open" caller (no credentials configured on this server at all, so everyone is the owner) may
 * all change it; only a hub token -- a customer's -- is refused (403 NotLicensed).
 */
export function isOwner(me: Me | null): boolean {
  return me !== null && me.kind !== 'hub';
}

interface Loaded {
  adapters: AdapterEntry[];
  adaptersVersion: number;
  functions: FunctionListing[];
}

export function Adapters({ api, me }: { api: Api; me: Me | null }) {
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [loadError, setLoadError] = useState<ShownError | null>(null);
  const [pending, setPending] = useState<ReadonlySet<string>>(new Set());
  const [switchError, setSwitchError] = useState<ShownError | null>(null);
  // The served set as the server last reported it (GET /v1/adapters or a PUT's answer). Each change is built from
  // it, and changes run one after another, so a second switch flipped before the first change has answered still
  // keeps the first change rather than sending a served set from before it.
  const served = useRef<Set<string> | null>(null);
  const changes = useRef<Promise<void>>(Promise.resolve());

  const load = useCallback(async () => {
    try {
      const [list, listing] = await Promise.all([api.adapters(), api.functions()]);
      served.current = new Set(list.adapters.filter((entry) => entry.served).map((entry) => entry.id));
      setLoaded({ adapters: list.adapters, adaptersVersion: list.adapters_version, functions: listing.functions });
      setLoadError(null);
    } catch (failure) {
      setLoaded(null);
      setLoadError(shownError(failure));
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  const owner = isOwner(me);

  const change = async (adapter: AdapterEntry, serve: boolean) => {
    try {
      if (served.current === null) throw new Error('The served set is not known yet: GET /v1/adapters has not answered');
      const next = new Set(served.current);
      if (serve) next.add(adapter.id);
      else next.delete(adapter.id);
      const answer = await api.setServed([...next]);
      served.current = new Set(answer.adapters.filter((entry) => entry.served).map((entry) => entry.id));
      await load();
    } catch (failure) {
      setSwitchError(shownError(failure));
    } finally {
      setPending((ids) => {
        const rest = new Set(ids);
        rest.delete(adapter.id);
        return rest;
      });
    }
  };

  const toggle = (adapter: AdapterEntry, serve: boolean) => {
    setPending((ids) => new Set(ids).add(adapter.id));
    setSwitchError(null);
    changes.current = changes.current.then(() => change(adapter, serve));
  };

  return (
    <Page
      eyebrow="Manage"
      title="Adapters"
      lead="Every adapter in the catalogue trained for the base this server runs, served or not. A served adapter answers its functions; switching one on or off changes every engine at once, with no restart."
    >
      {me === null && <p className="m-0 text-sm text-mute">Checking who is signed in.</p>}
      {me !== null && !owner && (
        <p className="m-0 text-sm text-mute">
          Signed in as {me.kind}: only the server&apos;s owner (its API key or a console session) can change which
          adapters are served.
        </p>
      )}
      {switchError && <ErrorAlert error={switchError} />}
      {loadError && <ErrorAlert error={loadError} />}
      {!loadError && !loaded && (
        <div className="flex items-center gap-2 text-sm text-mute">
          <Spinner /> Loading the adapter catalogue
        </div>
      )}
      {loaded && loaded.adapters.length === 0 && (
        <EmptyState title="The catalogue has no adapters" description="Nothing to serve." />
      )}
      {loaded && (
        <p className="m-0 text-xs text-mute">adapters_version {loaded.adaptersVersion} -- the engines&apos; own change counter.</p>
      )}
      {loaded &&
        byAdapterArea(loaded.adapters).map(([area, adapters]) => (
          <section key={area} className="flex flex-col gap-3" aria-label={areaTitle(area)}>
            <h2 className="m-0 text-[10.5px] font-bold uppercase tracking-[0.14em] text-mute">{areaTitle(area)}</h2>
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
              {adapters.map((adapter) => {
                const functions = loaded.functions.filter((listing) => adapterOf(listing.id) === adapter.id);
                const licence = functions[0]?.licence ?? null;
                return (
                  <Card key={adapter.id} className="flex flex-col gap-3">
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex min-w-0 flex-col gap-1">
                        <p className="m-0 flex items-center gap-1.5 font-mono text-[15px] font-semibold text-text">
                          <Boxes className="size-3.5 shrink-0 text-mute" aria-hidden="true" />
                          <span className="min-w-0 truncate">{adapter.id}</span>
                        </p>
                        <p className="m-0 font-mono text-xs text-mute">version {adapter.version}</p>
                        <p className="m-0 text-sm text-soft">{adapter.description}</p>
                      </div>
                      <Switch
                        checked={adapter.served}
                        disabled={!owner || pending.has(adapter.id)}
                        onChange={(checked) => toggle(adapter, checked)}
                        aria-label={`Serve ${adapter.id}`}
                      />
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      <Badge tone={adapter.served ? 'ok' : 'empty'}>{adapter.served ? 'served' : 'not served'}</Badge>
                      {licence ? (
                        licence.kind === 'proprietary' ? (
                          <Badge tone="proprietary">proprietary</Badge>
                        ) : (
                          <Badge tone="teal">{licence.adapter}</Badge>
                        )
                      ) : (
                        <Badge tone="empty">licence unknown</Badge>
                      )}
                    </div>
                    {functions.length > 0 ? (
                      <ul className="m-0 flex flex-col gap-1 pl-0 text-sm text-soft">
                        {functions.map((listing) => (
                          <li key={listing.id} className="flex items-center gap-1.5">
                            <span className="font-mono text-xs text-text">{listing.id.slice(adapter.id.length + 1)}</span>
                            <Badge tone="neutral">{listing.type}</Badge>
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <ul className="m-0 flex flex-col gap-1 pl-0 text-sm text-soft">
                        {adapter.functions.map((entry) => (
                          <li key={entry.id} className="flex items-center gap-1.5">
                            <span className="font-mono text-xs text-text">{entry.id.slice(adapter.id.length + 1)}</span>
                            <Badge tone="neutral">{entry.interface}</Badge>
                          </li>
                        ))}
                      </ul>
                    )}
                  </Card>
                );
              })}
            </div>
          </section>
        ))}
    </Page>
  );
}
