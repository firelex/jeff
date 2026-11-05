// The access log (administrators only), ported from jeff-server's AccessLog.tsx: who signed in and from where,
// what pages a signed-in person opened, and visits that never sign in at all. GET /ui/access-log answers 403
// NotLicensed for anyone but a console session under a JEFF_ADMINS login, and 422 InvalidRequest when
// JEFF_ACCESS_LOG is not set on this server; both are shown with the server's own message.
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Globe, MapPin, RefreshCw, TriangleAlert, UserRound } from 'lucide-react';
import { Alert, Badge, type BadgeTone, Button, Spinner } from '@jeff/ui';
import { shownError, type Api, type ShownError } from '../api';
import { ErrorAlert } from '../components/ErrorAlert';
import { isRecord } from '../json';
import { Page } from '../components/Page';

const LIMIT = 1000;
const PRIVATE_PLACE = 'private network (tailnet or LAN)';

function required(record: Record<string, unknown>, key: string, where: string): unknown {
  if (!(key in record)) throw new Error(`${where} has no "${key}": ${JSON.stringify(record).slice(0, 200)}`);
  return record[key];
}

function asString(value: unknown, key: string, where: string): string {
  if (typeof value !== 'string') throw new Error(`${where}'s "${key}" is not a string: ${JSON.stringify(value)}`);
  return value;
}

function asNullableString(value: unknown, key: string, where: string): string | null {
  return value === null ? null : asString(value, key, where);
}

function asNumber(value: unknown, key: string, where: string): number {
  if (typeof value !== 'number') throw new Error(`${where}'s "${key}" is not a number: ${JSON.stringify(value)}`);
  return value;
}

function asNullableNumber(value: unknown, key: string, where: string): number | null {
  return value === null ? null : asNumber(value, key, where);
}

function asCountMap(value: unknown, key: string, where: string): Record<string, number> {
  if (!isRecord(value)) throw new Error(`${where}'s "${key}" is not an object: ${JSON.stringify(value)}`);
  for (const [name, count] of Object.entries(value)) {
    if (typeof count !== 'number') throw new Error(`${where}'s "${key}.${name}" is not a number: ${JSON.stringify(count)}`);
  }
  return value as Record<string, number>;
}

type EventKind = 'console' | 'signin' | 'signin-failed' | 'signin-locked' | 'view' | 'api';

interface Entry {
  time: number;
  event: EventKind;
  user: string | null;
  ip: string;
  peer: string | null;
  forwarded: string | null;
  location: string | null;
  path: string;
  status: number | null;
  server: string;
  detail: string | null;
  agent: string;
}

/** jeff_server.access_log.AccessLog.record()'s own fields, read defensively: a field this page does not
 * recognise fails loudly here rather than showing a row with blanks nobody can explain. */
function asEntry(record: Record<string, unknown>): Entry {
  const where = 'An access log entry';
  return {
    time: asNumber(required(record, 'time', where), 'time', where),
    event: asEventKind(asString(required(record, 'event', where), 'event', where)),
    user: asNullableString(required(record, 'user', where), 'user', where),
    ip: asString(required(record, 'ip', where), 'ip', where),
    peer: asNullableString(required(record, 'peer', where), 'peer', where),
    forwarded: asNullableString(required(record, 'forwarded', where), 'forwarded', where),
    location: asNullableString(required(record, 'location', where), 'location', where),
    path: asString(required(record, 'path', where), 'path', where),
    status: asNullableNumber(required(record, 'status', where), 'status', where),
    server: asString(required(record, 'server', where), 'server', where),
    detail: asNullableString(required(record, 'detail', where), 'detail', where),
    agent: asString(required(record, 'agent', where), 'agent', where),
  };
}

/**
 * jeff-server's AccessLog.tsx tooltip ("connection from <peer>"), extended with the whole X-Forwarded-For header
 * when the server read one: `ip` is client_address()'s trusted result (the header's last hop, or the connection's
 * own address with no header), `peer` is always the connection's own address, so this is what to double-check
 * when they differ -- and `forwarded` is the raw evidence for that trust decision.
 */
function addressTooltip(entry: Entry): string | undefined {
  if (!entry.peer) return undefined;
  const base = `connection from ${entry.peer}`;
  return entry.forwarded ? `${base}; X-Forwarded-For: ${entry.forwarded}` : base;
}

interface Person {
  user: string;
  last: number;
  signIns: number;
  failedSignIns: number;
  ips: Record<string, number>;
  locations: Record<string, number>;
}

