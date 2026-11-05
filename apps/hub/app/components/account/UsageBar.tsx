// This period's input tokens against the allowance: an ink-framed bar filled in violet, then the numbers and the
// reset date in words. jeff-server may count a little past the allowance (the call that crosses it is answered), so
// the meter's value is held at its maximum and the words say the allowance is used up.
export function UsageBar({ used, allowance, resets }: { used: number; allowance: number; resets: string }) {
  const usedUp = used >= allowance;
  const shown = Math.min(used, allowance);
  const counted = `${used.toLocaleString('en-US')} of ${allowance.toLocaleString('en-US')} input tokens used`;
  const words = usedUp
    ? `${counted}: your allowance is used up, and calls are refused until it resets on ${resets}.`
    : `${counted}. Resets on ${resets}.`;
  return (
    <div className="mt-4">
      <div role="meter" aria-label="Input tokens used this period" aria-valuemin={0} aria-valuemax={allowance} aria-valuenow={shown}
        aria-valuetext={words} className="h-6 w-full border-2 border-ink bg-background p-0.5">
        <div className="h-full bg-primary" style={{ width: `${((shown / allowance) * 100).toFixed(2)}%` }} />
      </div>
      <p className={`mt-3 text-sm ${usedUp ? 'font-medium text-danger' : 'text-soft'}`}>{words}</p>
    </div>
  );
}
