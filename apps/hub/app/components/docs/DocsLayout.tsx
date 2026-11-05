import { useEffect, useRef, useState, type ReactNode } from 'react';
import type { Heading, Sidebar as SidebarData, Tab } from '../../lib/docs.server';
import { OnThisPage } from './OnThisPage';
import { Sidebar } from './Sidebar';
import { TabBar } from './TabBar';
import { BUTTON_SECONDARY } from '../styles';

export function DocsLayout({ tab, sidebar, current, headings, children }: {
  tab: Tab | 'api'; sidebar: SidebarData | { api: SidebarData['guides'] }; current: string; headings: Heading[];
  children: ReactNode;
}) {
  const [menu, setMenu] = useState(false);
  const opener = useRef<HTMLButtonElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  // The Contents dialog (phone width): focus moves into it when it opens, Escape closes it, and focus goes back to
  // the Contents button when it closes.
  useEffect(() => {
    if (!menu) return;
    close.current?.focus();
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') setMenu(false); };
    window.addEventListener('keydown', onKey);
    const button = opener.current;
    return () => {
      window.removeEventListener('keydown', onKey);
      button?.focus();
    };
  }, [menu]);
  const groups = (sidebar as Record<string, SidebarData['guides']>)[tab];
  if (groups === undefined) throw new Error(`No sidebar for tab ${tab}`);
  return (
    <div className="mx-auto max-w-7xl px-4">
      <TabBar active={tab} />
      <button ref={opener} type="button" className={`${BUTTON_SECONDARY} my-4 px-3 py-1 lg:hidden`}
        onClick={() => setMenu(true)}>Contents</button>
      {menu && (
        <div role="dialog" aria-modal="true" aria-label="Contents" className="fixed inset-0 z-30 overflow-y-auto bg-background p-4 lg:hidden">
          <button ref={close} type="button" className={`${BUTTON_SECONDARY} mb-4 px-3 py-1`} onClick={() => setMenu(false)}>Close</button>
          <Sidebar tab={tab} groups={groups} current={current} onNavigate={() => setMenu(false)} />
        </div>
      )}
      <div className="grid gap-10 lg:grid-cols-[220px_minmax(0,1fr)_200px]">
        <aside className="hidden py-8 lg:block"><Sidebar tab={tab} groups={groups} current={current} /></aside>
        <main id="main" className="min-w-0 max-w-[72ch] py-8"><article>{children}</article></main>
        <aside className="hidden py-8 lg:block">{headings.length > 0 ? <OnThisPage headings={headings} /> : null}</aside>
      </div>
    </div>
  );
}
