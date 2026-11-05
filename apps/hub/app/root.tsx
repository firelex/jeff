import { isRouteErrorResponse, Links, Meta, Outlet, Scripts, ScrollRestoration, useRouteLoaderData } from 'react-router';
import { Footer } from './components/Footer';
import { Header } from './components/Header';
import { NotFound, PageError } from './components/NotFound';
import { searchIndex } from './lib/site.server';
import type { SearchEntry } from './lib/search';
import jeffPixelUrl from '@jeff/brand/jeff-pixel.woff2?url';
import './app.css';

// The favicon: J and the block cursor (public/favicon.svg), SVG only -- every current browser takes an SVG icon.
// Jeff Pixel, the headings' font, is preloaded on every page so it is there by the first paint (its @font-face uses
// font-display: block, see packages/brand/jeff-pixel.css). This is the same fingerprinted file app.css's @font-face
// points at (scripts/check-build.mjs checks both, and that its bytes are packages/brand's).
export function links() {
  return [
    { rel: 'icon', href: '/favicon.svg', type: 'image/svg+xml' },
    { rel: 'preload', href: jeffPixelUrl, as: 'font', type: 'font/woff2', crossOrigin: 'anonymous' },
  ];
}

// The footer's year is fixed when the page is built (Footer.tsx says why).
export async function loader() {
  return { searchIndex: searchIndex(), year: new Date().getFullYear() };
}

export function Layout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        <meta charSet="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <Meta />
        <Links />
      </head>
      <body className="flex min-h-screen flex-col">
        <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-50 focus:border-2 focus:border-ink focus:bg-panel focus:px-3 focus:py-2 focus:font-mono focus:text-sm">
          Skip to content
        </a>
        {children}
        <ScrollRestoration />
        <Scripts />
      </body>
    </html>
  );
}

function useRootData(): { searchIndex: SearchEntry[]; year: number } {
  const data = useRouteLoaderData('root') as { searchIndex: SearchEntry[]; year: number } | undefined;
  if (data === undefined) throw new Error('The root loader data (the search index and the footer\'s year) is missing');
  return data;
}

// Every page: the header, the page (which takes the height left, so a short page keeps the footer at the bottom of
// the window), and the footer.
function Frame({ searchIndex, year, children }: { searchIndex: SearchEntry[] | null; year: number; children: React.ReactNode }) {
  return (
    <>
      <Header searchIndex={searchIndex} />
      <div className="flex-1">{children}</div>
      <Footer year={year} />
    </>
  );
}

export default function App() {
  const { searchIndex, year } = useRootData();
  return <Frame searchIndex={searchIndex} year={year}><Outlet /></Frame>;
}

// Every page the site has is prerendered with its data, so this renders only in the build's SPA fallback page, which
// scripts/static-404.mjs turns into 404.html, without scripts, for the host to serve at an address with no file. With
// no script there is no search box to offer. It has no loader data, so the footer's year is the build's, computed
// here: the page is rendered once, at build time, and never hydrated.
export function HydrateFallback() {
  return <Frame searchIndex={null} year={new Date().getFullYear()}><NotFound /></Frame>;
}

// A 404 (an address no route matches, or a page whose data file does not exist) shows the Not found page in the
// site's layout; any other error is shown with its message, never swallowed.
export function ErrorBoundary({ error }: { error: unknown }) {
  const { searchIndex, year } = useRootData();
  let body: React.ReactNode;
  if (isRouteErrorResponse(error) && error.status === 404) body = <NotFound />;
  else if (isRouteErrorResponse(error)) body = <PageError message={`${error.status} ${error.statusText}`} />;
  else if (error instanceof Error) body = <PageError message={error.message} />;
  else body = <PageError message={String(error)} />;
  return <Frame searchIndex={searchIndex} year={year}>{body}</Frame>;
}
