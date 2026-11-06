// The home page (spec Section 3.1): the pitch, the one example every reader can run -- in each SDK language the site
// shows (content/sdk-languages.yaml), switched by a language tab above the two columns -- three panels pointing at the guides that explain it, the catalogue's
// own counts. The example sits on a "screen" that boots with Jeff's banner and the catalogue's counts,
// types the example's lines in one after another and ends on READY. and a blinking cursor (app.css: .type-line, .screen-cursor; no animation for
// prefers-reduced-motion).
import { useEffect, useId, useState, type CSSProperties, type ReactNode } from 'react';
import { Link, useLoaderData } from 'react-router';
import { CodeBlock } from '../components/docs/CodeBlock';
import { readStoredCodeLanguage, storeCodeLanguage } from '../lib/code-language';
import { CODE_MODE_LABELS } from '../lib/code-mode';
import { engineServe } from '../lib/engine-socket';
import { SDK_CODE_NAMES, SDK_LABELS, type SdkLanguage } from '../lib/sdk-languages';
import { HeroHeadline } from '../components/HeroHeadline';
import { ScreenTabs } from '../components/ScreenTabs';
import { BUTTON_PRIMARY, BUTTON_SECONDARY, CARD_LINK, TEXT_LINK } from '../components/styles';
import { priceText, publicSettings } from '../lib/public-settings.server';
import { catalogue, homeExample } from '../lib/site.server';

export function loader() {
  const entries = catalogue();
  return {
    example: homeExample(),
    // Only the total (owner, 4 November 2026): the site no longer counts open and API-only adapters apart.
    adapterCount: entries.length,
    // The plan's price, a setting (public-settings.server.ts), as /pricing shows it.
    price: priceText(publicSettings().priceUsd),
  };
}

export function meta() {
  return [
    { title: 'Jeff | A New Software Primitive' },
    { name: 'description', content: 'Jeff: learned decision functions for any domain, answering in milliseconds with a typed value and a calibrated confidence.' },
  ];
}

const PANELS = [
  { title: 'Functions', to: '/docs/guides/decide', body: 'Call a named function directly and get a typed, calibrated decision back — never free text.' },
  { title: 'Graphs', to: '/docs/guides/graphs', body: 'Wire several functions together with declarative conditions, so the same graph runs locally or through the server.' },
  { title: 'Escalation', to: '/docs/guides/escalation', body: 'Decide what happens when a decision is not confident enough to act on, from handing it back to asking a person.' },
];

// When each line of the home screen appears, as if typed (app.css: .type-line): the banner and the catalogue's counts
// first, then the language tabs, the column headings and install lines, then the code from TYPED.startMs, one line
// every TYPED.stepMs; the closing lines (and READY. with the cursor) after the longest example would have finished.
// Only the lines' visibility is animated: the text, and what each Copy button copies, is the complete example from
// the start.
const TYPED = { startMs: 1000, stepMs: 70 };

function lineDelay(ms: number): CSSProperties {
  return { '--line-delay': `${ms}ms` } as CSSProperties;
}

// The engine the languages other than Python attach to on the reader's machine (the model runs only in it).
const ENGINE_SERVE = engineServe('jeff.safety.guard');
const LINK = 'font-medium text-screen-text underline underline-offset-2 hover:text-white';

// What each column says to install, given the package the language installs (content/sdk-languages.yaml).
function installLines(language: SdkLanguage, name: string): { local: string[]; remote: string[] } {
  if (language === 'python') return { local: [`pip install ${name} jeff-runtime`], remote: [`pip install ${name}`] };
  const add = {
    node: `npm install ${name}`,
    rust: `cargo add ${name} serde_json tokio --features tokio/macros,tokio/rt-multi-thread`,
    csharp: `dotnet add package ${name}`,
    // ${name} is the Maven coordinates (ai.jeffhub:jeff-sdk:<version>), as a Gradle dependency line.
    java: `implementation("${name}")`,
  }[language];
  return { local: [add, 'pip install jeff-runtime', ENGINE_SERVE], remote: [add] };
}

// The line under the example: how the call on your machine runs in the language, and where to read how to install it.
function note(language: SdkLanguage): ReactNode {
  if (language === 'python') {
    return (
      <>
        On a Mac or a CPU the same code runs either way; on an NVIDIA GPU, running on your machine takes two more
        settings (<Link to="/docs/sdk/connect#section-only-on-an-nvidia-gpu-the-prefix-pool" className={LINK}>Connect</Link>). The first
        local run downloads the open adapter and base model from Hugging Face. More on the packages:{' '}
        <Link to="/docs/sdk/install" className={LINK}>Install</Link>.
      </>
    );
  }
  const label = SDK_LABELS[language];
  return (
    <>
      {label} attaches to a Jeff engine running on your machine; the model runs in the engine (on an NVIDIA GPU,
      the engine takes two more options): <Link to={`/docs/sdk/${language}`} className={LINK}>Install the {label} SDK</Link>.
    </>
  );
}

function InstallLines({ lines }: { lines: string[] }) {
  return (
    <>
      {lines.map((line, i) => (
        <p key={line} className={`type-line ${i === 0 ? 'mt-1 ' : ''}whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-xs text-screen-dim`} style={lineDelay(800)}>
          {line}
        </p>
      ))}
    </>
  );
}

