// One Decision from POST /v1/decide (contract/schemas/decision.schema.json): its value, confidence, whether it is
// calibrated or abstained, the probability of each option, the route a dispatch took, any escalations, the timing
// the engine reported, and the request id.
import type { ReactNode } from 'react';
import { Badge } from '@jeff/ui';
import type { Decision, Value } from '../api';
import { milliseconds, percent } from '../format';
import { ProbabilityBar } from './ProbabilityBar';

function shownValue(value: Value): string {
  return typeof value === 'string' ? value : String(value);
}

function timingValue(key: string, value: unknown): string {
  if (typeof value === 'number') return key.endsWith('_ms') ? `${milliseconds(value)} ms` : String(value);
  return JSON.stringify(value);
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-2">
      <p className="text-[10.5px] font-bold uppercase tracking-[0.14em] text-mute">{title}</p>
      {children}
    </div>
  );
}

export interface DecisionViewProps {
  decision: Decision;
  requestId: string;
  roundTripMs: number;
  title?: string;
}

export function DecisionView({ decision, requestId, roundTripMs, title }: DecisionViewProps) {
  // A score's options are its levels in order; any other function's options are shown most likely first.
  const options = Object.entries(decision.probabilities);
  if (decision.score === null) options.sort((a, b) => b[1] - a[1]);
  const chosen = shownValue(decision.value);
  const timing = Object.entries(decision.timing);

  return (
    <article className="flex flex-col gap-5 rounded-card border border-line bg-panel p-5 shadow-card">
      <header className="flex flex-col gap-2">
        {title && <p className="font-mono text-xs text-mute">{title}</p>}
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone="neutral">{decision.interface}</Badge>
          <span className="font-mono text-xs text-soft">{decision.decided_by}</span>
        </div>
        <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
          <p data-testid="decision-value" className="font-mono text-2xl font-semibold tracking-tight text-text">{chosen}</p>
          {decision.score !== null && <span className="text-sm text-soft">score {decision.score.toFixed(2)}</span>}
        </div>
        <div className="flex flex-wrap items-center gap-2 text-sm text-soft">
          <span>
            Confidence{' '}
            <b data-testid="decision-confidence" className="font-semibold text-text">{decision.confidence === null ? 'none reported' : percent(decision.confidence)}</b>
          </span>
          {decision.calibrated ? <Badge tone="ok">Calibrated</Badge> : <Badge tone="empty">Not calibrated</Badge>}
          {decision.abstained && <Badge tone="preview">Abstained</Badge>}
        </div>
        {decision.abstained && (
          <p className="rounded-button border border-[#fde68a] bg-[#fffbeb] px-3 py-2 text-sm text-warning">
            Jeff abstained: treat this as no decision, and escalate it or ask a person.
          </p>
        )}
      </header>

      <Section title="Probabilities">
        <div className="flex flex-col gap-2">
          {options.map(([label, probability]) => (
            <ProbabilityBar key={label} label={label} probability={probability} chosen={label === chosen} />
          ))}
        </div>
      </Section>

      {decision.route.length > 0 && (
        <Section title="Route">
          <ol className="flex flex-col gap-1 text-sm">
            {decision.route.map((step, index) => (
              <li key={index} className="flex flex-wrap gap-2">
                <span className="font-mono text-[13px] text-text">{step.router}</span>
                <span className="text-mute">chose</span>
                <span className="font-mono text-[13px] text-text">{step.chose}</span>
                <span className="text-mute">({percent(step.confidence)})</span>
              </li>
            ))}
          </ol>
        </Section>
      )}

      {decision.escalations.length > 0 && (
        <Section title="Escalations">
          <ol className="flex flex-col gap-1 text-sm">
            {decision.escalations.map((step, index) => (
              <li key={index} className="flex flex-wrap gap-2">
                <span className="font-mono text-[13px] text-text">{step.decided_by}</span>
                <span className="text-mute">answered</span>
                <span className="font-mono text-[13px] text-text">{shownValue(step.value)}</span>
                <span className="text-mute">({step.confidence === null ? 'no confidence' : percent(step.confidence)}): {step.reason}</span>
              </li>
            ))}
          </ol>
        </Section>
      )}

      <Section title="Timing">
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-0.5 text-sm">
          {timing.map(([key, value]) => (
            <div key={key} className="contents">
              <dt className="font-mono text-[13px] text-mute">{key}</dt>
              <dd className="font-mono text-[13px] text-text">{timingValue(key, value)}</dd>
            </div>
          ))}
          <dt className="text-mute">Round trip</dt>
          <dd className="font-mono text-[13px] text-text">{milliseconds(roundTripMs)} ms</dd>
          <dt className="text-mute">Request id</dt>
          <dd className="font-mono text-[13px] text-text">{requestId}</dd>
        </dl>
      </Section>
    </article>
  );
}
