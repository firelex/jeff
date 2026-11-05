// The answer to a POST /v1/systemone request (contract/schemas/systemone_response.schema.json): the timings, each
// question's answer with the probability of every option, and the response and request as JSON.
import { useId, useState } from 'react';
import { Badge, Button, Tabs, tabPanelProps } from '@jeff/ui';
import type { Json, SystemOneAnswer, SystemOneResponse, Timed } from '../api';
import { milliseconds, percent } from '../format';
import { ProbabilityBar } from './ProbabilityBar';

const SHOWN_OPTIONS = 6;

function describe(value: Json | undefined): string | undefined {
  if (value === null || value === undefined) return undefined;
  return typeof value === 'string' ? value : JSON.stringify(value);
}

function Metric({ label, value, unit, note }: { label: string; value: string; unit?: string; note: string }) {
  return (
    <div className="min-w-0 rounded-card border border-line bg-panel px-4 py-3 shadow-card" title={note}>
      <p className="text-xs text-mute">{label}</p>
      <p className="mt-1 truncate font-mono text-2xl font-semibold tracking-tight text-text">
        {value}
        {unit && <span className="ml-1 text-sm font-normal text-mute">{unit}</span>}
      </p>
    </div>
  );
}

function AnswerCard({ name, answer }: { name: string; answer: SystemOneAnswer }) {
  const [expanded, setExpanded] = useState(false);
  let headline: string;
  let rows: [string, number][];
  let chosen: string;
  if (answer.type === 'noul') {
    headline = answer.noul >= 0.5 ? 'True' : 'False';
    rows = [['true', answer.noul], ['false', 1 - answer.noul]];
    chosen = answer.noul >= 0.5 ? 'true' : 'false';
  } else if (answer.type === 'choice') {
    headline = answer.choice;
    rows = Object.entries(answer.probabilities).sort((a, b) => b[1] - a[1]);
    chosen = answer.choice;
  } else {
    const levels = Object.keys(answer.legend).length;
    headline = `${answer.score.toFixed(2)} of ${levels - 1}`;
    rows = Object.entries(answer.probabilities);
    chosen = rows.reduce((best, row) => (row[1] > best[1] ? row : best))[0];
  }
  const visible = expanded ? rows : rows.slice(0, SHOWN_OPTIONS);
  const legend = answer.type === 'score' ? answer.legend : undefined;
  return (
    <div className="flex flex-col gap-3 border-b border-line px-5 py-4 last:border-b-0">
      <div className="flex items-center justify-between gap-3">
        <h3 className="truncate font-mono text-sm text-mute">{name}</h3>
        <Badge tone="neutral">{answer.type}</Badge>
      </div>
      <div className="flex items-baseline justify-between gap-3">
        <p className="truncate text-xl font-semibold text-text">{headline}</p>
        {answer.type !== 'noul' && <span className="shrink-0 text-xs text-mute">Confidence {percent(answer.confidence)}</span>}
      </div>
      <div className="flex flex-col gap-2">
        {visible.map(([label, probability]) => {
          const meaning = describe(legend?.[label]);
          return <ProbabilityBar key={label} label={meaning ? `${label} · ${meaning}` : label} probability={probability} chosen={label === chosen} title={meaning} />;
        })}
      </div>
      {rows.length > SHOWN_OPTIONS && (
        <div>
          <Button variant="ghost" size="sm" onClick={() => setExpanded(!expanded)}>
            {expanded ? 'Show fewer options' : `Show all ${rows.length} options`}
          </Button>
        </div>
      )}
    </div>
  );
}

export function SystemOneResult({ result, requestJson }: { result: Timed<SystemOneResponse>; requestJson: string }) {
  const [tab, setTab] = useState('answers');
  const tabs = useId();
  const response = result.body;
  return (
    <div className="flex flex-col gap-5">
      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        <Metric label="Server time" value={milliseconds(response.timing.server_ms)} unit="ms"
          note="timing.server_ms: from the request reaching the handler to the response being ready" />
        <Metric label="Round trip" value={milliseconds(result.roundTripMs)} unit="ms" note="Measured in this browser: network plus server" />
        <Metric label="Input tokens" value={response.usage.input_tokens.toLocaleString('en-GB')} note="Prompt tokens of every pass" />
        <Metric label="Orders" value={String(response.usage.orders)} note="2: each question answered twice, options reversed the second time" />
      </div>
      <Tabs id={tabs} items={[{ value: 'answers', label: 'Answers' }, { value: 'response', label: 'Response JSON' }, { value: 'request', label: 'Request JSON' }]}
        value={tab} onChange={setTab} />
      {tab === 'answers' && (
        <div {...tabPanelProps(tabs, 'answers')} className="flex flex-col gap-2">
          <div className="overflow-hidden rounded-card border border-line bg-panel shadow-card">
            {Object.entries(response.answers).map(([name, answer]) => <AnswerCard key={name} name={name} answer={answer} />)}
          </div>
          <p className="text-xs text-mute">
            Answered by <span className="font-mono">{response.model}</span> · request id <span className="font-mono">{result.requestId}</span>
          </p>
        </div>
      )}
      {tab === 'response' && (
        <pre {...tabPanelProps(tabs, 'response')} className="max-h-[60vh] overflow-auto rounded-card border border-line bg-panel p-4 font-mono text-xs leading-relaxed">
          {JSON.stringify(response, null, 2)}
        </pre>
      )}
      {tab === 'request' && (
        <pre {...tabPanelProps(tabs, 'request')} className="max-h-[60vh] overflow-auto rounded-card border border-line bg-panel p-4 font-mono text-xs leading-relaxed">{requestJson}</pre>
      )}
    </div>
  );
}
