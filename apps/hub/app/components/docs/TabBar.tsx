// The three docs tabs: Guides, SDK and the API reference. The active one is violet, led by the block cursor, on a
// violet underline.
import { Link } from 'react-router';
import { tabClass } from '../styles';

const TABS: { tab: 'guides' | 'sdk' | 'api'; label: string; to: string }[] = [
  { tab: 'guides', label: 'Guides', to: '/docs/guides/introduction' },
  { tab: 'sdk', label: 'SDK', to: '/docs/sdk/install' },
  { tab: 'api', label: 'API reference', to: '/docs/api' },
];

export function TabBar({ active }: { active: 'guides' | 'sdk' | 'api' }) {
  return (
    <nav aria-label="Docs sections" className="flex gap-2 overflow-x-auto border-b border-ink sm:gap-4">
      {TABS.map(({ tab, label, to }) => (
        <Link
          key={tab}
          to={to}
          aria-current={tab === active ? 'page' : undefined}
          className={`-mb-px shrink-0 border-b-2 py-3 ${tabClass(tab === active)} ${tab === active ? 'border-primary' : 'border-transparent'}`}
        >
          {label}
        </Link>
      ))}
    </nav>
  );
}
