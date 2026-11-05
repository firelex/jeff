import type { SelectHTMLAttributes } from 'react';
import { ChevronDown } from 'lucide-react';

export interface SelectProps extends SelectHTMLAttributes<HTMLSelectElement> {}

/** A styled native `<select>` — no custom listbox, so it keeps native keyboard and a11y behaviour. */
export function Select({ className = '', children, ...rest }: SelectProps) {
  return (
    <div className="relative inline-flex w-full items-center">
      <select
        className={[
          'w-full appearance-none rounded-button border border-line bg-panel px-3 py-2 pr-8 text-sm text-text',
          'focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary/20 disabled:cursor-not-allowed disabled:opacity-60',
          className,
        ].join(' ')}
        {...rest}
      >
        {children}
      </select>
      <ChevronDown aria-hidden="true" className="pointer-events-none absolute right-2.5 size-4 text-mute" />
    </div>
  );
}
