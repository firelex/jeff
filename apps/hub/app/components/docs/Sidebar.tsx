// The docs contents list, one group of pages at a time. The hierarchy is plain at a glance: each group's name is a
// heavier mono label in ink with more space above it than between its pages; its pages hang under it, indented
// behind a thin ink rule; the current page takes the rule in violet, a violet tint, violet text and the block cursor.
import { Link } from 'react-router';
import type { Sidebar as SidebarData } from '../../lib/docs.server';

export function Sidebar({ tab, groups, current, onNavigate }: {
  tab: string;
  groups: SidebarData['guides'];
  current: string;
  onNavigate?: () => void;
}) {
  return (
    <nav aria-label="Documentation">
      {groups.map(({ group, pages }) => (
        <div key={group} className="mt-8 first:mt-0">
          <p className="px-3 font-mono text-xs font-bold uppercase tracking-wider text-text">{group}</p>
          <ul className="mt-2 ml-3 border-l-2 border-ink/20">
            {pages.map((page) => (
              <li key={page.id}>
                <Link
                  to={`/docs/${tab}/${page.id}`}
                  aria-current={page.id === current ? 'page' : undefined}
                  onClick={onNavigate}
                  className={`-ml-0.5 block border-l-2 py-1.5 pr-3 pl-3 text-sm ${page.id === current
                    ? 'cursor-mark border-primary bg-primary/10 font-medium text-primary'
                    : 'border-transparent text-soft hover:border-ink hover:text-text'}`}
                >
                  {page.title}
                </Link>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </nav>
  );
}
