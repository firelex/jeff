// An adapter page's "How it was measured" (content/adapter-numbers.yaml, owner-approved layout of 4 November 2026):
// the test figures, a public-benchmark or real-document one leading where there is one, each with what its test set
// is; per function, how often it is right when confident, how often it is confident, and from which confidence (the
// manifest's calibration); the caveats of the data notes in one line each; and, where a figure is missing, a sentence
// saying so rather than nothing.
import type { AdapterNumbers, TestKind } from '../../lib/adapter-numbers.server';
import { TABLE_FRAME, TD, TH } from '../styles';
import { Tag } from '../Tag';

export const KIND_LABEL: Record<TestKind, string> = {
  public: 'public benchmark',
  real: 'held-out real documents',
  generator: 'in-house: same generator as the training data',
  mixed: 'mixed sources',
  unknown: 'test-set kind not recorded',
};

// An accuracy as recorded (95.95 stays 95.95), a whole number with one decimal (100 -> 100.0).
export function accuracyText(accuracy: number): string {
  return `${Number.isInteger(accuracy) ? accuracy.toFixed(1) : String(accuracy)}%`;
}

// A share (0 to 1) as a percentage with one decimal, or two where one would round a share below 1 up to 100.0.
export function shareText(share: number): string {
  const one = (share * 100).toFixed(1);
  return `${one === '100.0' && share < 1 ? (share * 100).toFixed(2) : one}%`;
}

// A threshold with two decimals, or as many more as it takes not to round one below 1 up to 1.00.
export function thresholdText(threshold: number): string {
  for (let digits = 2; digits < 6; digits++) {
    const text = threshold.toFixed(digits);
    if (threshold === 1 || Number(text) < 1) return text;
  }
  return String(threshold);
}

const rowsText = (rows: number) => `${rows.toLocaleString('en-US')} rows`;

const DATE = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC' });

export function Measured({ numbers }: { numbers: AdapterNumbers }) {
  const { tests, noAccuracy, confident, noCalibration, caveats, base, date } = numbers;
  return (
    <div data-measured>
      {noAccuracy !== null ? (
        <p className="mt-3 text-sm leading-6 text-soft">Test accuracy: not recorded. {noAccuracy}</p>
      ) : (
        <>
          <p className="mt-3 text-sm leading-6 text-soft">Test accuracy: the share of test rows whose top answer was right.</p>
          <ul className="mt-2 space-y-2">
            {tests.map((t, i) => (
              <li key={`${t.set}-${i}`} className="text-sm leading-6 text-soft" data-test-figure={t.kind}>
                <span className={i === 0 ? 'font-pixel text-pixel-20 text-text' : 'font-mono text-text'}>{accuracyText(t.accuracy)}</span>
                {` on ${t.set}${t.rows === null ? '' : ` (${rowsText(t.rows)})`}. `}
                <Tag tone={t.kind === 'public' || t.kind === 'real' ? 'open' : 'quiet'}>{KIND_LABEL[t.kind]}</Tag>
              </li>
            ))}
          </ul>
        </>
      )}

      <h3 className="mt-5 font-mono text-xs uppercase tracking-wide text-mute">When confident</h3>
      {confident === null ? (
        <p className="mt-2 text-sm leading-6 text-soft">{noCalibration}</p>
      ) : (
        <>
          <p className="mt-2 text-sm leading-6 text-soft">
            From the calibration set: at or above the threshold confidence, how often the answer is right, and how often
            the adapter is that confident. Below it, escalate.
          </p>
          <div className={`mt-3 ${TABLE_FRAME}`}>
            <table className="w-full text-left text-sm">
              <thead>
                <tr>
                  <th className={TH}>Function</th>
                  <th className={TH}>Right when confident</th>
                  <th className={TH}>Confident on</th>
                  <th className={TH}>Threshold</th>
                  <th className={TH}>Calibration rows</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(confident).map(([key, c]) => (
                  <tr key={key}>
                    <td className={`${TD} font-mono text-xs text-text`}>{key}</td>
                    <td className={`${TD} font-mono text-xs text-soft`}>{shareText(c.precision)}</td>
                    <td className={`${TD} font-mono text-xs text-soft`}>{shareText(c.coverage)}</td>
                    <td className={`${TD} font-mono text-xs text-soft`}>{thresholdText(c.threshold)}</td>
                    <td className={`${TD} font-mono text-xs text-soft`}>{c.rows.toLocaleString('en-US')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {caveats.length > 0 && (
        <>
          <h3 className="mt-5 font-mono text-xs uppercase tracking-wide text-mute">Caveats</h3>
          <ul className="mt-2 list-disc space-y-1 pl-5 text-sm leading-6 text-soft">
            {caveats.map((c) => <li key={c}>{c}</li>)}
          </ul>
        </>
      )}

      <p className="mt-4 text-xs text-mute">
        Base: {base}. Adapter trained: {date === null ? 'date not recorded' : DATE.format(new Date(`${date}T00:00:00Z`))}.
      </p>
    </div>
  );
}
