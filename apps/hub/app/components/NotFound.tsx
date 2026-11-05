import { Link } from 'react-router';
import { PAGE_TITLE, TEXT_LINK } from './styles';

// The page for an address the site does not have: shown by the static 404.html (root.tsx's HydrateFallback, which is
// what the build's SPA fallback page renders) and by the root ErrorBoundary when a link inside the site leads nowhere.
export function NotFound() {
  return (
    <main id="main" className="mx-auto max-w-3xl px-4 py-16">
      <h1 className={PAGE_TITLE}>Not found</h1>
      <p className="mt-4 text-sm leading-7 text-soft">
        There is no page at this address. It may have moved, or the link that led here may be wrong.
      </p>
      <ul className="mt-6 flex flex-wrap gap-x-6 gap-y-2 text-sm">
        <li><Link to="/" className={TEXT_LINK}>Home</Link></li>
        <li><Link to="/docs/guides/introduction" className={TEXT_LINK}>Docs</Link></li>
        <li><Link to="/catalogue" className={TEXT_LINK}>Catalogue</Link></li>
      </ul>
    </main>
  );
}

// Any other error: what went wrong, said plainly, with the error's own message so it can be reported.
export function PageError({ message }: { message: string }) {
  return (
    <main id="main" className="mx-auto max-w-3xl px-4 py-16">
      <h1 className={PAGE_TITLE}>Something went wrong</h1>
      <p className="mt-4 text-sm leading-7 text-soft">This page could not be shown:</p>
      <pre className="mt-2 overflow-x-auto whitespace-pre-wrap border border-ink bg-panel p-4 font-mono text-xs">{message}</pre>
    </main>
  );
}
