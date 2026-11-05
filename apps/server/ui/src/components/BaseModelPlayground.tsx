// The POST /v1/systemone request editor: a model (the base, or a served adapter, from GET /v1/models), the state as
// text or JSON, the questions as JSON, up to four images and the both-orders switch; the answer is shown by
// SystemOneResult.
import { useEffect, useMemo, useRef, useState } from 'react';
import { AlignLeft, Braces, ImagePlus, Play, X } from 'lucide-react';
import { Badge, Button, JsonEditor, Select, Spinner, Switch, Textarea } from '@jeff/ui';
import {
  shownError, type Api, type Health, type Json, type ModelEntry, type Question, type ShownError, type SystemOneRequest,
  type SystemOneResponse, type Timed,
} from '../api';
import { EXAMPLES, TEMPLATES, type BaseExample } from '../examples';
import { pretty } from '../json';
import { ErrorAlert } from './ErrorAlert';
import { Card } from './Page';
import { RUN_SHORTCUT } from '../shortcut';
import { SystemOneResult } from './SystemOneResult';

type StateFormat = 'text' | 'json';

const MAX_IMAGES = 4;
const MAX_IMAGE_BYTES = 8_000_000;

function parseQuestions(text: string): { questions?: Record<string, Question>; problem?: string } {
  if (!text.trim()) return { problem: 'Add at least one question' };
  try {
    const value: unknown = JSON.parse(text);
    if (typeof value !== 'object' || value === null || Array.isArray(value)) return { problem: 'Questions must be a JSON object: name → question' };
    if (Object.keys(value).length === 0) return { problem: 'Add at least one question' };
    return { questions: value as Record<string, Question> };
  } catch (error) {
    return { problem: (error as Error).message };
  }
}

/**
 * The questions text with one more question, under the next free question_<n> name. An empty editor starts a new
 * set; text that does not parse as a questions object is refused with the parse problem, never replaced.
 */
export function questionsWithTemplate(text: string, question: Question): { text: string; problem?: never } | { text?: never; problem: string } {
  let current: Record<string, Question> = {};
  if (text.trim()) {
    const parsed = parseQuestions(text);
    if (!parsed.questions) return { problem: `The questions are not valid, so no template was added: ${parsed.problem}` };
    current = parsed.questions;
  }
  let index = Object.keys(current).length + 1;
  while (`question_${index}` in current) index += 1;
  return { text: pretty({ ...current, [`question_${index}`]: question }) };
}

function parseState(format: StateFormat, text: string): { state?: Json; problem?: string } {
  if (format === 'text') return text.trim() ? { state: text } : { problem: 'The state is empty' };
  try {
    return { state: JSON.parse(text) as Json };
  } catch (error) {
    return { problem: (error as Error).message };
  }
}

function readImage(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = () => reject(new Error(`Could not read ${file.name}: ${String(reader.error)}`));
    reader.readAsDataURL(file);
  });
}

