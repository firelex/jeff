// /admin/requests: the owner's list of adapter requests and, with ?id=<request number>, one request in full, where
// its status is set (a declined one with its reason), its typed examples are downloaded as training rows (JSON
// Lines) or its data set's zip is downloaded (through a link of the private file store that works for five
// minutes), with a manifest skeleton either way, and where it is deleted (with its data set). Once its examples were
// deleted (90 days after it was closed), the page says when in place of the training rows and the data set download. Prerendered like every page of the hub, so the shipped HTML holds nothing but the
// heading: in the browser the page asks the hub's function (GET /api/admin/adapter-requests and /<id>), which
// answers only the owner (the session's email is OWNER_EMAIL); for anyone else it answers 404, and the page says
// Not found.
import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { NotFound } from '../components/NotFound';
import { BUTTON_DANGER, BUTTON_PRIMARY, BUTTON_SECONDARY, CARD, PAGE_TITLE, SECTION_HEADING, TABLE_FRAME, TD, TEXT_LINK, TH } from '../components/styles';
import {
  ANSWER_KIND_LABELS, EXAMPLE_SOURCE_LABELS, LIMITS, megabytes, REQUEST_STATUSES, STATUS_LABELS,
  type AdminRequestDetail, type AdminRequestRow, type AdminRequests, type RequestStatus,
} from '../lib/adapter-request';
import { askHub, refusalOf } from '../lib/ask-hub';
import { longDate } from '../lib/dates';

export function meta() {
  return [{ title: 'Adapter requests | Jeff' }, { name: 'robots', content: 'noindex' }];
}

type Loaded<T> = { kind: 'loading' } | { kind: 'missing' } | { kind: 'failed'; message: string } | { kind: 'ready'; value: T };

async function load<T>(path: string, valid: (body: unknown) => body is T): Promise<Loaded<T>> {
  const answer = await askHub(path, null);
  if (answer.kind === 'answer' && answer.status === 404) return { kind: 'missing' };
  if (answer.kind === 'answer' && answer.status === 200 && valid(answer.body)) return { kind: 'ready', value: answer.body };
  return { kind: 'failed', message: refusalOf(answer) };
}

const isList = (body: unknown): body is AdminRequests => Array.isArray((body as { requests?: unknown } | null)?.requests);
const isDetail = (body: unknown): body is AdminRequestDetail => typeof (body as { request?: { id?: unknown } } | null)?.request?.id === 'string';

// What a request's examples are, in a few words: "data set: 1,234 rows, 12.3 MB", or the count of typed examples;
// "examples deleted on 3 December 2026" once they were.
export function examplesText(row: AdminRequestRow): string {
  if (row.examplesDeletedAt !== null) return examplesDeletedText(row.examplesDeletedAt);
  return row.dataSet === null
    ? row.exampleCount.toLocaleString('en-US')
    : `data set: ${row.dataSet.rows.toLocaleString('en-US')} rows, ${megabytes(row.dataSet.bytes)}`;
}

export const examplesDeletedText = (at: string) => `examples deleted on ${longDate(at)}`;

