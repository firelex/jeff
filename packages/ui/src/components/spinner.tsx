import { Loader2 } from 'lucide-react';

export interface SpinnerProps {
  className?: string;
  label?: string;
}

/** A small spinning indicator, used alone or inside a pending Button. */
export function Spinner({ className = 'size-4', label = 'Loading' }: SpinnerProps) {
  return <Loader2 role="status" aria-label={label} className={`animate-spin ${className}`} />;
}
