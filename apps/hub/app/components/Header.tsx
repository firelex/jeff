import { Link, NavLink } from 'react-router';
import type { SearchEntry } from '../lib/search';
import { AccountButtons } from './AccountButtons';
import { Logo } from './Logo';
import { Search } from './Search';

// The links are mono below 1280 px and the pixel font from there: at 20 px, the smallest crisp size, the pixel font is
// wide, so from 1280 px the gaps and the links' padding are a little smaller, the search box keeps its narrower width
// and the GitHub link stays mono, for the three links and the account buttons to fit the header's 1280 px. Below 768 px
// the three links are a second row under the logo, the search and the account buttons (one row would be about 420 px
// wide). Below 1024 px the account buttons are one key icon (AccountButtons.tsx). There is no Console link: the Jeff
// Console is for the people who run a server, and a customer of the public API never uses it.
// searchIndex null: no search box and no account buttons (they learn the session in the browser), on the static Not
// found page (root.tsx: HydrateFallback), which runs no script.
export function Header({ searchIndex }: { searchIndex: SearchEntry[] | null }) {
  const item = ({ isActive }: { isActive: boolean }) =>
    `px-1.5 py-1.5 font-mono text-sm sm:px-3 xl:px-2 xl:font-pixel xl:text-pixel-20 ${isActive ? 'text-primary underline decoration-2 underline-offset-[6px]' : 'text-soft hover:text-text'}`;
  return (
    <header className="sticky top-0 z-20 border-b border-line bg-background">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-1 px-4 sm:gap-x-4 md:h-14 md:flex-nowrap xl:gap-x-2.5">
        {/* The logo's pixel grid stays crisp at whole pixels per cell: 2 px at phone width, 3 px above. */}
        <Link to="/" className="mr-1 flex h-14 shrink-0 items-center text-ink sm:mr-2 md:h-auto">
          <Logo className="h-[14px] w-[86px] sm:h-[21px] sm:w-[129px]" />
        </Link>
        {/* Below 768 px: the last item, a row of its own. */}
        <nav aria-label="Site" className="order-last -mx-1.5 -mt-2 flex w-full shrink-0 items-center gap-0.5 pb-1.5 sm:-mx-3 sm:gap-1 md:order-none md:mx-0 md:mt-0 md:w-auto md:pb-0">
          <NavLink to="/docs/guides/introduction" className={item}>Docs</NavLink>
          <NavLink to="/catalogue" className={item}>Catalogue</NavLink>
          <NavLink to="/playground" className={item}>Playground</NavLink>
        </nav>
        <div className="flex-1" />
        {searchIndex === null ? null : <Search index={searchIndex} />}
        <a href="https://github.com/firelex/jeff" className="hidden font-mono text-sm text-soft hover:text-text sm:block">GitHub</a>
        {searchIndex === null ? null : <AccountButtons />}
      </div>
    </header>
  );
}
