import type { ButtonHTMLAttributes } from 'react';

export interface SwitchProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'onChange' | 'role' | 'type'> {
  checked: boolean;
  onChange: (checked: boolean) => void;
}

/** A controlled on/off toggle. */
export function Switch({ checked, onChange, disabled, className = '', ...rest }: SwitchProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={[
        'relative inline-flex h-5 w-9 shrink-0 items-center rounded-pill border border-transparent transition-colors',
        'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary',
        'disabled:cursor-not-allowed disabled:opacity-60',
        checked ? 'bg-primary' : 'bg-line',
        className,
      ].join(' ')}
      {...rest}
    >
      <span
        aria-hidden="true"
        className={['block size-4 rounded-full bg-white transition-transform', checked ? 'translate-x-[18px]' : 'translate-x-0.5'].join(' ')}
      />
    </button>
  );
}
