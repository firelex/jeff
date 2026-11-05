// One adapter's page: what it decides, its worked examples (content/catalogue-examples.yaml: the featured one in full,
// up to four more listed under it, each opening to the same full case and code), its functions, its base,
// how it was measured (content/adapter-numbers.yaml, components/catalogue/Measured.tsx), its first function's call in every shown language and curl (catalogue-call.server.ts), and -- for an open adapter -- how to download its weights and its licence. A proprietary adapter has no weights to show: it says so and points to
// the API docs instead.
import { Link, useLoaderData } from 'react-router';
import { FunctionTable } from '../components/catalogue/FunctionTable';
import { Measured } from '../components/catalogue/Measured';
import { CallCode, Example } from '../components/docs/Example';
import { BUTTON_SECONDARY, CARD, SECTION_HEADING, TABLE_FRAME, TD, TEXT_LINK, TH } from '../components/styles';
import { Tag } from '../components/Tag';
import { useOwnerView } from '../lib/owner-view';
import type { CatalogueEntry } from '../lib/catalogue.server';
import { adapterCall, localAdapters } from '../lib/catalogue-call.server';
import type { ExampleBlock } from '../lib/example-block';
import { exampleBlock } from '../lib/examples.server';
import { adapterPlaygroundLink, playgroundLink } from '../lib/playground-samples.server';
import { adapterNumbers, catalogue, catalogueExamples, publishedWeights, shownLanguages } from '../lib/site.server';
import { settings } from '../lib/settings.server';
import type { Route } from './+types/catalogue-adapter';

// loadCatalogue only ever gives an open entry both a weights repo and a licence (catalogue.server.ts: the build
// fails first if either is missing), so a null here means that guarantee broke -- which must fail loud, naming the
// adapter, rather than silently falling through to the page's proprietary "API only" rendering.
export function assertOpenAdapterIsComplete(entry: CatalogueEntry): void {
  if (entry.kind === 'open' && (entry.weights === null || entry.licence === null)) {
    throw new Error(`${entry.id}: an open adapter with no weights or licence`);
  }
}

export function loader({ params }: Route.LoaderArgs) {
  const entry = catalogue().find((e) => e.id === params.id);
  if (entry === undefined) throw new Response(`No catalogue adapter ${params.id}`, { status: 404 });
  assertOpenAdapterIsComplete(entry);
  const apiOrigin = settings().apiOrigin;
  const shown = catalogueExamples().get(entry.id);
  if (shown === undefined) throw new Error(`${entry.id}: no entry in content/catalogue-examples.yaml`);
  // An adapter listed with a finding answered a clear case wrongly, so its page shows no example.
  const local = localAdapters(catalogue());
  const examples = 'finding' in shown ? [] : shown.examples.map(({ example, shows }) => ({
    block: exampleBlock(example, apiOrigin, shownLanguages(), playgroundLink(example), local),
    shows,
    // The loader has checked every listed example has a name (catalogue-examples.server.ts).
    name: example.name as string,
  }));
  // Each listed example's line gives its recorded answer, so one with none (an offline example) can only be featured.
  const unrecorded = examples.slice(1).filter((e) => e.block.answer === null).map((e) => e.block.id);
  if (unrecorded.length > 0) throw new Error(`${entry.id}: listed examples with no recorded answer: ${unrecorded.join(', ')}`);
  const example = examples.length === 0 ? null : examples[0];
  const more = examples.slice(1);
  // Weights not yet on Hugging Face are published at launch; the page says so beside the link.
  const published = entry.weights !== null && publishedWeights().has(`${entry.weights.repo}@${entry.weights.revision}`);
  // The playground with this adapter's own example chosen when it is a sample, else the adapter's first sample; null
  // when the playground has none of this adapter's examples.
  const playground = example === null ? adapterPlaygroundLink(entry.name) : example.block.playground;
  const call = adapterCall(entry, apiOrigin, shownLanguages());
  const numbers = adapterNumbers().get(entry.id);
  if (numbers === undefined) throw new Error(`${entry.id}: no entry in content/adapter-numbers.yaml`);
  return { entry, example, more, published, playground, call, numbers };
}

export function meta({ loaderData }: Route.MetaArgs) {
  return [{ title: `${loaderData.entry.title} | Jeff catalogue` }];
}

