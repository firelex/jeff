import type { ButtonHTMLAttributes, ReactNode } from 'react';
import { Spinner } from './spinner';

export type ButtonVariant = 'primary' | 'ghost' | 'pending';

export interface ButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'children'> {
  variant?: ButtonVariant;
  size?: 'sm';
  /** True while an action triggered by this button is in flight: disables it and shows a spinner. */
  pending?: boolean;
  children?: ReactNode;
}

// `.btn`, `.btn.ghost` and `.btn.pend` from the deck (jeff-pitches/main/pitch-v2.html).
const VARIANT_CLASS: Record<ButtonVariant, string> = {
  primary: 'bg-primary text-white shadow-violet hover:bg-primary-dark',
  ghost: 'bg-white text-primary-dark border border-primary-border shadow-none hover:bg-primary-light',
  pending: 'bg-white text-mute border border-dashed border-[#c9c9d3] shadow-none hover:border-primary-border hover:text-primary-dark',
};

export function Button({
  variant = 'primary',
  size,
  pending = false,
  disabled,
  className = '',
  children,
  type = 'button',
  ...rest
}: ButtonProps) {
  const isDisabled = disabled || pending;
  return (
    <button
      type={type}
      disabled={isDisabled}
      aria-busy={pending || undefined}
      className={[
        'inline-flex items-center justify-center gap-2 rounded-button font-semibold',
        'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary',
        'disabled:pointer-events-none disabled:opacity-60',
        size === 'sm' ? 'px-2.5 py-1 text-xs' : 'px-3.5 py-2 text-sm',
        VARIANT_CLASS[variant],
        className,
      ].join(' ')}
      {...rest}
    >
      {pending && <Spinner className="size-3.5" />}
      {children}
    </button>
  );
}
