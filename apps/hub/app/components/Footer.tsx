import { Link } from 'react-router';

// The site's footer, on every page (root.tsx): the year and the company that runs Jeff (named for Stripe and the
// terms, owner 5 November 2026), the GitHub repository, the docs, the catalogue, and the
// terms and the privacy policy, which most pages otherwise reach only through the sign-in page. The links wrap onto
// more rows on a narrow screen, so nothing scrolls sideways at 375 px.
// `year` is fixed when the page is built (the root loader): a year computed in the browser would differ from the
// prerendered one after New Year, until the next build, and React would report a hydration mismatch.
export function Footer({ year }: { year: number }) {
  const link = 'hover:text-text';
  return (
    <footer className="border-t border-ink">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-4 px-4 py-8 text-sm text-soft">
        <span className="font-mono">&copy; {year} Casperaki Multimedia Solutions Ltd., company 10634954</span>
        <nav aria-label="Footer" className="flex flex-wrap items-center gap-x-5 gap-y-2 font-pixel text-pixel-20">
          <a href="https://github.com/firelex/jeff" className={link}>GitHub</a>
          <Link to="/docs/guides/introduction" className={link}>Docs</Link>
          <Link to="/catalogue" className={link}>Catalogue</Link>
          <Link to="/terms" className={link}>Terms</Link>
          <Link to="/privacy" className={link}>Privacy</Link>
        </nav>
      </div>
    </footer>
  );
}
