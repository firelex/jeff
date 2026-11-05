import type { ReactNode } from 'react';

export interface EmptyStateProps {
  icon?: ReactNode;
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
}

/** A centred placeholder for a list or page with nothing in it yet. */
export function EmptyState({ icon, title, description, action, className = '' }: EmptyStateProps) {
  return (
    <div className={['flex flex-col items-center justify-center gap-2 py-12 text-center', className].join(' ')}>
      {icon && <div aria-hidden="true" className="mb-1 text-faint">{icon}</div>}
      <p className="text-sm font-semibold text-text">{title}</p>
      {description && <p className="max-w-sm text-sm text-mute">{description}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}