// A sentence with `backticked` values (content/catalogue-examples.yaml), the backticked parts set as code. The
// loader has checked the backticks pair up.
export function WithInlineCode({ text }: { text: string }) {
  return (
    <>
      {text.split('`').map((part, i) => (i % 2 === 1
        ? <code key={i} className="bg-white/10 px-1 font-mono text-xs text-white">{part}</code>
        : part))}
    </>
  );
}

// A field's code name is what a caller passes; its raw, as-trained key is shown underneath only when they differ.
function FieldName({ codeName, fieldKey }: { codeName: string; fieldKey: string }) {
  return (
    <div className="font-mono text-xs text-text">
      {codeName}
      {codeName !== fieldKey && <div className="mt-0.5 font-sans text-[11px] text-mute">as trained: {fieldKey}</div>}
    </div>
  );
}

type ListedExample = { block: ExampleBlock; shows: string; name: string };

// The adapter's other examples under the featured one: one line each with its name, its recorded answer and the
// confidence, opening (a native details element, so keyboard and screen readers work without script) to the same
// full case, answer, sentence and code as the featured example, with the same language tabs and API or local choice.
function MoreExamples({ examples }: { examples: ListedExample[] }) {
  return (
    <div className="mt-2">
      <h3 className="font-mono text-xs uppercase tracking-wide text-mute">More examples</h3>
      <ul className="mt-2 divide-y divide-ink/30 border-2 border-ink" data-more-examples>
        {examples.map(({ block, shows, name }) => (
          <li key={block.id}>
            <details className="group" data-example-line={block.id}>
              <summary className="flex cursor-pointer list-none flex-wrap items-baseline gap-x-3 gap-y-1 px-3 py-2 hover:bg-ink/5 [&::-webkit-details-marker]:hidden">
                <span aria-hidden="true" className="font-mono text-xs text-mute group-open:rotate-90">&gt;</span>
                <span className="min-w-0 flex-1 text-sm text-text">{name}</span>
                {/* The loader has checked every listed example has a recorded answer. */}
                <span className="font-mono text-xs text-soft">
                  <span className="sr-only">answer </span>{block.answer!.value}
                  <span className="text-mute"> · <span className="sr-only">confidence </span>{block.answer!.confidence}</span>
                </span>
              </summary>
              <div className="px-1.5 pb-1.5">
                <Example block={block}>
                  <p><WithInlineCode text={shows} /></p>
                </Example>
              </div>
            </details>
          </li>
        ))}
      </ul>
    </div>
  );
}

