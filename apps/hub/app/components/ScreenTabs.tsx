import { screenTabClass } from './styles';

// The tabs on a screen (the home page's language switch, a worked example's languages and curl): one tab stop,
// the selected tab; Left and Right arrows move to the previous and next tab (wrapping), Home and End to the first and
// last, and moving selects (the "automatic activation" tab pattern of the ARIA authoring practices). Tab ids are
// `${idBase}-tab-${value}`, so a panel can name the selected one in aria-labelledby; `controls` is the id (or
// space-separated ids) of the panel or panels the tabs switch. The tabs never wrap (screenTabClass's small pixel
// size fits six languages, curl and Copy on one row at every width they used to wrap at); at a phone's 375 px they
// may still not all fit, so the default className lets this box alone scroll sideways (min-w-0 so it can shrink
// inside its row, overflow-x-auto so the overflow scrolls here, never the page).
export function ScreenTabs<T extends string>({ idBase, tabs, selected, onSelect, controls, className = 'flex min-w-0 flex-nowrap gap-1 overflow-x-auto' }: {
  idBase: string;
  tabs: { value: T; label: string }[];
  selected: T;
  onSelect: (value: T) => void;
  controls: string;
  className?: string;
}) {
  const move = (fromIndex: number, key: string): number | null => {
    if (key === 'ArrowLeft') return (fromIndex + tabs.length - 1) % tabs.length;
    if (key === 'ArrowRight') return (fromIndex + 1) % tabs.length;
    if (key === 'Home') return 0;
    if (key === 'End') return tabs.length - 1;
    return null;
  };
  return (
    <div role="tablist" aria-label="Language" className={className}>
      {tabs.map((tab, index) => (
        <button
          key={tab.value}
          type="button"
          role="tab"
          id={`${idBase}-tab-${tab.value}`}
          aria-selected={tab.value === selected}
          aria-controls={controls}
          tabIndex={tab.value === selected ? 0 : -1}
          onClick={() => onSelect(tab.value)}
          onKeyDown={(event) => {
            const nextIndex = move(index, event.key);
            if (nextIndex === null) return;
            event.preventDefault();
            const next = tabs[nextIndex]!.value;
            onSelect(next);
            document.getElementById(`${idBase}-tab-${next}`)?.focus();
          }}
          className={screenTabClass(tab.value === selected)}
        >
          {tab.label}
        </button>
      ))}
    </div>
  );
}
