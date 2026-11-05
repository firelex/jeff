import { useEffect, useState } from 'react';
import { KeyRound } from 'lucide-react';
import { Link } from 'react-router';
import { authClient } from '../lib/auth-client';
import { HEADER_BUTTON } from './styles';

// Mono below 1280 px and the pixel font from there, as the header's other links are (Header.tsx). Below 1024 px the
// header has room for one 32 px key only: "Sign in" is left out (the sign-up page signs in too) and the keycap is a key
// icon, named "Get an API key".
const LINK = 'hidden font-mono text-sm text-soft hover:text-text lg:inline xl:font-pixel xl:text-pixel-20';
const NOTE_LINK = 'font-mono text-sm text-soft hover:text-text xl:font-pixel xl:text-pixel-20';
const KEYCAP = `${HEADER_BUTTON} size-8 shrink-0 lg:size-auto lg:px-3 lg:py-1`;

function KeycapFace() {
  return (
    <>
      <KeyRound className="size-4 lg:hidden" aria-hidden="true" />
      <span className="hidden lg:inline">Get an API key</span>
    </>
  );
}

// The visitor's two buttons: "Sign in" and the "Get an API key" keycap. `live` false draws the same boxes as plain,
// hidden text, holding their place without a link.
function VisitorButtons({ live }: { live: boolean }) {
  if (!live) {
    return (
      <span aria-hidden="true" className="invisible flex shrink-0 items-center gap-4">
        <span className={LINK}>Sign in</span>
        <span className={KEYCAP}><KeycapFace /></span>
      </span>
    );
  }
  return (
    <span className="flex shrink-0 items-center gap-4">
      <Link to="/signin" className={LINK}>Sign in</Link>
      <Link to="/signup" aria-label="Get an API key" className={KEYCAP}><KeycapFace /></Link>
    </span>
  );
}

// Why the session could not be checked, in words: Better Auth's message, or the status when it gives none (an empty
// message included), so the sentence never ends on a colon.
function failureText(error: { message?: string; status: number; statusText: string }): string {
  if (error.message !== undefined && error.message.trim() !== '') return error.message;
  const status = `${error.status} ${error.statusText}`.trim();
  return status === '0' ? 'the request did not reach the hub' : `the hub answered ${status}`;
}

// Asked of Better Auth in the browser only (authClient() does not run at build time).
function SessionButtons() {
  const { data, isPending, error } = authClient().useSession();
  const failure = error === null ? null : `Could not check whether you are signed in: ${failureText(error)}`;
  // Said once in the browser's console for each failure, not on every render.
  useEffect(() => {
    if (failure !== null) console.error(failure, error);
  }, [failure, error]);
  if (isPending) return <VisitorButtons live={false} />;
  if (failure !== null) {
    // Whether the visitor is signed in could not be learned. The account page finds out for itself (it sends a visitor
    // who is not signed in to /signin), so the header links there and says why it does not show more.
    return <Link to="/account" className={NOTE_LINK} title={failure}>Account</Link>;
  }
  if (data === null) return <VisitorButtons live />;
  // An account made by an email link has no name (the sign-in page asks for none): its email names it.
  const { name, email } = data.user;
  const who = name === '' ? email : name;
  // The initial, not the provider's picture: the pages' content security policy loads images from the hub only.
  return (
    <Link to="/account" aria-label={`Your account (${who})`}
      className="flex size-8 shrink-0 items-center justify-center border-2 border-ink bg-primary-light text-ink hover:bg-panel">
      <span className="font-pixel text-pixel-20" aria-hidden="true">{who.slice(0, 1).toUpperCase()}</span>
    </Link>
  );
}

// The header's right end (spec Section 7). Pages are prerendered without a session, so the session is read in the
// browser after hydration; until it is known, the visitor's buttons hold their place as hidden text, so the first
// render matches the prerendered markup and no button shows that may be wrong.
export function AccountButtons() {
  const [hydrated, setHydrated] = useState(false);
  useEffect(() => { setHydrated(true); }, []);
  return hydrated ? <SessionButtons /> : <VisitorButtons live={false} />;
}