function FieldTable({ context, input }: {
  context: { key: string; codeName: string; description: string; required: boolean }[];
  input: { key: string; codeName: string; description: string };
}) {
  return (
    <div className={`mt-4 ${TABLE_FRAME}`}>
      <table className="w-full text-left text-sm">
        <thead>
          <tr>
            <th className={TH}>Field</th>
            <th className={TH}>Kind</th>
            <th className={TH}>Description</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td className={`${TD} `}><FieldName codeName={input.codeName} fieldKey={input.key} /></td>
            <td className={`${TD} text-xs text-soft`}>input</td>
            <td className={`${TD} text-sm text-soft`}>{input.description}</td>
          </tr>
          {context.map((field) => (
            <tr key={field.key}>
              <td className={`${TD} `}><FieldName codeName={field.codeName} fieldKey={field.key} /></td>
              <td className={`${TD} text-xs text-soft`}>context{field.required ? '' : ' (optional)'}</td>
              <td className={`${TD} text-sm text-soft`}>{field.description}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function CatalogueAdapter() {
  const { entry, example, more, published, playground, call, numbers } = useLoaderData<typeof loader>();
  // Only the owner's view shows the Open / API only tag (lib/owner-view.ts); the page's sections still differ by kind.
  const ownerView = useOwnerView();
  return (
    <main id="main" className="mx-auto max-w-4xl px-4 py-10">
      <div className="flex flex-wrap items-center gap-2">
        {ownerView && <Tag tone={entry.kind === 'open' ? 'open' : 'accent'}>{entry.kind === 'open' ? 'Open' : 'API only'}</Tag>}
        <span className="font-mono text-xs text-mute">{entry.name}</span>
        <span className="text-xs text-mute">v{entry.version}</span>
        {entry.name !== entry.id && (
          <span className="text-xs text-mute">
            pinned to this version: <span className="font-mono">{entry.id}</span> (
            <Link to="/docs/guides/decide#section-pinning-a-version" className={TEXT_LINK}>Pinning a version</Link>)
          </span>
        )}
      </div>
      {/* Smaller on phones, so a long one-word title (Communications) fits the width without breaking. */}
      <h1 className="mt-3 break-words font-pixel text-pixel-20 text-text sm:text-pixel-30">{entry.title}</h1>
      <p className="mt-4 text-sm leading-7 text-soft">{entry.summary}</p>
      {playground !== null && (
        <p className="mt-5">
          <Link to={playground} className={`${BUTTON_SECONDARY} px-3 py-1`}>Open in playground</Link>
        </p>
      )}

      {example !== null && (
        <section className="mt-8">
          <h2 className={`border-b border-ink pb-2 ${SECTION_HEADING}`}>Example</h2>
          <Example block={example.block}>
            <p><WithInlineCode text={example.shows} /></p>
          </Example>
          {more.length > 0 && <MoreExamples examples={more} />}
        </section>
      )}

      <div className="mt-8 grid gap-4 sm:grid-cols-2">
        <section className={`${CARD} p-4`}>
          <h2 className="font-pixel text-pixel-20 text-text">Use when</h2>
          <ul className="mt-2 list-disc space-y-1.5 pl-5 text-sm leading-6 text-soft">
            {entry.useWhen.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </section>
        <section className={`${CARD} p-4`}>
          <h2 className="font-pixel text-pixel-20 text-text">Avoid when</h2>
          {entry.avoidWhen.length === 0 ? (
            <p className="mt-2 text-sm text-mute">No restrictions noted.</p>
          ) : (
            <ul className="mt-2 list-disc space-y-1.5 pl-5 text-sm leading-6 text-soft">
              {entry.avoidWhen.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          )}
        </section>
      </div>

      <section className="mt-10">
        <h2 className={`border-b border-ink pb-2 ${SECTION_HEADING}`}>Context and input</h2>
        <FieldTable context={entry.context} input={entry.input} />
      </section>

      <section className="mt-10">
        <h2 className={`border-b border-ink pb-2 ${SECTION_HEADING}`}>Functions</h2>
        <FunctionTable functions={entry.functions} />
      </section>

      <section className="mt-10">
        <h2 className={`border-b border-ink pb-2 ${SECTION_HEADING}`}>How it was measured</h2>
        <Measured numbers={numbers} />
      </section>

      <section className="mt-10">
        <h2 className={`border-b border-ink pb-2 ${SECTION_HEADING}`}>Base</h2>
        <p className="mt-2 text-sm text-soft">{entry.base}</p>
      </section>

      <section className="mt-10">
        <h2 className={`border-b border-ink pb-2 ${SECTION_HEADING}`}>Call it</h2>
        <p className="mt-2 text-sm leading-6 text-soft">
          {`${entry.functions[0].id}, with placeholders for its input and context.`}
        </p>
        <figure className="on-screen mt-4 border-2 border-ink bg-screen-frame" data-call={entry.id}>
          <CallCode code={call.code} curl={call.curl} local={call.local} name={`the call of ${entry.id}`} />
        </figure>
      </section>

      {entry.kind === 'open' && entry.weights !== null && entry.licence !== null ? (
        <section className="mt-10">
          <h2 className={`border-b border-ink pb-2 ${SECTION_HEADING}`}>Download</h2>
          <p className="mt-2 text-sm text-soft">
            Weights:{' '}
            <a
              href={`https://huggingface.co/${entry.weights.repo}/tree/${entry.weights.revision}`}
              className={TEXT_LINK}
              target="_blank"
              rel="noopener noreferrer"
            >
              {entry.weights.repo} ({entry.weights.revision})
            </a>
            {published ? null : ' (published at launch)'}
          </p>
          <p className="mt-1 text-sm text-soft">Licence: {entry.licence}</p>
        </section>
      ) : (
        <section className="mt-10">
          <h2 className={`border-b border-ink pb-2 ${SECTION_HEADING}`}>Download</h2>
          <p className="mt-2 text-sm text-soft">
            This adapter is available through the Jeff API only — see{' '}
            <Link to="/docs/guides/decide" className={TEXT_LINK}>
              Functions and /v1/decide
            </Link>.
          </p>
        </section>
      )}
    </main>
  );
}
