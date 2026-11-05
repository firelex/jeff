import type { ButtonHTMLAttributes, ReactNode } from 'react';

export interface AppShellProps {
  rail: ReactNode;
  children: ReactNode;
  className?: string;
}

/** The deck's two-column layout: a 236px left rail, the page to its right. */
export function AppShell({ rail, children, className = '' }: AppShellProps) {
  return (
    <div className={['grid h-screen bg-background text-text', className].join(' ')} style={{ gridTemplateColumns: '236px 1fr' }}>
      <aside className="flex flex-col overflow-auto border-r border-line bg-[#f4f4f6] p-3 pt-4">{rail}</aside>
      <main className="min-w-0 overflow-auto">{children}</main>
    </div>
  );
}

export interface RailGroupProps {
  label: ReactNode;
  className?: string;
}

/** A section heading inside the rail (`.grp` in the deck). */
export function RailGroup({ label, className = '' }: RailGroupProps) {
  return <p className={['mx-2.5 mt-[7px] mb-px text-[10.5px] font-bold uppercase tracking-[0.14em] text-mute', className].join(' ')}>{label}</p>;
}

export interface RailItemProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  active?: boolean;
  icon?: ReactNode;
  children?: ReactNode;
}

/** A clickable row inside the rail (`.ch`/`.ch.on` in the deck). */
export function RailItem({ active = false, icon, children, className = '', type = 'button', ...rest }: RailItemProps) {
  return (
    <button
      type={type}
      aria-current={active ? 'page' : undefined}
      className={[
        'flex w-full items-center gap-2.5 rounded-nav px-2.5 py-0.5 text-left text-[13px] font-medium leading-[1.45] text-mute transition-colors',
        active ? 'bg-grad text-white shadow-nav' : 'hover:bg-white/70 hover:text-text',
        className,
      ].join(' ')}
      {...rest}
    >
      {icon && <span aria-hidden="true" className="flex size-[18px] shrink-0 items-center justify-center">{icon}</span>}
      <span className="min-w-0 flex-1 truncate">{children}</span>
    </button>
  );
}
