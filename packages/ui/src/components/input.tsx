import type { InputHTMLAttributes } from 'react';

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {}

const BASE =
  'w-full rounded-button border border-line bg-panel px-3 py-2 text-sm text-text placeholder:text-faint ' +
  'focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary/20 disabled:cursor-not-allowed disabled:opacity-60';

export function Input({ className = '', ...rest }: InputProps) {
  return <input className={[BASE, className].join(' ')} {...rest} />;
}
