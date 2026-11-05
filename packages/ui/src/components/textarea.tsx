import type { TextareaHTMLAttributes } from 'react';

export interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {}

const BASE =
  'w-full min-h-[80px] rounded-button border border-line bg-panel px-3 py-2 text-sm text-text placeholder:text-faint ' +
  'focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary/20 disabled:cursor-not-allowed disabled:opacity-60';

export function Textarea({ className = '', ...rest }: TextareaProps) {
  return <textarea className={[BASE, className].join(' ')} {...rest} />;
}
