// The playground's live answer (POST /api/playground/decide, a run): the decision's value, its confidence, every
// option's probability (highest first, each with a bar), the time the call took and whose run it was.
import type { ExampleBlock } from '../../lib/example-block';
import type { SdkLanguage } from '../../lib/sdk-languages';
import { CARD } from '../styles';

// A run as the endpoint answers it (app/lib/playground.server.ts: PlaygroundAnswer). `decision` is the API's decision
// object (contract/schemas/decision.schema.json); the page reads its value, confidence, abstained and probabilities.
// `local` is the same call "On your machine" for the input run, as every other widget's (examples.server.ts:
// localCallOf); null for a proprietary adapter or a via-http sample, which offer no choice.
export interface RunAnswer {
  ran_as: 'visitor' | 'account';
  latency_ms: number;
  decision: { value: string | boolean | number; confidence: number | null; abstained: boolean; probabilities: Record<string, number> };
  code: { language: SdkLanguage; code: string }[];
  curl: string;
  local: ExampleBlock['local'];
}

const LABEL = 'font-mono text-xs uppercase tracking-wide text-mute';

export function Result({ answer }: { answer: RunAnswer }) {
  const { decision } = answer;
  const options = Object.entries(decision.probabilities).sort(([, a], [, b]) => b - a);
  return (
    <section aria-label="Answer" className={`${CARD} p-4 sm:p-5`}>
      <p className={LABEL}>Answer</p>
      <p className="mt-1 break-words font-pixel text-pixel-30 text-text">{String(decision.value)}</p>
      <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 font-mono text-sm text-soft">
        <span>{decision.confidence === null ? 'No confidence' : `Confidence ${decision.confidence}`}</span>
        <span>{`${answer.latency_ms} ms`}</span>
        <span>{answer.ran_as === 'account' ? 'Ran on your account' : 'Ran as a visitor'}</span>
      </div>
      {decision.abstained && <p className="mt-2 text-sm text-soft">The adapter abstained: it was not confident enough to decide.</p>}
      {options.length > 0 && (
        <>
          <p className={`mt-4 ${LABEL}`}>Every option</p>
          <ul className="mt-2 space-y-1.5">
            {options.map(([option, probability]) => (
              <li key={option} className="grid grid-cols-[minmax(0,1fr)_4.5rem_3.5rem] items-center gap-3 font-mono text-xs text-text sm:grid-cols-[minmax(0,1fr)_8rem_3.5rem]">
                <span className="min-w-0 [overflow-wrap:anywhere]">{option}</span>
                <span className="h-2 border border-ink bg-background" aria-hidden="true">
                  <span className="block h-full bg-primary" style={{ width: `${Math.round(probability * 100)}%` }} />
                </span>
                <span className="text-right text-soft">{probability.toFixed(3)}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