function List({ rows }: { rows: AdminRequestRow[] }) {
  if (rows.length === 0) return <p className="mt-6 text-sm text-soft">No adapter requests yet.</p>;
  return (
    <div className={`mt-6 ${TABLE_FRAME}`}>
      <table className="w-full min-w-[640px] text-left text-sm">
        <thead>
          <tr><th className={TH}>Request</th><th className={TH}>From</th><th className={TH}>Question</th><th className={TH}>Status</th><th className={TH}>Options</th><th className={TH}>Examples</th><th className={TH}>Sent</th></tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id}>
              <td className={`${TD} font-mono text-xs`}><Link to={`/admin/requests?id=${encodeURIComponent(row.id)}`} className={TEXT_LINK}>{row.id}</Link></td>
              <td className={`${TD} break-all font-mono text-xs`}>{row.email}</td>
              <td className={TD}>{row.question}</td>
              <td className={TD}>{STATUS_LABELS[row.status]}</td>
              <td className={`${TD} font-mono text-xs`}>{row.optionCount}</td>
              <td className={`${TD} font-mono text-xs`}>{examplesText(row)}</td>
              <td className={`${TD} whitespace-nowrap text-xs`}>{longDate(row.createdAt)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Fact({ name, value }: { name: string; value: string }) {
  return (
    <div className="mt-2 flex flex-col gap-0.5 sm:flex-row sm:gap-3">
      <dt className="font-mono text-xs text-mute sm:w-48 sm:shrink-0">{name}</dt>
      <dd className="break-words text-sm text-text">{value}</dd>
    </div>
  );
}

function Detail({ request, onChanged }: { request: AdminRequestDetail['request']; onChanged: () => void }) {
  const navigate = useNavigate();
  const [status, setStatus] = useState<RequestStatus>(request.status);
  const [deleting, setDeleting] = useState(false);
  const [reason, setReason] = useState(request.declineReason ?? '');
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [notice, setNotice] = useState<{ kind: 'refused' | 'done'; text: string } | null>(null);
  const { form } = request;
  const base = `/api/admin/adapter-requests/${encodeURIComponent(request.id)}`;
  // Whether its data set's zip is still in the file store (deleting the request deletes it too).
  const holdsDataSet = request.dataSet !== null && request.examplesDeletedAt === null;

  const save = async () => {
    setConfirming(false);
    setBusy(true);
    setNotice(null);
    const answer = await askHub(`${base}/status`, { body: status === 'declined' ? { status, declineReason: reason } : { status } });
    setBusy(false);
    if (answer.kind === 'answer' && answer.status === 200) { setNotice({ kind: 'done', text: `Status set to ${STATUS_LABELS[status]}.` }); onChanged(); return; }
    setNotice({ kind: 'refused', text: refusalOf(answer) });
  };
  const remove = async () => {
    setDeleting(false);
    setBusy(true);
    setNotice(null);
    const answer = await askHub(`${base}/delete`, { body: { confirm: 'delete' } });
    setBusy(false);
    if (answer.kind === 'answer' && answer.status === 200) { navigate('/admin/requests'); return; }
    setNotice({ kind: 'refused', text: refusalOf(answer) });
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (status === 'declined') setConfirming(true);
    else void save();
  };

  const options = form.answer.kind === 'caller'
    ? form.answer.exampleSets.map((set, i) => `Set ${i + 1}: ${set.map((o) => o.key).join(', ')}`).join(' · ')
    : form.answer.options.map((o) => `${o.key}: ${o.definition}`).join(' · ');
  return (
    <>
      <p className="mt-2 font-mono text-sm text-mute"><Link to="/admin/requests" className={TEXT_LINK}>Every request</Link></p>
      <section className={`mt-6 p-5 sm:p-6 ${CARD}`} aria-label="The request">
        <h2 className={SECTION_HEADING}>{request.id}</h2>
        <dl className="mt-3">
          <Fact name="From" value={request.email} />
          <Fact name="Sent" value={longDate(request.createdAt)} />
          <Fact name="Status" value={STATUS_LABELS[request.status]} />
          {request.closedAt !== null && <Fact name="Closed" value={longDate(request.closedAt)} />}
          {request.declineReason !== null && <Fact name="Why declined" value={request.declineReason} />}
          <Fact name="Question" value={form.decision.question} />
          <Fact name="Why it matters" value={form.decision.why} />
          <Fact name="Kind of answer" value={ANSWER_KIND_LABELS[form.answer.kind]} />
          <Fact name="Options" value={options} />
          <Fact name="When none fits" value={form.answer.noneFits === 'none_option' ? 'A "none" option' : 'Abstain'} />
          <Fact name="Input" value={form.input.form === 'text' ? 'One text' : `Named fields: ${form.input.fields.map((f) => f.name).join(', ')}`} />
          <Fact name="Length" value={`typically ${form.input.typicalLength.toLocaleString('en-US')}, at most ${form.input.maxLength.toLocaleString('en-US')} characters`} />
          <Fact name="Languages" value={form.input.languages} />
          <Fact name="Context" value={form.context.length === 0 ? 'None' : form.context.map((f) => f.name).join(', ')} />
          <Fact name="Examples" value={EXAMPLE_SOURCE_LABELS[request.source]} />
          {request.examplesDeletedAt !== null
            ? <Fact name="Deleted" value={examplesDeletedText(request.examplesDeletedAt)} />
            : request.dataSet === null
            ? <Fact name="Typed examples" value={`${request.exampleCount.toLocaleString('en-US')} (${request.examples.filter((e) => e.tricky).length} tricky)`} />
            : (
              <>
                <Fact name="Data set" value={examplesText(request)} />
                {request.dataSetCounts !== null && request.dataSetCounts.length > 0
                  && <Fact name="Rows per option" value={request.dataSetCounts.map((c) => `${c.key} ${c.count.toLocaleString('en-US')}`).join(', ')} />}
              </>
            )}
          <Fact name="More later" value={form.moreExamplesLater.toLocaleString('en-US')} />
          <Fact name="Calls a day" value={form.use.callsPerDay.toLocaleString('en-US')} />
          <Fact name="Speed matters" value={form.use.speedMatters ? 'Yes' : 'No'} />
          <Fact name="Useful accuracy" value={form.use.accuracy} />
          <Fact name="Abstain over wrong" value={form.use.abstainBetter ? 'Yes' : 'No'} />
          <Fact name="May improve the base" value={request.consent.mayImproveBase ? 'Yes' : 'No'} />
        </dl>
        <div className="mt-5 flex flex-wrap items-center gap-3">
          {request.examplesDeletedAt !== null
            ? <p className="font-mono text-sm text-soft">{examplesDeletedText(request.examplesDeletedAt)}</p>
            : request.dataSet === null
            ? <a href={`${base}/export?file=examples`} className={`${BUTTON_SECONDARY} px-4 py-2`} download>Training rows</a>
            : <a href={`${base}/data-set`} className={`${BUTTON_SECONDARY} px-4 py-2`}>Download the data set</a>}
          <a href={`${base}/export?file=manifest`} className={`${BUTTON_SECONDARY} px-4 py-2`} download>Manifest draft</a>
        </div>
      </section>
      <section className={`mt-6 p-5 sm:p-6 ${CARD}`} aria-label="Status">
        <h2 className={SECTION_HEADING}>Status</h2>
        <form className="mt-3 flex flex-col gap-3" onSubmit={submit}>
          <label htmlFor="status" className="font-mono text-sm text-soft">Status</label>
          <select id="status" value={status} onChange={(e) => setStatus(e.target.value as RequestStatus)} className="border border-ink bg-background px-3 py-2 text-sm sm:w-60">
            {REQUEST_STATUSES.map((s) => <option key={s} value={s}>{STATUS_LABELS[s]}</option>)}
          </select>
          {status === 'declined' && (
            <>
              <label htmlFor="decline-reason" className="font-mono text-sm text-soft">Why it is declined (the requester sees this)</label>
              <textarea id="decline-reason" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={LIMITS.declineReasonMax} rows={3}
                className="border border-ink bg-background px-3 py-2 text-sm" />
            </>
          )}
          <div><button type="submit" disabled={busy} className={`${BUTTON_PRIMARY} px-4 py-2`}>Set status</button></div>
        </form>
        {notice !== null && <p role={notice.kind === 'refused' ? 'alert' : 'status'} className={`mt-3 text-sm ${notice.kind === 'refused' ? 'text-danger' : 'text-soft'}`}>{notice.text}</p>}
      </section>
      <section className={`mt-6 p-5 sm:p-6 ${CARD}`} aria-label="Delete">
        <h2 className={SECTION_HEADING}>Delete</h2>
        <p className="mt-3 text-sm text-soft">{`Deletes the request${request.examplesDeletedAt === null ? ' and its examples' : ''}${holdsDataSet ? ', and its data set from the file store' : ''}. It cannot be undone.`}</p>
        <div className="mt-3"><button type="button" disabled={busy} className={`${BUTTON_DANGER} px-4 py-2`} onClick={() => setDeleting(true)}>Delete request</button></div>
      </section>
      <ConfirmDialog open={deleting} title="Delete request" message={`Delete ${request.id}${holdsDataSet ? ' and its data set' : ''}? It cannot be undone.`}
        confirmLabel="Delete request" danger onConfirm={() => void remove()} onCancel={() => setDeleting(false)} />
      <ConfirmDialog open={confirming} title="Decline request" message={`Decline ${request.id}? The requester sees the reason on their account page.`}
        confirmLabel="Decline request" danger onConfirm={() => void save()} onCancel={() => setConfirming(false)} />
    </>
  );
}

export default function AdminRequests() {
  const [params] = useSearchParams();
  const [hydrated, setHydrated] = useState(false);
  const [list, setList] = useState<Loaded<AdminRequests>>({ kind: 'loading' });
  const [detail, setDetail] = useState<Loaded<AdminRequestDetail>>({ kind: 'loading' });
  const id = hydrated ? params.get('id') : null;

  useEffect(() => { setHydrated(true); }, []);

  const refresh = useCallback(async () => {
    if (id === null) setList(await load('/api/admin/adapter-requests', isList));
    else setDetail(await load(`/api/admin/adapter-requests/${encodeURIComponent(id)}`, isDetail));
  }, [id]);

  useEffect(() => {
    if (!hydrated) return;
    setList({ kind: 'loading' });
    setDetail({ kind: 'loading' });
    void refresh();
  }, [hydrated, refresh]);

  const state = id === null ? list : detail;
  if (state.kind === 'missing') return <NotFound />;
  return (
    <main id="main" className="mx-auto max-w-6xl px-4 py-12 sm:py-16">
      <h1 className={PAGE_TITLE}>Adapter requests</h1>
      {state.kind === 'loading' && <p role="status" className="mt-4 text-sm text-soft">Loading…</p>}
      {state.kind === 'failed' && <p role="alert" className="mt-4 text-sm text-danger">{state.message}</p>}
      {state.kind === 'ready' && id === null && <List rows={(state.value as AdminRequests).requests} />}
      {state.kind === 'ready' && id !== null && <Detail key={(state.value as AdminRequestDetail).request.id} request={(state.value as AdminRequestDetail).request} onChanged={() => void refresh()} />}
    </main>
  );
}