export default function Home() {
  const { example, adapterCount, price } = useLoaderData<typeof loader>();
  const idBase = useId();
  const [language, setLanguage] = useState<SdkLanguage>('python');
  const languages = example.map((e) => ({ value: e.language, label: SDK_LABELS[e.language] }));
  const offered = example.map((e) => e.language).join(' ');
  const typedEndMs = TYPED.startMs + (Math.max(...example.flatMap((e) => [e.local, e.remote].map((code) => code.trimEnd().split('\n').length))) + 1) * TYPED.stepMs;

  // Runs once, after hydration. The prerendered HTML always shows Python, and carries the other languages' panels too
  // (hidden), so there is no layout jump and no client fetch: this only flips which panels are hidden, from a value
  // read off this browser -- never from anything the server decided. A stored language this page does not show is
  // ignored.
  useEffect(() => {
    const stored = readStoredCodeLanguage();
    if (stored !== null && offered.split(' ').includes(stored)) setLanguage(stored);
  }, [offered]);

  const selectLanguage = (next: SdkLanguage) => {
    setLanguage(next);
    storeCodeLanguage(next);
  };

  const localPanelId = `${idBase}-panel-local`;
  const remotePanelId = `${idBase}-panel-remote`;

  return (
    <main id="main">
      <section className="dot-grid border-b border-ink">
        <div className="mx-auto max-w-5xl px-4 pt-16 pb-12 text-center sm:pt-20">
          <HeroHeadline />
          <p className="mx-auto mt-6 max-w-[62ch] text-base leading-7 text-soft">
            Turn learned decisions into ordinary function calls. Import an adapter, pass in unstructured data, and
            get a typed result with calibrated confidence. Run locally or through our API, in milliseconds.
          </p>
          <p className="mt-3 font-mono text-sm text-text">
            Free for local use. <Link to="/pricing" className={TEXT_LINK}>{`${price} a month for the API.`}</Link>
          </p>
          <div className="mt-8 flex flex-wrap items-center justify-center gap-4">
            <Link to="/docs/guides/quickstart" className={`${BUTTON_PRIMARY} px-5 py-2.5`}>
              Get started
            </Link>
            <Link to="/catalogue" className={`${BUTTON_SECONDARY} px-5 py-2.5`}>
              Browse the catalogue
            </Link>
          </div>
          <p className="mt-5 text-sm text-soft">
            New to this? <Link to="/docs/guides/system-1" className={TEXT_LINK}>What are System 1 models?</Link>
          </p>
        </div>

        <div className="mx-auto max-w-5xl px-4 pb-16">
          <div className="on-screen border-2 border-ink bg-screen-frame p-2.5 sm:p-5">
            <div className="bg-screen px-4 py-5 text-left text-screen-text sm:px-6 sm:py-6">
              <div className="font-pixel text-pixel-20 uppercase">
                <p className="type-line text-center" style={lineDelay(0)}>**** Jeff decision engine ****</p>
                <p className="type-line mt-2 text-center text-screen-dim" style={lineDelay(250)}>
                  {adapterCount} adapters loaded
                </p>
              </div>
              <div className="type-line mt-6" style={lineDelay(500)}>
                <ScreenTabs idBase={idBase} tabs={languages} selected={language} onSelect={selectLanguage} controls={`${localPanelId} ${remotePanelId}`} className="flex flex-wrap gap-1" />
              </div>
              {/* Side by side, each column is a subgrid of the same two rows -- headings and install lines, then the
                  code -- so both code boxes start, and end, level whatever each column's install lines take. */}
              <div className="mt-4 grid grid-cols-1 gap-y-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)] lg:grid-rows-[auto_1fr] lg:gap-x-6 lg:gap-y-0">
                {(['local', 'remote'] as const).map((mode) => (
                  <div key={mode} role="tabpanel" id={mode === 'local' ? localPanelId : remotePanelId} aria-labelledby={`${idBase}-tab-${language}`} className="min-w-0 lg:row-span-2 lg:grid lg:grid-cols-[minmax(0,1fr)] lg:grid-rows-subgrid">
                    <div>
                      <h2 className="type-line font-pixel text-pixel-20 text-screen-text" style={lineDelay(700)}>{CODE_MODE_LABELS[mode === 'local' ? 'local' : 'api']}</h2>
                      {example.map((e) => (
                        <div key={e.language} hidden={language !== e.language}>
                          <InstallLines lines={installLines(e.language, e.package)[mode]} />
                        </div>
                      ))}
                    </div>
                    <div className="mt-3 min-w-0">
                      {example.map((e) => (
                        <div key={e.language} hidden={language !== e.language} className="h-full">
                          <CodeBlock code={e[mode]} language={SDK_CODE_NAMES[e.language]} typed={TYPED} fill wrap className="" />
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
              <p className="type-line mt-5 text-sm leading-6 text-screen-dim" style={lineDelay(typedEndMs)}>
                {example.map((e) => <span key={e.language} hidden={language !== e.language}>{note(e.language)}</span>)}
              </p>
              <p className="type-line mt-4 font-pixel text-pixel-20 uppercase" style={lineDelay(typedEndMs + 150)}>
                Ready.
                <br />
                <span className="screen-cursor" aria-hidden />
              </p>
            </div>
          </div>
        </div>
      </section>

      <section className="mx-auto max-w-5xl px-4 pt-12 pb-16">
        {/* Whole-pixel thirds, so the pixel-font titles start on whole pixels (as in the catalogue's grid). */}
        <div className="grid gap-6 sm:grid-cols-[repeat(2,round(down,calc((100%_-_3rem)/3),1px))_minmax(0,1fr)]">
          {PANELS.map((panel) => (
            <Link
              key={panel.title}
              to={panel.to}
              className={`flex flex-col gap-2 p-6 ${CARD_LINK}`}
            >
              <h2 className="cursor-mark font-pixel text-pixel-20 text-text">{panel.title}</h2>
              <p className="text-sm leading-6 text-soft">{panel.body}</p>
            </Link>
          ))}
        </div>
        <p className="mt-10 text-center font-mono text-sm text-mute">
          {adapterCount} adapters
        </p>
      </section>

    </main>
  );
}
