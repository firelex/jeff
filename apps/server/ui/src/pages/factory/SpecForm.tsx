import { useEffect, useState, type ReactNode } from 'react';
import { Plus, Trash2 } from 'lucide-react';
import { Badge, Button, Input, Textarea } from '@jeff/ui';
import type { SpecQuestion, TaskSpec } from './api';

/** A small label above its control; the control is inside the <label>, so it takes the label as its name. */
function Labelled({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-xs font-medium text-mute">{label}</span>
      {children}
      {hint && <span className="text-xs text-mute">{hint}</span>}
    </label>
  );
}

function RemoveButton({ label, onClick, disabled }: { label: string; onClick: () => void; disabled: boolean }) {
  return (
    <Button variant="ghost" size="sm" className="shrink-0 border-danger/40 text-danger hover:bg-danger/10" aria-label={label}
      onClick={onClick} disabled={disabled}>
      <Trash2 className="size-4" aria-hidden="true" />
    </Button>
  );
}

const SECTION = 'm-0 text-sm font-semibold text-text';

// The task spec as an editable form. Read-only once approved (and always in a replay).
export function SpecForm({ spec, readOnly, onChange }: { spec: TaskSpec; readOnly: boolean; onChange: (spec: TaskSpec) => void }) {
  const [draft, setDraft] = useState(spec);
  useEffect(() => setDraft(spec), [spec]);
  const update = (next: TaskSpec) => {
    setDraft(next);
    onChange(next);
  };
  const setQuestion = (index: number, question: SpecQuestion) =>
    update({ ...draft, questions: draft.questions.map((q, i) => (i === index ? question : q)) });

  return (
    <div className="flex flex-col gap-6">
      <div className="grid gap-4 md:grid-cols-[14rem_1fr]">
        <Labelled label="Adapter name"><Input className="font-mono" value={draft.name} disabled={readOnly} onChange={(e) => update({ ...draft, name: e.target.value })} /></Labelled>
        <Labelled label="What it decides"><Input value={draft.summary} disabled={readOnly} onChange={(e) => update({ ...draft, summary: e.target.value })} /></Labelled>
      </div>

      <section className="flex flex-col gap-3">
        <h4 className={SECTION}>State</h4>
        <div className="grid gap-4 md:grid-cols-[14rem_1fr]">
          <Labelled label="One deployment is a"><Input value={draft.family.noun} disabled={readOnly} onChange={(e) => update({ ...draft, family: { ...draft.family, noun: e.target.value } })} /></Labelled>
          <Labelled label="Description"><Input value={draft.family.description} disabled={readOnly} onChange={(e) => update({ ...draft, family: { ...draft.family, description: e.target.value } })} /></Labelled>
        </div>
        <div className="flex flex-col gap-2 rounded-card border border-line p-3">
          <p className="m-0 text-xs text-mute">{draft.family.fields.length
            ? `Fields that stay the same for one ${draft.family.noun || 'deployment'} (first in the state)`
            : 'No fixed fields: the state is just the request text'}</p>
          {draft.family.fields.map((field, index) => (
            <div key={index} className="flex gap-2">
              <Input className="w-48 font-mono" value={field.name} disabled={readOnly} aria-label="Field name"
                onChange={(e) => update({ ...draft, family: { ...draft.family, fields: draft.family.fields.map((f, i) => (i === index ? { ...f, name: e.target.value } : f)) } })} />
              <Input value={field.description} disabled={readOnly} aria-label="Field description"
                onChange={(e) => update({ ...draft, family: { ...draft.family, fields: draft.family.fields.map((f, i) => (i === index ? { ...f, description: e.target.value } : f)) } })} />
              {!readOnly && <RemoveButton label={`Remove ${field.name}`} disabled={draft.family.fields.length < 2}
                onClick={() => update({ ...draft, family: { ...draft.family, fields: draft.family.fields.filter((_, i) => i !== index) } })} />}
            </div>
          ))}
          {!readOnly && (
            <div>
              <Button variant="ghost" size="sm" onClick={() => update({ ...draft, family: { ...draft.family, fields: [...draft.family.fields, { name: '', description: '' }] } })}>
                <Plus className="size-4" aria-hidden="true" />Add field
              </Button>
            </div>
          )}
          <div className="flex items-center gap-2 border-t border-line pt-2">
            <Badge tone="info" className="shrink-0">changes per request · last</Badge>
            <Input className="w-48 font-mono" value={draft.live_field.name} disabled={readOnly} aria-label="Changing field name"
              onChange={(e) => update({ ...draft, live_field: { ...draft.live_field, name: e.target.value } })} />
            <Input value={draft.live_field.description} disabled={readOnly} aria-label="Changing field description"
              onChange={(e) => update({ ...draft, live_field: { ...draft.live_field, description: e.target.value } })} />
          </div>
        </div>
      </section>

      {draft.questions.every((q) => q.imported) ? (
        <section className="flex flex-col gap-3">
          <h4 className={SECTION}>Questions <Badge tone="info" className="ml-1">{draft.questions.length} imported verbatim</Badge>
            <span className="ml-2 font-normal text-mute">· the teacher never writes or rewords them</span></h4>
          <div className="max-h-96 divide-y divide-line overflow-y-auto rounded-card border border-line">
            {draft.questions.map((question) => (
              <div key={question.id} className="grid grid-cols-[16rem_3.5rem_1fr] gap-3 px-3 py-2 text-xs">
                <span className="truncate font-mono text-text" title={question.id}>{question.id}</span>
                <span className="font-mono text-mute">{question.type}</span>
                <span className="text-text">{question.instruction}
                  {question.options.length > 0 && <span className="text-mute"> · {question.options.map((o) => o.key).join(', ')}</span>}
                  {question.applies_when && <span className="block text-mute">{question.applies_when}</span>}
                </span>
              </div>
            ))}
          </div>
        </section>
      ) : (
        <section className="flex flex-col gap-3">
          <h4 className={SECTION}>Questions <span className="font-normal text-mute">· one per training row; the first is asked of every request</span></h4>
          {draft.questions.map((question, index) => (
            <div key={index} className="flex flex-col gap-3 rounded-card border border-line p-3">
              <div className="flex flex-wrap items-center gap-2">
                <Input className="w-56 font-mono" value={question.id} disabled={readOnly} aria-label="Question id"
                  onChange={(e) => setQuestion(index, { ...question, id: e.target.value })} />
                <Badge tone="neutral" className="font-mono">{question.type}</Badge>
                <span className="text-xs text-mute">{question.applies_when ?? 'asked of every request'}</span>
                {!readOnly && index > 0 && <span className="ml-auto"><RemoveButton label={`Remove ${question.id}`} disabled={false}
                  onClick={() => update({ ...draft, questions: draft.questions.filter((_, i) => i !== index) })} /></span>}
              </div>
              <Textarea rows={2} value={question.instruction} disabled={readOnly} aria-label="Canonical instruction"
                onChange={(e) => setQuestion(index, { ...question, instruction: e.target.value })} />
              {index > 0 && (
                <Input value={question.applies_when ?? ''} disabled={readOnly} placeholder="Asked only when… (empty: every request)" aria-label="Applies when"
                  onChange={(e) => setQuestion(index, { ...question, applies_when: e.target.value || null })} />
              )}
              <div className="flex flex-col gap-1.5">
                {question.options.map((option, optionIndex) => (
                  <div key={optionIndex} className="flex gap-2">
                    <Input className="w-56 font-mono" value={option.key} disabled={readOnly || question.type === 'noul'} aria-label="Option key"
                      onChange={(e) => setQuestion(index, { ...question, options: question.options.map((o, i) => (i === optionIndex ? { ...o, key: e.target.value } : o)) })} />
                    <Input value={option.description} disabled={readOnly} aria-label="Option description"
                      onChange={(e) => setQuestion(index, { ...question, options: question.options.map((o, i) => (i === optionIndex ? { ...o, description: e.target.value } : o)) })} />
                    {!readOnly && question.type !== 'noul' && <RemoveButton label={`Remove ${option.key}`} disabled={question.options.length < 3}
                      onClick={() => setQuestion(index, { ...question, options: question.options.filter((_, i) => i !== optionIndex) })} />}
                  </div>
                ))}
                {!readOnly && question.type !== 'noul' && (
                  <div>
                    <Button variant="ghost" size="sm" onClick={() => setQuestion(index, { ...question, options: [...question.options, { key: '', description: '' }] })}>
                      <Plus className="size-4" aria-hidden="true" />Add option
                    </Button>
                  </div>
                )}
              </div>
            </div>
          ))}
        </section>
      )}

      <section className="flex flex-col gap-3">
        <h4 className={SECTION}>Hard cases to include</h4>
        {draft.hard_cases.map((item, index) => (
          <div key={index} className="flex gap-2">
            <Input className="w-56 font-mono" value={item.kind} disabled={readOnly} aria-label="Hard case kind"
              onChange={(e) => update({ ...draft, hard_cases: draft.hard_cases.map((h, i) => (i === index ? { ...h, kind: e.target.value } : h)) })} />
            <Input value={item.description} disabled={readOnly} aria-label="Hard case description"
              onChange={(e) => update({ ...draft, hard_cases: draft.hard_cases.map((h, i) => (i === index ? { ...h, description: e.target.value } : h)) })} />
            {!readOnly && <RemoveButton label={`Remove ${item.kind}`} disabled={false}
              onClick={() => update({ ...draft, hard_cases: draft.hard_cases.filter((_, i) => i !== index) })} />}
          </div>
        ))}
        {!readOnly && (
          <div>
            <Button variant="ghost" size="sm" onClick={() => update({ ...draft, hard_cases: [...draft.hard_cases, { kind: '', description: '' }] })}>
              <Plus className="size-4" aria-hidden="true" />Add hard case
            </Button>
          </div>
        )}
        <Labelled label="Phrasing styles (comma separated)">
          <Input value={draft.styles.join(', ')} disabled={readOnly}
            onChange={(e) => update({ ...draft, styles: e.target.value.split(',').map((s) => s.trim()).filter(Boolean) })} />
        </Labelled>
      </section>

      <section className="flex flex-col gap-3">
        <h4 className={SECTION}>Sizes</h4>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-6">
          <Labelled label="Training rows">
            <Input type="number" value={draft.sizes.train} disabled={readOnly}
              onChange={(e) => update({ ...draft, sizes: { ...draft.sizes, train: Number(e.target.value) } })} />
          </Labelled>
          <Labelled label="Updates (batch 16)" hint={draft.sizes.train > 0 ? `${((draft.sizes.updates * 16) / draft.sizes.train).toFixed(2)} passes over the rows` : 'no training rows'}>
            <Input type="number" value={draft.sizes.updates} disabled={readOnly}
              onChange={(e) => update({ ...draft, sizes: { ...draft.sizes, updates: Number(e.target.value) } })} />
          </Labelled>
          {(['dev', 'calibration', 'test', 'families'] as const).map((key) => (
            <Labelled key={key} label={key === 'families' ? 'Families (≈10% held out)' : `${key[0].toUpperCase()}${key.slice(1)} rows`}>
              <Input type="number" value={draft.sizes[key]} disabled={readOnly}
                onChange={(e) => update({ ...draft, sizes: { ...draft.sizes, [key]: Number(e.target.value) } })} />
            </Labelled>
          ))}
        </div>
      </section>
    </div>
  );
}
