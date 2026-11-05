// /signin and /signup: one page (spec Section 3), prerendered like every page of the hub; /signup carries the pricing
// line. Which of the two it is comes from the matched route's id (routes.ts names them signin and signup): the
// prerender's data request for /signup is /signup.data, so the loader's request cannot tell. The build gives it
// Turnstile's site key, the plan's price and allowance, and the sign-in methods to show (SIGN_IN_PROVIDERS): public
// settings, read at build time. In the browser it asks the hub's function (GET /api/signin-options) whether the
// visitor is signed in already (then it goes to the account), which methods the function offers (when that is not
// the list the page was built with, the page says so and offers none), and whether the test OpenID provider is on,
// and it signs in through Better Auth at /api/auth on the same origin. The ?error= a failed sign-in comes back with is read only after
// hydration, as the catalogue reads its query, so the first render matches the prerendered markup.
import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { Link, useLoaderData, useMatches, useNavigate, useSearchParams } from 'react-router';
import { BUTTON_PRIMARY, BUTTON_SECONDARY, PAGE_TITLE, TEXT_LINK } from '../components/styles';
import { Turnstile } from '../components/Turnstile';
import { authClient } from '../lib/auth-client';
import { MAGIC_LINK_MINUTES, NEXT_PAGES, SOCIAL_PROVIDERS, TEST_PROVIDER_ID, type CallbackPath, type SignInProvider, type SocialProvider } from '../lib/auth-shared';
import { priceText, publicSettings, tokensText } from '../lib/public-settings.server';
import { TURNSTILE_HEADER_NAME } from '../lib/turnstile-header';
import type { Route } from './+types/signin';

// routes.ts's id for /signup.
const SIGN_UP_ROUTE_ID = 'signup';

function isSignUp(matches: readonly ({ id: string } | undefined)[]): boolean {
  const own = matches[matches.length - 1];
  if (own === undefined) throw new Error('The sign-in page rendered outside a route match');
  return own.id === SIGN_UP_ROUTE_ID;
}

export function meta({ matches }: Route.MetaArgs) {
  return [{ title: isSignUp(matches) ? 'Get an API key | Jeff' : 'Sign in | Jeff' }];
}

export function loader() {
  const shown = publicSettings();
  return {
    siteKey: shown.turnstileSiteKey,
    price: priceText(shown.priceUsd),
    allowance: tokensText(shown.allowanceInputTokens),
    providers: shown.signInProviders,
  };
}

const SOCIAL_LABELS: Record<SocialProvider, string> = { github: 'Continue with GitHub', google: 'Continue with Google' };

// Where Better Auth sends the visitor after signing in, and after a sign-in that failed (with ?error=<code>): two of
// the CALLBACK_PATHS the hub's function accepts, the only ones it does. A page that sent the visitor here with
// ?next=<page> (one of NEXT_PAGES, e.g. /request-adapter) is where they go back to instead; a ?next naming any other
// page is not followed.
const AFTER_SIGN_IN: CallbackPath = '/account';
const AFTER_FAILURE: CallbackPath = '/signin';

function afterSignIn(next: string | null): CallbackPath {
  return (NEXT_PAGES as readonly string[]).includes(next ?? '') ? next as CallbackPath : AFTER_SIGN_IN;
}

// The codes Better Auth 1.7.16 puts in ?error= that a visitor can do something about. Its magic-link plugin answers
// INVALID_TOKEN for both a used and an expired link (it has no separate code for an expired one); its OAuth callback
// answers account_not_linked when a provider did not confirm the email of an existing account. Any other code is
// shown as it is.
const SIGN_IN_ERRORS: Record<string, string> = {
  INVALID_TOKEN: 'That sign-in link has expired or was already used. Ask for a new one below.',
  account_not_linked: 'That email already has a Jeff account, and the provider did not confirm that the email is '
    + 'yours. Sign in with a link sent to your email instead.',
};

interface SigninOptions { signedIn: boolean; providers: SignInProvider[]; testProvider: boolean }

type SendState = { kind: 'idle' | 'sending' | 'sent' | 'failed'; message: string };

