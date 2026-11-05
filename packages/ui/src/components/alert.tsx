import type { ReactNode } from 'react';

export type AlertTone = 'error' | 'warning' | 'info';

export interface AlertProps {
  tone: AlertTone;
  title?: ReactNode;
  children?: ReactNode;
  className?: string;
}

// `error` and `warning` use the deck's danger/warning accents; `info` reuses the
// violet accent card (`.card.accent`: background var(--vl), border var(--vb)).
const TONE_CLASS: Record<AlertTone, string> = {
  error: 'border-danger/30 bg-danger/10 text-danger',
  warning: 'border-warning/30 bg-warning/10 text-warning',
  info: 'border-primary-border bg-primary-light text-primary-dark',
};

/**
 * Surfaces a failure or notice: an error or a warning is role="alert" (announced at once), a notice (info) is
 * role="status" (announced when the reader is idle). A failed API call renders its server-given
 * `type` as the title and its `message` as the body — never a made-up default.
 */
export function Alert({ tone, title, children, className = '' }: AlertProps) {
  return (
    <div role={tone === 'info' ? 'status' : 'alert'} className={['rounded-card border p-3 text-sm', TONE_CLASS[tone], className].join(' ')}>
      {title && <p className="font-semibold">{title}</p>}
      {children && <p className="mt-0.5 text-soft">{children}</p>}
    </div>
  );
}
