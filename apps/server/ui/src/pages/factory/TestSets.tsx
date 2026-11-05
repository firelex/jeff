import { useCallback, useEffect, useId, useMemo, useState } from 'react';
import { Check, Lock, Pencil, Trash2 } from 'lucide-react';
import { Alert, Badge, Button, Select, Spinner, Tabs, Textarea, tabPanelProps } from '@jeff/ui';
import { shownError, type ShownError } from '../../api';
import { ErrorAlert } from '../../components/ErrorAlert';
import type { Call, FactoryApi, TestItem, TestSet, TestSetSummary } from './api';

export function formatCall(call: Call | null | undefined): string {
  if (!call) return '(no call)';
  const args = Object.entries(call.arguments).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(', ');
  return `${call.function}(${args})`;
}

type Queue = 'open' | 'sample' | 'reviewed';

function Loading({ children }: { children: string }) {
  return <div className="flex items-center gap-2 text-sm text-mute"><Spinner /> {children}</div>;
}

// Review an independent test set before it is frozen: every disputed item and cookbook command needs a verdict, and
// at least the minimum number of items must be checked. Agreed items are offered in a fixed random order to sample.
export function TestSets({ factory }: { factory: FactoryApi }) {
  const [sets, setSets] = useState<TestSetSummary[] | null>(null);
  const [name, setName] = useState<string>('');
  const [set, setSet] = useState<TestSet | null>(null);
  const [queue, setQueue] = useState<Queue>('open');
  const [error, setError] = useState<ShownError | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const queueTabs = useId();

  useEffect(() => {
    factory.testsets().then((r) => {
      setSets(r.testsets);
      if (r.testsets.length) setName((current) => current || r.testsets[0].name);
    }, (failure: unknown) => setError(shownError(failure)));
  }, [factory]);
  const load = useCallback(() => {
    if (!name) return;
    factory.testset(name).then(setSet, (failure: unknown) => setError(shownError(failure)));
  }, [factory, name]);
  useEffect(load, [load]);

  const order = useMemo(() => {
    // a fixed shuffle of the agreed items, so the sample is not the writer's order (grouped by function)
    if (set === null) return [];
    return set.candidates.filter((c) => c.status === 'agreed').sort((a, b) => hash(a.id) - hash(b.id));
  }, [set]);
  if (!sets) return error ? <ErrorAlert error={error} /> : <Loading>Loading test sets</Loading>;
  if (!sets.length) return <p className="m-0 text-sm text-mute">No test sets on this server yet.</p>;

  const decide = async (item: TestItem, verdict: 'confirm' | 'correct' | 'drop', call?: Call) => {
    setBusy(item.id);
    setError(null);
    try {
      await factory.review(name, { item_id: item.id, verdict, ...(call ? { call } : {}) });
      load();
    } catch (failure) {
      setError(shownError(failure));
    } finally {
      setBusy(null);
    }
  };
  const freeze = async () => {
    setBusy('freeze');
    setError(null);
    try {
      await factory.freeze(name);
      load();
    } catch (failure) {
      setError(shownError(failure));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="flex flex-col gap-5">
      <div className="w-96">
        <Select aria-label="Test set" value={name} onChange={(e) => { setSet(null); setName(e.target.value); }}>
          {sets.map((s) => <option key={s.name} value={s.name}>{s.name} · {s.items} items{s.frozen ? ' · frozen' : ''}</option>)}
        </Select>
      </div>
      {error && <ErrorAlert error={error} />}
      {!set ? <Loading>Loading the test set</Loading> : (
        <Loaded set={set} order={order} queue={queue} queueTabs={queueTabs} busy={busy} onQueue={setQueue} onDecide={decide} onFreeze={freeze} />
      )}
    </div>
  );
}

function Loaded({ set, order, queue, queueTabs, busy, onQueue, onDecide, onFreeze }: {
  set: TestSet; order: TestItem[]; queue: Queue; queueTabs: string; busy: string | null; onQueue: (queue: Queue) => void;
  onDecide: (item: TestItem, verdict: 'confirm' | 'correct' | 'drop', call?: Call) => void; onFreeze: () => void;
}) {
  const reviews = set.reviews;
  const open = set.candidates.filter((c) => c.status !== 'agreed' && !reviews[c.id]);
  const sample = order.filter((c) => !reviews[c.id]);
  const reviewed = set.candidates.filter((c) => reviews[c.id]);
  const shown = queue === 'open' ? open : queue === 'sample' ? sample.slice(0, 20) : reviewed;
  const frozen = set.frozen;
  return (
    <>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <Stat label="Items" value={set.candidates.length} />
        <Stat label="Writer and judge agree" value={set.candidates.filter((c) => c.status === 'agreed').length} />
        <Stat label="Need your verdict" value={open.length} note="disputed, and the cookbook's commands" />
        <Stat label="Checked by you" value={Object.keys(reviews).length} note={`at least ${set.min_owner_checks} to freeze`} />
        <Stat label="Dropped" value={Object.values(reviews).filter((r) => r.verdict === 'drop').length} />
      </div>
      <p className="m-0 text-xs text-mute">
        Written by {String(set.summary.writer)}, checked blind by {String(set.summary.judge)}. Every call was checked against the
        functions' value lists and defaults; {String(set.summary.invalid)} invalid calls and {String(set.summary.duplicates)} duplicates were dropped before review.
      </p>
      {frozen ? (
        <Alert tone="info" title={<span className="inline-flex items-center gap-1.5"><Lock className="size-4" aria-hidden="true" />Frozen {new Date(frozen.frozen).toLocaleString()}</span>}>
          <span className="font-mono text-xs">{frozen.items} items, {frozen.checked_by_owner} checked by you · sha256 {frozen.sha256}</span>
        </Alert>
      ) : (
        <>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <Tabs id={queueTabs} value={queue} onChange={(value) => onQueue(value as Queue)} items={[
              { value: 'open', label: `Needs your verdict (${open.length})` },
              { value: 'sample', label: `Check a sample (${sample.length} left)` },
              { value: 'reviewed', label: `Reviewed (${reviewed.length})` },
            ]} />
            <Button pending={busy === 'freeze'} disabled={open.length > 0 || Object.keys(reviews).length < set.min_owner_checks} onClick={onFreeze}>
              <Lock className="size-4" aria-hidden="true" />Freeze the test set
            </Button>
          </div>
          <div {...tabPanelProps(queueTabs, queue)} className="divide-y divide-line overflow-hidden rounded-card border border-line bg-panel">
            {shown.length === 0 && <p className="m-0 px-5 py-6 text-sm text-mute">Nothing here.</p>}
            {shown.map((item) => <ItemRow key={item.id} item={item} review={reviews[item.id]} busy={busy === item.id} onDecide={onDecide} />)}
          </div>
        </>
      )}
    </>
  );
}

function hash(text: string): number {
  let h = 2166136261;
  for (const char of text) h = Math.imul(h ^ char.charCodeAt(0), 16777619);
  return h >>> 0;
}

function Stat({ label, value, note }: { label: string; value: number; note?: string }) {
  return (
    <div className="rounded-card border border-line bg-panel px-3 py-2">
      <p className="m-0 text-xs text-mute">{label}</p>
      <p className="m-0 font-mono text-xl font-semibold text-text">{value}</p>
      {note && <p className="m-0 text-xs text-mute">{note}</p>}
    </div>
  );
}

const DANGER = 'border-danger/40 text-danger hover:bg-danger/10';

function ItemRow({ item, review, busy, onDecide }: {
  item: TestItem; review?: { verdict: string; call: Call | null }; busy: boolean;
  onDecide: (item: TestItem, verdict: 'confirm' | 'correct' | 'drop', call?: Call) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(JSON.stringify(item.writer_call, null, 1));
  const [parseError, setParseError] = useState<string | null>(null);
  const differs = item.judge_call && formatCall(item.judge_call) !== formatCall(item.writer_call);
  return (
    <div className="flex flex-col gap-2 px-5 py-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs text-mute">{item.id}</span>
        <Badge tone={item.status === 'agreed' ? 'ok' : item.status === 'cookbook' ? 'info' : 'proprietary'}>{item.status}</Badge>
        <Badge tone="neutral">{item.pattern}</Badge>
        {review && <Badge tone="brand">you: {review.verdict}</Badge>}
      </div>
      <p className="m-0 text-base text-text">{item.command}</p>
      <p className="m-0 font-mono text-xs text-text"><span className="text-mute">{item.status === 'cookbook' ? 'cookbook: ' : 'writer: '}</span>{formatCall(review !== undefined && review.call !== null ? review.call : item.writer_call)}</p>
      {differs && <p className="m-0 font-mono text-xs text-text"><span className="text-mute">judge: </span>{formatCall(item.judge_call)}</p>}
      {item.judge_error && <p className="m-0 text-xs text-danger">judge failed: {item.judge_error.slice(0, 160)}</p>}
      {editing && (
        <div className="flex flex-col gap-1">
          <Textarea rows={5} className="font-mono text-xs" aria-label={`Corrected call for ${item.id}`} value={text} onChange={(e) => setText(e.target.value)} />
          {parseError && <p className="m-0 text-xs text-danger">{parseError}</p>}
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        <Button size="sm" pending={busy} onClick={() => onDecide(item, 'confirm')}>
          <Check className="size-4" aria-hidden="true" />Confirm{differs ? " writer's" : ''}
        </Button>
        {differs && item.judge_call && (
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => onDecide(item, 'correct', item.judge_call as Call)}>Use judge's</Button>
        )}
        {!editing ? (
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => setEditing(true)}><Pencil className="size-4" aria-hidden="true" />Correct</Button>
        ) : (
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => {
            let call: Call;
            try {
              call = JSON.parse(text) as Call;
            } catch (failure) {
              setParseError(`The correction is not JSON: ${(failure as Error).message}`);
              return;
            }
            setParseError(null);
            onDecide(item, 'correct', call);
            setEditing(false);
          }}>Save correction</Button>
        )}
        <Button size="sm" variant="ghost" disabled={busy} className={DANGER} onClick={() => onDecide(item, 'drop')}>
          <Trash2 className="size-4" aria-hidden="true" />Drop
        </Button>
      </div>
    </div>
  );
}
