import { useRef, type KeyboardEvent, type ReactNode } from 'react';

export interface TabItem {
  value: string;
  label: ReactNode;
}

export interface TabsProps {
  /** A page-unique base for the tab and panel ids (React's useId() gives one); see tabPanelProps. */
  id: string;
  items: TabItem[];
  value: string;
  onChange: (value: string) => void;
  className?: string;
}

function tabId(id: string, value: string): string {
  return `${id}-tab-${value}`;
}

function panelId(id: string, value: string): string {
  return `${id}-panel-${value}`;
}

/** The attributes for the panel a tab shows: role="tabpanel", the id its tab's aria-controls names, and
 * aria-labelledby pointing back at that tab. `id` is the same base the Tabs were given. */
export function tabPanelProps(id: string, value: string): { role: 'tabpanel'; id: string; 'aria-labelledby': string } {
  return { role: 'tabpanel', id: panelId(id, value), 'aria-labelledby': tabId(id, value) };
}

/**
 * A row of tabs; the active one takes the deck's violet gradient fill.
 *
 * Follows the WAI-ARIA Tabs pattern's "automatic activation" keyboard model:
 * ArrowLeft/ArrowRight move focus to the adjacent tab (wrapping at the ends) and select
 * it immediately; Home/End jump to the first/last tab and select it; only the selected
 * tab is in the normal Tab-key sequence (roving `tabIndex`), the rest are `-1`.
 */
export function Tabs({ id, items, value, onChange, className = '' }: TabsProps) {
  const buttons = useRef<Record<string, HTMLButtonElement | null>>({});

  function selectAndFocus(index: number) {
    const item = items[index];
    if (!item) return;
    onChange(item.value);
    buttons.current[item.value]?.focus();
  }

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    switch (event.key) {
      case 'ArrowRight':
        event.preventDefault();
        selectAndFocus((index + 1) % items.length);
        break;
      case 'ArrowLeft':
        event.preventDefault();
        selectAndFocus((index - 1 + items.length) % items.length);
        break;
      case 'Home':
        event.preventDefault();
        selectAndFocus(0);
        break;
      case 'End':
        event.preventDefault();
        selectAndFocus(items.length - 1);
        break;
      default:
        break;
    }
  }

  return (
    <div role="tablist" className={['inline-flex gap-1 rounded-nav bg-background p-1', className].join(' ')}>
      {items.map((item, index) => {
        const active = item.value === value;
        return (
          <button
            key={item.value}
            ref={(el) => {
              buttons.current[item.value] = el;
            }}
            type="button"
            role="tab"
            id={tabId(id, item.value)}
            aria-controls={panelId(id, item.value)}
            aria-selected={active}
            tabIndex={active ? 0 : -1}
            onClick={() => onChange(item.value)}
            onKeyDown={(event) => handleKeyDown(event, index)}
            className={[
              'rounded-nav px-3 py-1.5 text-sm font-medium transition-colors',
              active ? 'bg-grad text-white shadow-nav' : 'text-mute hover:bg-white hover:text-text',
            ].join(' ')}
          >
            {item.label}
          </button>
        );
      })}
    </div>
  );
}
