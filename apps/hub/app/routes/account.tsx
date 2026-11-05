// /account: the signed-in customer's account (spec Section 7). Prerendered like every page of the hub, so the
// shipped HTML holds no account: in the browser the page asks the hub's function for it (GET /api/account) and acts
// through the account endpoints (server/endpoints/account.ts), each a same-origin POST:
// - an account that may not use keys sees the one subscribe screen (Checkout);
// - a paying one sees its subscription, this period's usage against the allowance, its keys (create, shown once;
//   revoke), "Manage billing" (the Customer Portal);
// - both see their adapter requests (components/account/AdapterRequests.tsx, GET /api/adapter-requests);
// - both can sign out, sign out everywhere and delete the account.
// A visitor who is not signed in goes to /signin. A refusal the customer can act on is shown in the hub's own words;
// any other failure is shown as what it was (the status the hub answered, or why the request failed).
import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from 'react';
import { flushSync } from 'react-dom';
import { useNavigate } from 'react-router';
import { AdapterRequests } from '../components/account/AdapterRequests';
import { CreatedKey } from '../components/account/CreatedKey';
import { KeysTable } from '../components/account/KeysTable';
import { UsageBar } from '../components/account/UsageBar';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { BUTTON_PRIMARY, BUTTON_SECONDARY, CARD, PAGE_TITLE, SECTION_HEADING } from '../components/styles';
import type { AccountKey, AccountPage, CreatedKeyAnswer, DoneAnswer, PlaygroundUsage } from '../lib/account-page';
import { askHub, refusalOf, sentence } from '../lib/ask-hub';
import { authClient } from '../lib/auth-client';
import { longDate } from '../lib/dates';
import { MAX_LIVE_KEYS, MAX_NAME_LENGTH } from '../lib/keys-format';

export const LOADING = 'Loading your account…';
export { REQUEST_TIMEOUT_MS } from '../lib/ask-hub';
// The word the deletion box asks for (server/endpoints/account.ts: DELETE_CONFIRMATION).
const DELETE_WORD = 'delete';

export function meta() {
  return [{ title: 'Your account | Jeff' }];
}

function isAccountPage(body: unknown): body is AccountPage {
  const view = (body as { view?: unknown } | null)?.view;
  return view === 'subscribe' || view === 'account';
}

type State = { kind: 'loading' } | { kind: 'failed'; message: string } | { kind: 'ready'; page: AccountPage };
type Notice = { kind: 'refused' | 'done'; text: string } | null;

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className={`mt-8 p-5 sm:p-6 ${CARD}`} aria-label={title}>
      <h2 className={SECTION_HEADING}>{title}</h2>
      {children}
    </section>
  );
}

function NoticeLine({ notice }: { notice: Notice }) {
  if (notice === null) return null;
  return notice.kind === 'refused'
    ? <p role="alert" className="mt-4 text-sm text-danger">{notice.text}</p>
    : <p role="status" className="mt-4 text-sm text-soft">{notice.text}</p>;
}

function PlaygroundLine({ playground }: { playground: PlaygroundUsage }) {
  if (playground === null) return null;
  return <p className="mt-2 text-sm text-soft">{`Playground since ${longDate(playground.since)}: ${playground.used.toLocaleString('en-US')} input tokens.`}</p>;
}

// Something the page did not expect (a bug): said on the page, then thrown for the browser's console.
function unexpected(error: unknown): string {
  return `Something went wrong on this page: ${error instanceof Error ? error.message : String(error)}. Reload the page.`;
}

function subscriptionLine(page: Extract<AccountPage, { view: 'account' }>): string {
  const ends = longDate(page.periodEnd);
  if (page.cancelAtPeriodEnd) return `Active until ${ends}; it will not renew.`;
  if (page.status === 'past_due') return `Payment failed; Stripe is retrying. Your keys keep working meanwhile. Next billing date: ${ends}.`;
  return `Active. Next billing date: ${ends}.`;
}

