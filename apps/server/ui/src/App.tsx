// The console: the sign-in screen until there is a session, then the deck-style shell (rail and page). A 401 for
// the session in use ends it and returns to sign-in with the server's message; a 401 for a session that has
// already ended changes nothing.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { BookOpen, Boxes, FlaskConical, Hammer, House, LibraryBig, LogOut, ScrollText } from 'lucide-react';
import logo from '@jeff/brand/logo-128.jpg';
import { AppShell, Button, EmptyState, LoginForm, RailGroup, RailItem, StatusDot, type Status, type TypedError } from '@jeff/ui';
import { createApi, shownError, signIn, type Api, type ApiError, type Health, type Me, type Session, type ShownError } from './api';
import { ErrorAlert } from './components/ErrorAlert';
import { ErrorBoundary } from './components/ErrorBoundary';
import { Page } from './components/Page';
import { AccessLog } from './pages/AccessLog';
import { Adapters, isOwner } from './pages/Adapters';
import { ApiReference } from './pages/ApiReference';
import { Factory } from './pages/factory/Factory';
import { Home, type TryFunction } from './pages/Home';
import { Playground } from './pages/Playground';
import { eventView, parseHash, routeHash, type Route } from './routes';
import { clearSession, readSession, writeSession } from './session';

const HEALTH_EVERY_MS = 30_000;

// The public hub's docs, baked in at build time (vite.config.ts requires it for a build). The rail's "Documentation"
// item is a plain external link to it, not a view of this app.
const DOCS_URL: string = import.meta.env.VITE_JEFF_DOCS_URL;

function Logo() {
  return (
    <div className="mx-1.5 mb-1.5 flex items-center gap-2.5">
      <img src={logo} alt="Jeff logo" className="block size-[34px] flex-none rounded-[9px] bg-[#06081a] object-cover" />
      <b className="text-[17px] font-semibold text-text">Jeff Console</b>
    </div>
  );
}

function SignIn({ error, onSignedIn }: { error: TypedError | null; onSignedIn: (session: Session) => void }) {
  const [failure, setFailure] = useState<TypedError | null>(error);
  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4 text-text">
      <div className="flex w-full max-w-sm flex-col gap-6 rounded-card border border-line bg-panel p-8 shadow-card">
        <div className="flex flex-col gap-3">
          <Logo />
          <p className="m-0 mx-1.5 text-sm text-soft">Sign in with the account this server's owner gave you.</p>
        </div>
        <LoginForm
          error={failure}
          onSubmit={async (email, password) => {
            try {
              onSignedIn(await signIn(email, password));
            } catch (caught) {
              const shown = shownError(caught);
              setFailure({ type: shown.type, message: shown.message });
            }
          }}
        />
      </div>
    </div>
  );
}

export function App() {
  const [initial] = useState(readSession);
  const [session, setSession] = useState<Session | null>(initial.session);
  const [signInError, setSignInError] = useState<TypedError | null>(
    initial.problem === null ? null : { type: 'SavedSignInRemoved', message: initial.problem },
  );
  // The token of the session in use right now, read synchronously by onUnauthorised: a 401 that arrives after its
  // session has already ended (a second concurrent refusal, or a late one for a token replaced by signing in again)
  // must not touch the current state.
  const currentToken = useRef<string | null>(initial.session?.token ?? null);

  const signOut = useCallback(() => {
    clearSession();
    currentToken.current = null;
    setSignInError(null);
    setSession(null);
  }, []);

  const onUnauthorised = useCallback((error: ApiError, token: string) => {
    if (token !== currentToken.current) return;
    clearSession();
    currentToken.current = null;
    setSignInError({ type: error.type, message: error.message });
    setSession(null);
  }, []);

  if (!session) {
    return (
      <SignIn
        key={signInError ? `${signInError.type}:${signInError.message}` : 'signed-out'}
        error={signInError}
        onSignedIn={(signedIn) => {
          writeSession(signedIn);
          currentToken.current = signedIn.token;
          setSignInError(null);
          setSession(signedIn);
        }}
      />
    );
  }
  return <Console session={session} onUnauthorised={onUnauthorised} onSignOut={signOut} />;
}