export function BaseModelPlayground({ api, health }: { api: Api; health: Health | null }) {
  const [models, setModels] = useState<ModelEntry[] | null>(null);
  const [loadError, setLoadError] = useState<ShownError | null>(null);
  const [model, setModel] = useState('');
  const [stateFormat, setStateFormat] = useState<StateFormat>('text');
  const [stateText, setStateText] = useState('');
  const [questionsText, setQuestionsText] = useState('');
  const [images, setImages] = useState<string[]>([]);
  const [bothOrders, setBothOrders] = useState(false);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<{ run: Timed<SystemOneResponse>; request: string } | null>(null);
  const [error, setError] = useState<ShownError | null>(null);
  const fileInput = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    let current = true;
    void (async () => {
      try {
        const answer = await api.models();
        // GET /v1/models lists the base first, then each served adapter.
        const first = answer.models.length > 0 ? answer.models[0].name : null;
        if (!current) return;
        setModels(answer.models);
        if (first !== null) setModel(first);
      } catch (failure) {
        if (current) setLoadError(shownError(failure));
      }
    })();
    return () => {
      current = false;
    };
  }, [api]);

  const questions = useMemo(() => parseQuestions(questionsText), [questionsText]);
  const parsedState = useMemo(() => parseState(stateFormat, stateText), [stateFormat, stateText]);
  const problems = [models !== null && models.length === 0 && 'No model to ask: GET /v1/models lists none', parsedState.problem && `State: ${parsedState.problem}`, questions.problem && `Questions: ${questions.problem}`]
    .filter(Boolean) as string[];
  const readsImages = health?.modalities?.includes('image') === true;

  const loadExample = (example: BaseExample) => {
    if (typeof example.state === 'string') {
      setStateFormat('text');
      setStateText(example.state);
    } else {
      setStateFormat('json');
      setStateText(pretty(example.state));
    }
    setQuestionsText(pretty(example.questions));
    setResult(null);
    setError(null);
    if (models && models.length > 0) setModel(models[0].name);
    if (example.needsImage && images.length === 0 && readsImages) fileInput.current?.click();
  };

  const addTemplate = (question: Question) => {
    const next = questionsWithTemplate(questionsText, question);
    if (next.problem !== undefined) {
      setError({ type: 'InvalidQuestions', message: next.problem, requestId: null });
      return;
    }
    setError(null);
    setQuestionsText(next.text);
  };

  const switchFormat = (format: StateFormat) => {
    if (format === stateFormat) return;
    if (format === 'json') {
      setStateText(stateText.trim() ? pretty({ text: stateText }) : '{\n  \n}');
    } else {
      const parsed = parseState('json', stateText);
      setStateText(typeof parsed.state === 'string' ? parsed.state : stateText);
    }
    setStateFormat(format);
  };

  const addImages = async (files: FileList | null) => {
    if (!files) return;
    const chosen = Array.from(files);
    if (images.length + chosen.length > MAX_IMAGES) {
      setError({ type: 'TooManyImages', message: `A request takes at most ${MAX_IMAGES} images.`, requestId: null });
      return;
    }
    const tooLarge = chosen.find((file) => file.size > MAX_IMAGE_BYTES);
    if (tooLarge) {
      setError({ type: 'ImageTooLarge', message: `${tooLarge.name} is over 8 MB.`, requestId: null });
      return;
    }
    try {
      const encoded = await Promise.all(chosen.map(readImage));
      setImages([...images, ...encoded]);
    } catch (failure) {
      setError(shownError(failure));
    }
  };

  const run = async () => {
    if (!questions.questions || parsedState.state === undefined || !model) return;
    const body: SystemOneRequest = {
      model,
      state: parsedState.state,
      questions: questions.questions,
      ...(images.length ? { images } : {}),
      ...(bothOrders ? { orders: 2 as const } : {}),
    };
    setRunning(true);
    setError(null);
    try {
      const answer = await api.systemOne(body);
      const shown = body.images ? { ...body, images: body.images.map((image) => `${image.slice(0, 40)}…`) } : body;
      setResult({ run: answer, request: pretty(shown) });
    } catch (failure) {
      setResult(null);
      setError(shownError(failure));
    } finally {
      setRunning(false);
    }
  };

  const runRef = useRef(run);
  runRef.current = run;

  if (loadError) return <ErrorAlert error={loadError} />;
  if (!models) return <div className="flex items-center gap-2 text-sm text-mute"><Spinner /> Loading the models</div>;

  const canRun = !running && problems.length === 0 && model !== '';

  return (
    <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,560px)_minmax(0,1fr)]"
      onKeyDown={(event) => {
        if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
          event.preventDefault();
          if (canRun) void runRef.current();
        }
      }}>
      <Card className="flex min-w-0 flex-col gap-5">
        <label className="flex flex-col gap-1.5 text-sm font-medium text-text">
          Model
          <Select aria-label="Model" value={model} onChange={(event) => setModel(event.target.value)} className="font-mono text-[13px]">
            {models.map((entry) => <option key={entry.name} value={entry.name}>{entry.name}: {entry.description}</option>)}
          </Select>
        </label>

        <div className="flex flex-col gap-1.5">
          <div className="flex items-center justify-between">
            <p className="m-0 text-sm font-medium text-text">State</p>
            <div className="flex gap-1">
              <button type="button" aria-label="State as text" aria-pressed={stateFormat === 'text'} onClick={() => switchFormat('text')}
                className={['rounded-tag p-1', stateFormat === 'text' ? 'bg-primary-light text-primary-dark' : 'text-faint hover:text-text'].join(' ')}>
                <AlignLeft className="size-4" aria-hidden="true" />
              </button>
              <button type="button" aria-label="State as JSON" aria-pressed={stateFormat === 'json'} onClick={() => switchFormat('json')}
                className={['rounded-tag p-1', stateFormat === 'json' ? 'bg-primary-light text-primary-dark' : 'text-faint hover:text-text'].join(' ')}>
                <Braces className="size-4" aria-hidden="true" />
              </button>
            </div>
          </div>
          {stateFormat === 'text' ? (
            <Textarea aria-label="State text" rows={6} value={stateText} onChange={(event) => setStateText(event.target.value)}
              placeholder="What is being decided about: a message, a log line, a document…" />
          ) : (
            <div className="h-[180px]"><JsonEditor label="State JSON" value={stateText} onChange={setStateText} /></div>
          )}
        </div>

        <div className="flex flex-col gap-1.5">
          <div className="flex items-center justify-between">
            <p className="m-0 text-sm font-medium text-text">Questions</p>
            {questions.questions && <Badge tone="neutral">{Object.keys(questions.questions).length} question{Object.keys(questions.questions).length === 1 ? '' : 's'}</Badge>}
          </div>
          {questionsText.trim() ? (
            <div className="h-[260px]"><JsonEditor label="Questions JSON" value={questionsText} onChange={setQuestionsText} /></div>
          ) : (
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
              {TEMPLATES.map((template) => (
                <button key={template.type} type="button" onClick={() => addTemplate(template.question)}
                  className="flex flex-col items-start gap-1.5 rounded-button border border-line bg-background p-3 text-left hover:border-primary-border hover:bg-primary-light">
                  <Badge tone="neutral">{template.type}</Badge>
                  <span className="text-sm font-medium text-text">{template.title}</span>
                  <span className="text-xs italic text-mute">{template.hint}</span>
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="flex flex-col gap-2">
          {images.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {images.map((image, index) => (
                <div key={index} className="relative">
                  <img src={image} alt={`Attached image ${index + 1}`} className="h-14 max-w-24 rounded-button border border-line object-contain" />
                  <button type="button" aria-label={`Remove image ${index + 1}`} onClick={() => setImages(images.filter((_, other) => other !== index))}
                    className="absolute -top-2 -right-2 flex size-5 items-center justify-center rounded-full border border-line bg-panel text-mute hover:text-danger">
                    <X className="size-3" aria-hidden="true" />
                  </button>
                </div>
              ))}
            </div>
          )}
          <div>
            <Button variant="pending" size="sm" disabled={images.length >= MAX_IMAGES || !readsImages} onClick={() => fileInput.current?.click()}>
              <ImagePlus className="size-3.5" aria-hidden="true" />
              {readsImages ? `Add images (${images.length} of ${MAX_IMAGES})` : 'This server does not read images'}
            </Button>
          </div>
          <input ref={fileInput} type="file" accept="image/png,image/jpeg,image/webp" multiple hidden
            onChange={(event) => {
              void addImages(event.target.files);
              event.target.value = '';
            }} />
        </div>

        <div className="flex items-center gap-3 border-t border-line pt-4">
          <label className="flex items-center gap-2 text-xs text-mute"
            title="Answer each question twice, the second time with the options reversed, and average (twice the cost)">
            <Switch checked={bothOrders} onChange={setBothOrders} aria-label="Both option orders" />
            Both orders
          </label>
          <span className="ml-auto text-xs text-faint" title={problems.join('\n')}>{problems.length > 0 ? problems[0] : RUN_SHORTCUT}</span>
          <Button pending={running} disabled={!canRun} onClick={() => void run()}>
            <Play className="size-4" aria-hidden="true" />
            Run
          </Button>
        </div>
      </Card>

      <div aria-live="polite" className="flex min-w-0 flex-col gap-4 xl:sticky xl:top-6 xl:self-start">
        {error && <ErrorAlert error={error} />}
        {result ? <SystemOneResult result={result.run} requestJson={result.request} /> : (
          <div className="flex flex-col gap-3">
            <p className="m-0 text-[10.5px] font-bold uppercase tracking-[0.14em] text-mute">Examples for the base model</p>
            <div className="overflow-hidden rounded-card border border-line bg-panel shadow-card">
              {EXAMPLES.map((example) => (
                <button key={example.id} type="button" onClick={() => loadExample(example)}
                  className="flex w-full items-baseline gap-3 border-b border-line px-5 py-3.5 text-left last:border-b-0 hover:bg-primary-light">
                  <span className="shrink-0 text-sm font-medium text-text">{example.title}</span>
                  <span className="truncate text-sm text-mute">{example.description}</span>
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