function asPerson(record: Record<string, unknown>): Person {
  const where = 'An access log person';
  return {
    user: asString(required(record, 'user', where), 'user', where),
    last: asNumber(required(record, 'last', where), 'last', where),
    signIns: asNumber(required(record, 'sign_ins', where), 'sign_ins', where),
    failedSignIns: asNumber(required(record, 'failed_sign_ins', where), 'failed_sign_ins', where),
    ips: asCountMap(required(record, 'ips', where), 'ips', where),
    locations: asCountMap(required(record, 'locations', where), 'locations', where),
  };
}

const EVENT_LABEL: Record<EventKind, string> = {
  console: 'Opened the console', signin: 'Signed in', 'signin-failed': 'Wrong password',
  'signin-locked': 'Sign-in locked', view: 'Opened a page', api: 'API call',
};
const EVENT_TONE: Record<EventKind, BadgeTone> = {
  console: 'info', signin: 'ok', 'signin-failed': 'empty', 'signin-locked': 'empty', view: 'neutral', api: 'neutral',
};

function asEventKind(event: string): EventKind {
  if (!Object.hasOwn(EVENT_LABEL, event)) {
    throw new Error(`An access log entry has the event "${event}", which this page does not know (it knows ${Object.keys(EVENT_LABEL).join(', ')})`);
  }
  return event as EventKind;
}

const when = (seconds: number) => new Date(seconds * 1000).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });

/** Places a login was used from, leaving out this deployment's own network, so one person on two devices at home
 * is not shown as a spread login. */
function publicPlaces(person: Person): string[] {
  return Object.keys(person.locations).filter((place) => place !== PRIVATE_PLACE);
}