function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));
  useEffect(() => {
    const onHash = () => setRoute(parseHash(window.location.hash));
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  return route;
}

function go(route: Route) {
  window.location.hash = routeHash(route);
}

const HEALTH_STATUS: Record<Health['status'], Status> = { ready: 'ready', degraded: 'degraded', broken: 'broken' };
const HEALTH_LABEL: Record<Health['status'], string> = { ready: 'Server ready', degraded: 'Server degraded', broken: 'Server broken' };

function Console({ session, onUnauthorised, onSignOut }: {
  session: Session; onUnauthorised: (error: ApiError, token: string) => void; onSignOut: () => void;
}) {
  const api: Api = useMemo(() => createApi({ token: session.token, onUnauthorised }), [session.token, onUnauthorised]);
  const route = useRoute();
  const [me, setMe] = useState<Me | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [healthError, setHealthError] = useState<ShownError | null>(null);
  const [shellErrors, setShellErrors] = useState<Record<string, ShownError>>({});
  const [seed, setSeed] = useState<(TryFunction & { nonce: number }) | null>(null);

  const report = useCallback((source: string, failure: unknown) => {
    setShellErrors((errors) => ({ ...errors, [source]: shownError(failure) }));
  }, []);
  const settle = useCallback((source: string) => {
    setShellErrors((errors) => {
      if (!(source in errors)) return errors;
      const next = { ...errors };
      delete next[source];
      return next;
    });
  }, []);

  useEffect(() => {
    let current = true;
    void (async () => {
      try {
        const answer = await api.me();
        if (current) setMe(answer);
      } catch (failure) {
        if (current) report('Who is signed in (GET /ui/me)', failure);
      }
    })();
    return () => {
      current = false;
    };
  }, [api, report]);

  useEffect(() => {
    let current = true;
    const check = async () => {
      try {
        const answer = await api.health();
        if (!current) return;
        setHealth(answer);
        setHealthError(null);
      } catch (failure) {
        if (current) setHealthError(shownError(failure));
      }
    };
    void check();
    const timer = window.setInterval(() => void check(), HEALTH_EVERY_MS);
    return () => {
      current = false;
      window.clearInterval(timer);
    };
  }, [api]);

  const view = eventView(route);
  useEffect(() => {
    const source = 'Recording the page view (POST /ui/events)';
    let current = true;
    void (async () => {
      try {
        await api.reportView(view);
        if (current) settle(source);
      } catch (failure) {
        if (current) report(source, failure);
      }
    })();
    return () => {
      current = false;
    };
  }, [api, view, report, settle]);

  const tryFunction = (request: TryFunction) => {
    setSeed({ ...request, nonce: Date.now() });
    go({ view: 'playground' });
  };

  const status: Status = healthError ? 'unknown' : health ? HEALTH_STATUS[health.status] : 'unknown';
  const statusLabel = healthError ? `Health: ${healthError.type}` : health ? HEALTH_LABEL[health.status] : 'Checking the server';
  const statusTitle = healthError ? healthError.message : health?.cause ?? health?.checkpoint ?? undefined;

  const rail = (
    <>
      <Logo />
      <nav aria-label="Console" className="flex flex-col">
      <RailGroup label="Use" />
      <RailItem active={route.view === 'home'} icon={<House className="size-4" />} onClick={() => go({ view: 'home' })}>Home</RailItem>
      <RailItem active={route.view === 'playground'} icon={<FlaskConical className="size-4" />} onClick={() => go({ view: 'playground' })}>Playground</RailItem>
      <RailGroup label="Manage" />
      <RailItem active={route.view === 'adapters'} icon={<Boxes className="size-4" />} onClick={() => go({ view: 'adapters' })}>Adapters</RailItem>
      {isOwner(me) && (
        <RailItem active={route.view === 'factory'} icon={<Hammer className="size-4" />} onClick={() => go({ view: 'factory' })}>Factory</RailItem>
      )}
      {me?.admin && (
        <RailItem active={route.view === 'access'} icon={<ScrollText className="size-4" />} onClick={() => go({ view: 'access' })}>Access log</RailItem>
      )}
      <RailGroup label="Learn" />
      <RailItem active={route.view === 'api'} icon={<BookOpen className="size-4" />} onClick={() => go({ view: 'api' })}>API reference</RailItem>
      <a
        href={DOCS_URL}
        target="_blank"
        rel="noopener noreferrer"
        className="flex w-full items-center gap-2.5 rounded-nav px-2.5 py-0.5 text-left text-[13px] font-medium leading-[1.45] text-mute transition-colors hover:bg-white/70 hover:text-text"
      >
        <span aria-hidden="true" className="flex size-[18px] shrink-0 items-center justify-center"><LibraryBig className="size-4" /></span>
        <span className="min-w-0 flex-1 truncate">Documentation</span>
      </a>
      </nav>
      <div className="flex-1" />
      <div className="mt-4 flex flex-col gap-2 border-t border-line px-1.5 pt-3">
        <span title={statusTitle}><StatusDot status={status} label={statusLabel} /></span>
        <span className="min-w-0 truncate text-xs text-soft" title={session.user}>{session.user}</span>
        <div>
          <Button variant="ghost" size="sm" className="whitespace-nowrap" onClick={onSignOut}>
            <LogOut className="size-3.5" aria-hidden="true" />
            Sign out
          </Button>
        </div>
      </div>
    </>
  );

  return (
    <AppShell rail={rail}>
      {Object.keys(shellErrors).length > 0 && (
        <div className="flex flex-col gap-2 px-[clamp(20px,4vw,64px)] pt-4">
          {Object.entries(shellErrors).map(([source, error]) => (
            <div key={source} className="flex flex-col gap-1">
              <p className="m-0 text-xs text-mute">{source}</p>
              <ErrorAlert error={error} />
            </div>
          ))}
        </div>
      )}
      <ErrorBoundary key={routeHash(route)}>
        <View route={route} api={api} session={session} health={health} me={me} seed={seed} onTry={tryFunction}
          onSeedApplied={() => setSeed(null)} />
      </ErrorBoundary>
    </AppShell>
  );
}

function View({ route, api, session, health, me, seed, onTry, onSeedApplied }: {
  route: Route; api: Api; session: Session; health: Health | null; me: Me | null;
  seed: (TryFunction & { nonce: number }) | null;
  onTry: (request: TryFunction) => void; onSeedApplied: () => void;
}) {
  switch (route.view) {
    case 'home':
      return <Home api={api} user={session.user} onTry={onTry} />;
    case 'playground':
      return <Playground api={api} health={health} seed={seed} onSeedApplied={onSeedApplied} />;
    case 'factory':
      return <Factory api={api} onTry={onTry} />;
    case 'adapters':
      return <Adapters api={api} me={me} />;
    case 'access':
      return <AccessLog api={api} />;
    case 'api':
      return <ApiReference api={api} />;
    case 'not-found':
      return (
        <Page eyebrow="Not found" title="No such page">
          <EmptyState title={`Nothing at ${route.hash}`} description="Choose a page in the rail on the left." />
        </Page>
      );
  }
}