export default function SignIn() {
  const { siteKey, price, allowance, providers } = useLoaderData<typeof loader>();
  const mode = isSignUp(useMatches()) ? 'signup' : 'signin';
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [hydrated, setHydrated] = useState(false);
  const [testProvider, setTestProvider] = useState(false);
  // Set when the function offers other sign-in methods than the page was built with: then the page offers none.
  const [mismatch, setMismatch] = useState<string | null>(null);
  const [serviceError, setServiceError] = useState<string | null>(null);
  const [email, setEmail] = useState('');
  const [token, setToken] = useState<string | null>(null);
  const [widget, setWidget] = useState(0);
  const [state, setState] = useState<SendState>({ kind: 'idle', message: '' });
  const onToken = useCallback((value: string | null) => setToken(value), []);

  useEffect(() => {
    setHydrated(true);
    let live = true;
    void (async () => {
      const response = await fetch('/api/signin-options', { credentials: 'same-origin', headers: { accept: 'application/json' } });
      if (!live) return;
      if (!response.ok) { setServiceError(`The sign-in service answered ${response.status}. Reload the page to try again.`); return; }
      const options = (await response.json()) as SigninOptions;
      if (!live) return;
      if (options.signedIn) { navigate(afterSignIn(params.get('next')), { replace: true }); return; }
      if (options.providers.join(',') !== providers.join(',')) {
        setMismatch(`This page was built to offer sign-in with ${providers.join(', ')}, but the hub's function offers `
          + `${options.providers.join(', ')}. The site and its function must be deployed with the same SIGN_IN_PROVIDERS.`);
        return;
      }
      setTestProvider(options.testProvider);
    })();
    return () => { live = false; };
  }, [navigate, providers, params]);

  const socials = SOCIAL_PROVIDERS.filter((provider) => providers.includes(provider));

  const signInError = hydrated ? params.get('error') : null;
  // Read only after hydration, as ?error= is: the prerendered page has no query.
  const after = hydrated ? afterSignIn(params.get('next')) : AFTER_SIGN_IN;

  const social = async (provider: string) => {
    setServiceError(null);
    const { error } = await authClient().signIn.social({ provider, callbackURL: after, errorCallbackURL: AFTER_FAILURE });
    if (error !== null) setServiceError(error.message ?? `The sign-in could not start (${error.status} ${error.statusText})`);
  };

  const sendLink = async (event: FormEvent) => {
    event.preventDefault();
    if (token === null) { setState({ kind: 'failed', message: 'Wait for the bot check above to finish, then try again.' }); return; }
    setState({ kind: 'sending', message: '' });
    const { error } = await authClient().signIn.magicLink(
      { email, callbackURL: after, errorCallbackURL: AFTER_FAILURE },
      { headers: { [TURNSTILE_HEADER_NAME]: token } });
    // A Turnstile token passes one check only: draw a fresh widget for the next send.
    setToken(null);
    setWidget((n) => n + 1);
    setState(error === null
      ? { kind: 'sent', message: `Check your email: we sent a link to ${email}. It works once, within ${MAGIC_LINK_MINUTES} minutes.` }
      : { kind: 'failed', message: error.message ?? `The email could not be sent (${error.status} ${error.statusText})` });
  };

  return (
    <main id="main" className="mx-auto max-w-md px-4 py-16">
      <h1 className={PAGE_TITLE}>{mode === 'signup' ? 'Get an API key' : 'Sign in'}</h1>
      {mode === 'signup' && <p className="mt-3 text-sm text-soft">{`${price} a month, ${allowance} input tokens, cancel any time.`}</p>}
      {signInError !== null && (
        <p role="alert" className="mt-4 text-sm text-danger">{SIGN_IN_ERRORS[signInError] ?? `Sign-in failed: ${signInError}`}</p>
      )}
      {serviceError !== null && <p role="alert" className="mt-4 text-sm text-danger">{serviceError}</p>}
      {mismatch !== null ? <p role="alert" className="mt-4 text-sm text-danger">{mismatch}</p> : (
        <>
          {(socials.length > 0 || testProvider) && (
            <div className="mt-8 flex flex-col gap-3">
              {socials.map((provider) => (
                <button key={provider} type="button" className={`${BUTTON_SECONDARY} px-4 py-2`} onClick={() => void social(provider)}>
                  {SOCIAL_LABELS[provider]}
                </button>
              ))}
              {testProvider && (
                <button type="button" className={`${BUTTON_SECONDARY} px-4 py-2`} onClick={() => void social(TEST_PROVIDER_ID)}>
                  Continue with the test provider
                </button>
              )}
            </div>
          )}
          {providers.includes('email') && (
            <form className="mt-10 flex flex-col gap-3" onSubmit={(event) => void sendLink(event)}>
              <label className="font-mono text-sm text-soft" htmlFor="email">
                {socials.length > 0 ? 'Or sign in with a link sent to your email' : 'Sign in with a link sent to your email'}
              </label>
              <input id="email" type="email" required value={email} onChange={(e) => setEmail(e.target.value)}
                className="border border-ink bg-panel px-3 py-2 font-mono text-sm" autoComplete="email" />
              <Turnstile key={widget} siteKey={siteKey} onToken={onToken} />
              <button type="submit" className={`${BUTTON_PRIMARY} px-4 py-2`} disabled={state.kind === 'sending'}>Email me a link</button>
              {state.message !== '' && (
                <p role={state.kind === 'failed' ? 'alert' : 'status'} className={`text-sm ${state.kind === 'failed' ? 'text-danger' : 'text-soft'}`}>
                  {state.message}
                </p>
              )}
            </form>
          )}
        </>
      )}
      <p className="mt-10 text-sm text-soft">
        {'By signing in you agree to the '}<Link to="/terms" className={TEXT_LINK}>terms of service</Link>
        {'. The '}<Link to="/privacy" className={TEXT_LINK}>privacy policy</Link>{' says what we keep and why.'}
      </p>
    </main>
  );
}
