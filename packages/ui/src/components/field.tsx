import type { ReactNode } from 'react';

export interface FieldProps {
  label?: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  htmlFor?: string;
  children: ReactNode;
  className?: string;
}

/** Wraps a form control with a consistent label, hint and error row. */
export function Field({ label, hint, error, htmlFor, children, className = '' }: FieldProps) {
  return (
    <div className={['flex flex-col gap-1.5', className].join(' ')}>
      {label && (
        <label htmlFor={htmlFor} className="text-sm font-medium text-text">
          {label}
        </label>
      )}
      {children}
      {error ? <p className="text-xs text-danger">{error}</p> : hint ? <p className="text-xs text-mute">{hint}</p> : null}
    </div>
  );
}
