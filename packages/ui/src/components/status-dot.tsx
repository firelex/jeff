import type { ReactNode } from 'react';

export type Status = 'ready' | 'degraded' | 'broken' | 'unknown';

export interface StatusDotProps {
  status: Status;
  label?: ReactNode;
  className?: string;
}

const STATUS_CLASS: Record<Status, string> = {
  ready: 'bg-success',
  degraded: 'bg-warning',
  broken: 'bg-danger',
  unknown: 'bg-faint',
};

/** A small coloured dot plus label for a service's health. */
export function StatusDot({ status, label, className = '' }: StatusDotProps) {
  return (
    <span className={['inline-flex items-center gap-2 text-xs text-soft', className].join(' ')}>
      <span data-testid={`status-dot-${status}`} className={['inline-block size-2 rounded-status', STATUS_CLASS[status]].join(' ')} />
      {label}
    </span>
  );
}
