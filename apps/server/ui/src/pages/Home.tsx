// The home page: what Jeff is, and every function this server serves (GET /v1/functions), grouped by area, each
// with a link that opens it in the playground with its first example (GET /ui/examples).
import { useEffect, useState } from 'react';
import { ArrowRight } from 'lucide-react';
import { Badge, Button, EmptyState, Spinner } from '@jeff/ui';
import { calibrationState, shownError, type Api, type BaseFormat, type DecideCall, type Example, type FunctionListing,
  type ShownError } from '../api';
import { adapterOf, areaTitle, byArea } from '../areas';
import { ErrorAlert } from '../components/ErrorAlert';
import { Card, Page } from '../components/Page';

export interface TryFunction {
  functionId: string;
  call: DecideCall | null;
}

interface Loaded {
  baseFormat: BaseFormat;
  functions: FunctionListing[];
  firstExample: Map<string, Example>;
}

export function Home({ api, user, onTry }: { api: Api; user: string; onTry: (request: TryFunction) => void }) {
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [error, setError] = useState<ShownError | null>(null);

  useEffect(() => {
    let current = true;
    void (async () => {
      try {
        const [functions, examples] = await Promise.all([api.functions(), api.examples()]);
        const firstExample = new Map<string, Example>();
        for (const example of examples.examples) {
          if (!firstExample.has(example.call.function)) firstExample.set(example.call.function, example);
        }
        if (current) setLoaded({ baseFormat: functions.base_format, functions: functions.functions, firstExample });
      } catch (failure) {
        if (current) setError(shownError(failure));
      }
    })();
    return () => {
      current = false;
    };
  }, [api]);

  return (
    <Page
      eyebrow={`Signed in as ${user}`}
      title="Jeff Console"
      lead={
        <>
          Jeff turns a judgement into a function call: it reads a message, a record or a document and picks one of a fixed
          set of answers, with the probability of each, in a few milliseconds. Every function below is a small model
          trained for one such decision; call it with <span className="font-mono text-[0.9em]">POST /v1/decide</span>, or try
          it here in the playground.
        </>
      }
    >
      {error && <ErrorAlert error={error} />}
      {!error && !loaded && <div className="flex items-center gap-2 text-sm text-mute"><Spinner /> Loading the served functions</div>}
      {loaded && loaded.functions.length === 0 && (
        <EmptyState title="This server serves no functions" description="No adapter is switched on. Serve one from the Adapters page." />
      )}
      {loaded && byArea(loaded.functions).map(([area, functions]) => (
        <section key={area} className="flex flex-col gap-3" aria-label={areaTitle(area)}>
          <h2 className="m-0 text-[10.5px] font-bold uppercase tracking-[0.14em] text-mute">{areaTitle(area)}</h2>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
            {functions.map((listing) => {
              const example = loaded.firstExample.get(listing.id) ?? null;
              return (
                <Card key={listing.id} className="flex flex-col gap-3">
                  <div className="flex flex-col gap-1">
                    <p className="m-0 font-mono text-xs text-mute">{adapterOf(listing.id)}</p>
                    <h3 className="m-0 font-mono text-[15px] font-semibold text-text">{listing.id.slice(adapterOf(listing.id).length + 1)}</h3>
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    <Badge tone="neutral">{listing.type}</Badge>
                    <Badge tone="brand">{listing.interface}</Badge>
                    {listing.licence.kind === 'proprietary' ? <Badge tone="proprietary">proprietary</Badge> : <Badge tone="teal">{listing.licence.adapter}</Badge>}
                    <CalibrationBadge listing={listing} base={loaded.baseFormat} />
                  </div>
                  <p className="m-0 text-sm text-soft">{listing.input.description}</p>
                  <div className="mt-auto">
                    <Button variant="ghost" size="sm" onClick={() => onTry({ functionId: listing.id, call: example ? example.call : null })}
                      aria-label={`Try ${listing.id}`}>
                      {example ? 'Try it with an example' : 'Try it'}
                      <ArrowRight className="size-3.5" aria-hidden="true" />
                    </Button>
                  </div>
                </Card>
              );
            })}
          </div>
        </section>
      ))}
    </Page>
  );
}

// calibrated: the function has a threshold on the base this server serves (its INT4 base's own, on an INT4 base).
function CalibrationBadge({ listing, base }: { listing: FunctionListing; base: BaseFormat }) {
  const state = calibrationState(listing, base);
  if (state === 'calibrated') return <Badge tone="ok">calibrated</Badge>;
  if (state === 'missing') return <Badge tone="info">no threshold for this base</Badge>;
  return <Badge tone="empty">not calibrated</Badge>;
}