export function AccessLog({ api }: { api: Api }) {
  const [entries, setEntries] = useState<Entry[] | null>(null);
  const [people, setPeople] = useState<Person[] | null>(null);
  const [total, setTotal] = useState(0);
  const [anonymousVisits, setAnonymousVisits] = useState(0);
  const [anonymousIps, setAnonymousIps] = useState<string[]>([]);
  const [locationsOn, setLocationsOn] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<ShownError | null>(null);
  const [who, setWho] = useState('all');
  const [showApi, setShowApi] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const report = await api.accessLog(LIMIT);
      setEntries(report.entries.map(asEntry));
      setPeople(report.people.map(asPerson));
      setTotal(report.total);
      setAnonymousVisits(report.anonymous_visits);
      setAnonymousIps(report.anonymous_ips);
      setLocationsOn(report.locations_on);
      setError(null);
    } catch (failure) {
      setEntries(null);
      setPeople(null);
      setError(shownError(failure));
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  const shown = useMemo(
    () =>
      (entries ?? []).filter(
        (entry) => (showApi || entry.event !== 'api') && (who === 'all' || (who === 'anonymous' ? entry.user === null : entry.user === who)),
      ),
    [entries, who, showApi],
  );

  return (
    <Page
      eyebrow="Administrators only"
      title="Access log"
      lead="Every sign-in, every page a signed-in person opens, and every visit to the console, with the address it came from. A login used from several places, or visits that never sign in, suggest the link or the login was passed on."
      actions={
        <Button variant="ghost" size="sm" onClick={() => void load()} disabled={loading}>
          <RefreshCw className={loading ? 'size-3.5 animate-spin' : 'size-3.5'} aria-hidden="true" />
          Refresh
        </Button>
      }
    >
      {error && <ErrorAlert error={error} />}
      {!error && !entries && (
        <div className="flex items-center gap-2 text-sm text-mute">
          <Spinner /> Loading the access log
        </div>
      )}
      {entries && people && (
        <>
          {!locationsOn && (
            <Alert tone="info" title="Locations are off">
              Set JEFF_GEOIP_DB to an IP location database to see a city and country for each address.
            </Alert>
          )}

          <section className="flex flex-col gap-3">
            <h2 className="m-0 text-[10.5px] font-bold uppercase tracking-[0.14em] text-mute">People</h2>
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
              {people.map((person) => {
                const places = publicPlaces(person);
                const ips = Object.keys(person.ips);
                const spread = places.length > 1;
                const active = who === person.user;
                return (
                  <button
                    key={person.user}
                    type="button"
                    onClick={() => setWho(active ? 'all' : person.user)}
                    className={[
                      'flex flex-col gap-2 rounded-card border bg-panel p-4 text-left shadow-card',
                      spread ? 'border-warning' : 'border-line',
                      active ? 'ring-2 ring-primary' : '',
                    ].join(' ')}
                  >
                    <div className="flex items-center gap-2">
                      <UserRound className="size-4 shrink-0 text-mute" aria-hidden="true" />
                      <span className="min-w-0 flex-1 truncate text-sm font-semibold text-text">{person.user}</span>
                      {spread && (
                        <Badge tone="empty">
                          <TriangleAlert className="mr-1 inline size-3" aria-hidden="true" />
                          {places.length} places
                        </Badge>
                      )}
                    </div>
                    <p className="m-0 text-xs text-mute">
                      {person.signIns} sign-ins{person.failedSignIns ? `, ${person.failedSignIns} failed` : ''} ·{' '}
                      {ips.length} {ips.length === 1 ? 'address' : 'addresses'} · last seen {when(person.last)}
                    </p>
                    <ul className="m-0 flex flex-col gap-1 pl-0 text-sm text-text">
                      {Object.entries(person.locations)
                        .sort((a, b) => b[1] - a[1])
                        .map(([place, count]) => (
                          <li key={place} className="flex items-center gap-1.5">
                            <MapPin className="size-3.5 shrink-0 text-mute" aria-hidden="true" />
                            <span className="min-w-0 flex-1 truncate">{place}</span>
                            <span className="text-xs text-mute">{count}</span>
                          </li>
                        ))}
                    </ul>
                  </button>
                );
              })}
              <button
                type="button"
                onClick={() => setWho(who === 'anonymous' ? 'all' : 'anonymous')}
                className={[
                  'flex flex-col gap-2 rounded-card border border-dashed bg-panel p-4 text-left',
                  anonymousIps.length > 0 ? 'border-primary-border' : 'border-line',
                  who === 'anonymous' ? 'ring-2 ring-primary' : '',
                ].join(' ')}
              >
                <div className="flex items-center gap-2">
                  <Globe className="size-4 shrink-0 text-mute" aria-hidden="true" />
                  <span className="text-sm font-semibold text-text">Visits without a sign-in</span>
                </div>
                <p className="m-0 text-xs text-mute">
                  {anonymousVisits} console visits from {anonymousIps.length} {anonymousIps.length === 1 ? 'address' : 'addresses'}.
                  Every visit starts here; addresses that never sign in afterwards opened the link without a login.
                </p>
              </button>
            </div>
          </section>

          <section className="flex flex-col gap-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <h2 className="m-0 text-[10.5px] font-bold uppercase tracking-[0.14em] text-mute">
                Events {who !== 'all' && <span className="font-normal text-mute">· {who === 'anonymous' ? 'without a sign-in' : who}</span>}
              </h2>
              <div className="flex items-center gap-2">
                {who !== 'all' && (
                  <Button variant="ghost" size="sm" onClick={() => setWho('all')}>
                    Show everyone
                  </Button>
                )}
                <Button variant="ghost" size="sm" onClick={() => setShowApi(!showApi)}>
                  {showApi ? 'Hide API calls' : 'Show API calls'}
                </Button>
              </div>
            </div>
            <div className="overflow-x-auto rounded-card border border-line bg-panel shadow-card">
              <table className="w-full text-sm">
                <thead className="border-b border-line bg-background text-left text-xs text-mute">
                  <tr>
                    {['Time', 'Who', 'What', 'Address', 'Location', 'Server', 'Browser'].map((title) => (
                      <th key={title} className="px-4 py-2 font-medium">{title}</th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {shown.length === 0 && (
                    <tr>
                      <td colSpan={7} className="px-4 py-6 text-center text-mute">Nothing logged yet.</td>
                    </tr>
                  )}
                  {shown.map((entry, index) => (
                    <tr key={`${entry.time}-${index}`} className="align-top">
                      <td className="px-4 py-2 whitespace-nowrap text-mute">{when(entry.time)}</td>
                      <td className="px-4 py-2 whitespace-nowrap">{entry.user ?? <span className="text-mute">not signed in</span>}</td>
                      <td className="px-4 py-2 whitespace-nowrap">
                        <Badge tone={EVENT_TONE[entry.event]}>{EVENT_LABEL[entry.event]}</Badge>
                        {entry.detail && (
                          <span className="ml-2 font-mono text-xs text-mute">{entry.event === 'api' ? `${entry.detail} ${entry.path}` : entry.detail}</span>
                        )}
                      </td>
                      <td className="px-4 py-2 font-mono text-xs whitespace-nowrap" title={addressTooltip(entry)}>{entry.ip}</td>
                      <td className="px-4 py-2">{entry.location ?? '—'}</td>
                      <td className="px-4 py-2 font-mono text-xs whitespace-nowrap text-mute">{entry.server}</td>
                      <td className="max-w-72 truncate px-4 py-2 text-xs text-mute" title={entry.agent}>{entry.agent}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="m-0 text-xs text-mute">
              {total} entries in all; the newest {entries.length} are loaded.
              {locationsOn && (
                <>
                  {' '}IP geolocation by <a className="underline" href="https://db-ip.com" target="_blank" rel="noreferrer">DB-IP</a>.
                </>
              )}
            </p>
          </section>
        </>
      )}
    </Page>
  );
}
