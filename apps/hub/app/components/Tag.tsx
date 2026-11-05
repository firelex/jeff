// A small bordered mono label: Open / API only on the catalogue, an adapter's interfaces, an API operation's method.
// The hub's own, rather than @jeff/ui's Badge, whose filled colours belong to the Jeff Console's look.
import type { ReactNode } from 'react';

export type TagTone = 'ink' | 'accent' | 'quiet' | 'open';

const TONE: Record<TagTone, string> = {
  ink: 'border-ink text-ink',
  accent: 'border-primary text-primary',
  open: 'border-open text-open',
  quiet: 'border-rule text-mute',
};

export function Tag({ tone = 'ink', children }: { tone?: TagTone; children: ReactNode }) {
  return (
    <span className={`inline-block whitespace-nowrap border px-1.5 py-px font-mono text-[11px] leading-4 ${TONE[tone]}`}>
      {children}
    </span>
  );
}
