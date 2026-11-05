import { percent } from '../format';

/** One option's probability as a bar; the chosen option's bar is the deck's violet. */
export function ProbabilityBar({ label, probability, chosen, title }: { label: string; probability: number; chosen: boolean; title?: string }) {
  const width = Math.max(0, Math.min(1, probability)) * 100;
  return (
    <div className="grid grid-cols-[minmax(0,14rem)_1fr_3.5rem] items-center gap-3 text-sm" title={title}>
      <span className={['truncate font-mono text-[13px]', chosen ? 'font-semibold text-text' : 'text-mute'].join(' ')}>{label}</span>
      <div role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={1} aria-valuenow={probability}
        className="h-2 overflow-hidden rounded-pill bg-[#f1f1f4]">
        <div className={['h-full rounded-pill', chosen ? 'bg-grad' : 'bg-primary-border'].join(' ')} style={{ width: `${width}%` }} />
      </div>
      <span className="text-right font-mono text-xs text-mute">{percent(probability)}</span>
    </div>
  );
}