export default function Account() {
  const navigate = useNavigate();
  const [state, setState] = useState<State>({ kind: 'loading' });
  const [busy, setBusy] = useState(false);
  // One notice per section, so each refusal shows next to the button that caused it.
  const [billing, setBilling] = useState<Notice>(null);
  const [keys, setKeys] = useState<Notice>(null);
  const [session, setSession] = useState<Notice>(null);
  const [deletion, setDeletion] = useState<Notice>(null);
  const [created, setCreated] = useState<CreatedKeyAnswer['created'] | null>(null);
  const [keyName, setKeyName] = useState('');
  const [confirm, setConfirm] = useState('');
  // The key a revoke was asked for, while the dialog asking to confirm it is open (null: no revoke is pending).
  const [revoking, setRevoking] = useState<AccountKey | null>(null);

  // The account, or why it could not be had (null: the visitor went to /signin).
  const fetchPage = useCallback(async (): Promise<{ page: AccountPage } | { failed: string } | null> => {
    const answer = await askHub('/api/account', null);
    if (answer.kind === 'answer' && answer.status === 401) { navigate('/signin', { replace: true }); return null; }
    if (answer.kind === 'answer' && answer.status === 200 && isAccountPage(answer.body)) return { page: answer.body };
    return { failed: answer.kind === 'answer' && answer.status === 200 ? 'the hub\'s answer was not an account. Reload the page.'
      : refusalOf(answer) };
  }, [navigate]);

  useEffect(() => {
    let live = true;
    fetchPage().then((got) => {
      if (!live || got === null) return;
      setState('page' in got ? { kind: 'ready', page: got.page } : { kind: 'failed', message: `Could not load your account. ${sentence(got.failed)}` });
    }, (error: unknown) => {
      if (live) setState({ kind: 'failed', message: unexpected(error) });
      throw error;
    });
    return () => { live = false; };
  }, [fetchPage]);

  // The account again, after an action: the page already shown stays (with a new key, if one was made). Returns why
  // it could not be had, or null (refreshed, or the visitor went to /signin).
  const reload = async (): Promise<string | null> => {
    const got = await fetchPage();
    if (got === null) return null;
    if ('page' in got) { setState({ kind: 'ready', page: got.page }); return null; }
    return `The list of keys could not be refreshed. ${sentence(got.failed)} Reload the page to see it.`;
  };

  // After a create or a revoke: a failed refresh is said next to the keys.
  const refresh = async () => {
    const failed = await reload();
    if (failed !== null) setKeys({ kind: 'refused', text: failed });
  };

  // The new key is dropped when the page is left (so the back-forward cache keeps no copy) and before going to Stripe.
  useEffect(() => {
    const drop = () => { flushSync(() => setCreated(null)); };
    window.addEventListener('pagehide', drop);
    return () => window.removeEventListener('pagehide', drop);
  }, []);

  // One POST at a time; a 401 (the session ended elsewhere) goes to /signin. Anything unexpected is said in the
  // section, then thrown. `onUnknown`, for an action that may have happened even though no answer came (the time
  // limit ran out, the connection dropped): given why, it says what may have happened instead of the plain refusal.
  const act = async (path: string, body: unknown, show: (notice: Notice) => void, onDone: (answer: unknown) => Promise<void> | void,
    onUnknown?: (reason: string) => Promise<void>) => {
    setBusy(true);
    show(null);
    try {
      const answer = await askHub(path, { body });
      if (answer.kind === 'answer' && answer.status === 401) { navigate('/signin', { replace: true }); return; }
      if (answer.kind === 'answer' && answer.status === 200) { await onDone(answer.body); return; }
      if (answer.kind === 'failed' && onUnknown !== undefined) { await onUnknown(answer.reason); return; }
      show({ kind: 'refused', text: refusalOf(answer) });
    } catch (error) {
      show({ kind: 'refused', text: unexpected(error) });
      throw error;
    } finally {
      setBusy(false);
    }
  };

  // Checkout and the portal answer {"url"}: the Stripe page to go to.
  const toStripe = (path: string, show: (notice: Notice) => void) =>
    act(path, undefined, show, (body) => {
      const url = (body as { url?: unknown } | null)?.url;
      if (typeof url !== 'string') {
        show({ kind: 'refused', text: 'Something went wrong: the hub\'s answer had no Stripe address to go to. Try again.' });
        return;
      }
      flushSync(() => setCreated(null));
      window.location.assign(url);
    });

  const createKey = (event: FormEvent) => {
    event.preventDefault();
    setCreated(null);
    void act('/api/account/create-key', { name: keyName }, setKeys, async (body) => {
      setCreated((body as CreatedKeyAnswer).created);
      setKeyName('');
      await refresh();
    }, async (reason) => {
      // The hub may have made the key without its answer arriving: the list shows it if so, and its secret was never
      // shown, so it can only be revoked.
      const failed = await reload();
      const said = `Something went wrong: ${reason}, so the key may have been made. If it appears in the list, revoke it: its secret was never shown.`;
      setKeys({ kind: 'refused', text: failed === null ? said : `${said} ${failed}` });
    });
  };

  // Revoking asks first (ConfirmDialog, below), since a revoked key cannot be brought back.
  const confirmRevoke = () => {
    const key = revoking;
    if (key === null) return;
    setRevoking(null);
    setCreated(null);
    void act('/api/account/revoke-key', { key: key.id }, setKeys, async (body) => {
      setKeys({ kind: 'done', text: (body as DoneAnswer).done });
      await refresh();
    });
  };

  const signOut = async () => {
    setBusy(true);
    setSession(null);
    const { error } = await authClient().signOut();
    setBusy(false);
    if (error !== null) { setSession({ kind: 'refused', text: error.message ?? `Could not sign out (${error.status} ${error.statusText}).` }); return; }
    navigate('/', { replace: true });
  };

  const signOutEverywhere = () =>
    void act('/api/account/sign-out-everywhere', undefined, setSession, () => { navigate('/signin', { replace: true }); });

  const deleteAccount = (event: FormEvent) => {
    event.preventDefault();
    void act('/api/account/delete', { confirm }, setDeletion, () => { navigate('/', { replace: true }); }, async (reason) => {
      // The hub may have deleted the account without its answer arriving. Asking for the account again finds out
      // when it has (the session went with it, so the page goes to /signin); otherwise the deletion may still be
      // under way.
      const said = `Something went wrong: ${reason}, so the account may have been deleted. Reload the page in a moment: if you are signed out, it was.`;
      const got = await fetchPage();
      if (got === null) return;
      if ('page' in got) { setState({ kind: 'ready', page: got.page }); setDeletion({ kind: 'refused', text: said }); return; }
      setDeletion({ kind: 'refused', text: `${said} Checking the account again failed too: ${sentence(got.failed)}` });
    });
  };

  if (state.kind !== 'ready') {
    return (
      <main id="main" className="mx-auto max-w-3xl px-4 py-12 sm:py-16">
        <h1 className={PAGE_TITLE}>Your account</h1>
        {state.kind === 'loading'
          ? <p role="status" className="mt-4 text-sm text-soft">{LOADING}</p>
          : <p role="alert" className="mt-4 text-sm text-danger">{state.message}</p>}
        {created !== null && <CreatedKey name={created.name} keyText={created.key} />}
      </main>
    );
  }
  const { page } = state;

  const leaving = (
    <>
      <AdapterRequests />
      <Section title="Sign out">
        <p className="mt-3 text-sm text-soft">Sign out here, or end every session of this account on every device.</p>
        <div className="mt-4 flex flex-wrap gap-3">
          <button type="button" disabled={busy} className={`${BUTTON_SECONDARY} px-4 py-2`} onClick={() => void signOut()}>Sign out</button>
          <button type="button" disabled={busy} className={`${BUTTON_SECONDARY} px-4 py-2`} onClick={signOutEverywhere}>Sign out everywhere</button>
        </div>
        <NoticeLine notice={session} />
      </Section>
      <Section title="Delete account">
        <p className="mt-3 text-sm text-soft">
          Deleting your account cancels your subscription and deletes your keys. Your invoices stay with Stripe. This
          cannot be undone.
        </p>
        <label htmlFor="delete-confirm" className="mt-4 block font-mono text-sm text-soft">{`Type ${DELETE_WORD} to confirm`}</label>
        <form className="mt-2 flex flex-col gap-3 sm:flex-row" onSubmit={deleteAccount}>
          <input id="delete-confirm" value={confirm} onChange={(e) => setConfirm(e.target.value)}
            placeholder={DELETE_WORD} autoComplete="off"
            className="min-w-0 border border-ink bg-background px-3 py-2 font-mono text-sm placeholder:text-faint sm:w-48" />
          <button type="submit" disabled={busy} className={`${BUTTON_SECONDARY} px-4 py-2`}>Delete account</button>
        </form>
        <NoticeLine notice={deletion} />
      </Section>
    </>
  );

  if (page.view === 'subscribe') {
    return (
      <main id="main" className="mx-auto max-w-3xl px-4 py-12 sm:py-16">
        <h1 className={PAGE_TITLE}>Your account</h1>
        <p className="mt-2 break-all font-mono text-sm text-mute">{page.email}</p>
        <Section title="Jeff API">
          <p className="mt-3 text-text">{`${page.price} a month, ${page.allowance} input tokens, cancel any time.`}</p>
          {page.status !== null && <p className="mt-2 text-sm text-soft">{`Your last subscription is ${page.status.replaceAll('_', ' ')}.`}</p>}
          <div className="mt-5 flex flex-wrap gap-3">
            <button type="button" disabled={busy} className={`${BUTTON_PRIMARY} px-4 py-2`}
              onClick={() => void toStripe('/api/account/checkout', setBilling)}>Subscribe with Stripe</button>
            {page.status !== null && (
              <button type="button" disabled={busy} className={`${BUTTON_SECONDARY} px-4 py-2`}
                onClick={() => void toStripe('/api/account/portal', setBilling)}>Manage billing</button>
            )}
          </div>
          <NoticeLine notice={billing} />
        </Section>
        {page.playground !== null && <Section title="Playground"><PlaygroundLine playground={page.playground} /></Section>}
        {leaving}
      </main>
    );
  }

  const ends = longDate(page.periodEnd);
  const full = page.keys.length >= MAX_LIVE_KEYS;
  return (
    <main id="main" className="mx-auto max-w-3xl px-4 py-12 sm:py-16">
      <h1 className={PAGE_TITLE}>Your account</h1>
      <p className="mt-2 break-all font-mono text-sm text-mute">{page.email}</p>

      <Section title="Subscription">
        <p className="mt-3 text-text">{subscriptionLine(page)}</p>
        <div className="mt-4">
          <button type="button" disabled={busy} className={`${BUTTON_SECONDARY} px-4 py-2`}
            onClick={() => void toStripe('/api/account/portal', setBilling)}>Manage billing</button>
        </div>
        <p className="mt-3 text-xs text-mute">Invoices, the card and cancelling are on Stripe's billing page.</p>
        <NoticeLine notice={billing} />
      </Section>

      <Section title="Usage this period">
        <UsageBar used={page.usage.used} allowance={page.usage.allowance} resets={ends} />
        <PlaygroundLine playground={page.playground} />
      </Section>

      <Section title="API keys">
        <p className="mt-3 text-sm text-soft">{`${page.keys.length} of ${MAX_LIVE_KEYS} keys. Send a key as Authorization: Bearer <key>.`}</p>
        {created !== null && <CreatedKey name={created.name} keyText={created.key} />}
        <KeysTable keys={page.keys} busy={busy} onRevoke={setRevoking} />
        <label htmlFor="key-name" className="mt-5 block font-mono text-sm text-soft">Key name</label>
        <form className="mt-2 flex flex-col gap-3 sm:flex-row" onSubmit={createKey}>
          <input id="key-name" value={keyName} onChange={(e) => setKeyName(e.target.value)} required maxLength={MAX_NAME_LENGTH}
            placeholder="e.g. laptop" autoComplete="off"
            className="min-w-0 flex-1 border border-ink bg-background px-3 py-2 font-mono text-sm placeholder:text-faint" />
          <button type="submit" disabled={busy || full} className={`${BUTTON_PRIMARY} px-4 py-2 disabled:opacity-60`}>Create key</button>
        </form>
        {full && <p className="mt-2 text-sm text-soft">{`You have ${MAX_LIVE_KEYS} keys, the most an account may hold; revoke one to create another.`}</p>}
        <NoticeLine notice={keys} />
      </Section>

      {leaving}
      <ConfirmDialog
        open={revoking !== null}
        title="Revoke key"
        message={revoking === null ? '' : `Revoke the key "${revoking.name}"? Programs that use it stop working within about a minute, and it cannot be brought back.`}
        confirmLabel="Revoke key"
        danger
        onConfirm={confirmRevoke}
        onCancel={() => setRevoking(null)}
      />
    </main>
  );
}
