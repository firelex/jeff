import type { ReactNode } from 'react';

/** A console page in the deck's layout: an eyebrow, a heading, an optional lead paragraph, then the content. */
export function Page({ eyebrow, title, lead, actions, children }: {
  eyebrow: string; title: ReactNode; lead?: ReactNode; actions?: ReactNode; children: ReactNode;
}) {
  return (
    <div className="mx-auto flex w-full max-w-[1280px] flex-col px-[clamp(20px,4vw,64px)] pt-[clamp(24px,5vh,56px)] pb-12">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="min-w-0">
          <p className="text-[12.5px] font-semibold uppercase tracking-[0.16em] text-mute">{eyebrow}</p>
          <h1 className="mt-2.5 mb-0 text-[clamp(26px,2.6vw,40px)] leading-[1.1] font-semibold tracking-[-0.02em] text-text">{title}</h1>
        </div>
        {actions && <div className="flex items-center gap-2.5">{actions}</div>}
      </div>
      {lead && <p className="mt-4 mb-0 max-w-[68ch] text-[clamp(15px,1.2vw,18px)] leading-[1.45] text-soft">{lead}</p>}
      <div className="mt-[clamp(20px,3.4vh,36px)] flex min-w-0 flex-col gap-6">{children}</div>
    </div>
  );
}

/** The deck's `.card`. `id` lets a page jump straight to one (an anchor link, for example). */
export function Card({ id, children, className = '', accent = false }: { id?: string; children: ReactNode; className?: string; accent?: boolean }) {
  return (
    <div id={id} className={['rounded-card border p-5 shadow-card', accent ? 'border-primary-border bg-primary-light' : 'border-line bg-panel', className].join(' ')}>
      {children}
    </div>
  );
}
