// One snippet group on an SDK docs page (lib/snippet-block.ts): the same code in every SDK language the site shows,
// as tabs in the code's title bar beside the copy button, in a framed screen like a worked example's. The language is
// the visitor's one choice for the whole site (lib/code-language.ts), so picking Rust here shows Rust on every page.
// A group with both forms (through the API and on the reader's machine) also offers "On your machine (free)" and
// "Through the API" above the tabs, the visitor's one choice of where code runs (lib/code-mode.ts); a group
// with one form shows it with no choice. Under local code, a line says what it needs on the reader's machine.
import { useEffect, useId, useState } from 'react';
import { Link } from 'react-router';
import { onCodeLanguageChange, readStoredCodeLanguage, storeCodeLanguage } from '../../lib/code-language';
import { DEFAULT_CODE_MODE, onCodeModeChange, readStoredCodeMode, storeCodeMode, type CodeMode } from '../../lib/code-mode';
import { SDK_CODE_NAMES, SDK_LABELS, type SdkLanguage } from '../../lib/sdk-languages';
import type { SnippetBlock } from '../../lib/snippet-block';
import { CodeBlock } from './CodeBlock';
import { ModeChoice } from './Example';
import { ScreenTabs } from '../ScreenTabs';

const NOTE_LINK = 'text-screen-text underline underline-offset-2 hover:text-white';

// What local code needs: Python runs the models in its own process; every other SDK attaches to a Jeff engine.
function LocalNote({ language }: { language: SdkLanguage }) {
  return (
    <p className="mx-1.5 mb-1.5 bg-screen px-4 pb-3 text-xs leading-5 text-screen-text">
      {language === 'python'
        ? <>Runs the models in this process, with the engine package <code className="bg-white/10 px-1 font-mono">jeff-runtime</code> installed; the first run downloads the open adapters and base model from Hugging Face.</>
        : <>Attaches to a Jeff engine on your machine serving the adapters this code calls: start it with <code className="bg-white/10 px-1 font-mono">jeff runtime serve</code>, as <Link to="/docs/sdk/connect#section-on-your-machine-free" className={NOTE_LINK}>Connect</Link> shows.</>}
    </p>
  );
}

export function Snippet({ block }: { block: SnippetBlock }) {
  const id = useId();
  const [language, setLanguage] = useState<SdkLanguage>('python');
  const [mode, setMode] = useState<CodeMode>(DEFAULT_CODE_MODE);
  const { code, local } = block;
  const forms = [code, local].filter((form) => form !== null);
  if (forms.length === 0) throw new Error(`snippet ${block.id} has no code`);
  const offered = forms[0].map((c) => c.language).join(' ');
  if (forms.some((form) => form.map((c) => c.language).join(' ') !== offered)) {
    throw new Error(`snippet ${block.id}: its local code and its API code are in different languages`);
  }
  const showChoice = code !== null && local !== null;
  const shownLocal = local !== null && (code === null || mode === 'local');
  const shown = shownLocal ? local : code!;

  // After hydration (the prerendered page always shows Python through the API): the visitor's stored choices, and any
  // change of them made by another block on the page.
  useEffect(() => {
    const has = new Set(offered.split(' '));
    const follow = (next: SdkLanguage) => { if (has.has(next)) setLanguage(next); };
    const stored = readStoredCodeLanguage();
    if (stored !== null) follow(stored);
    return onCodeLanguageChange(follow);
  }, [offered]);
  useEffect(() => {
    if (!showChoice) return undefined;
    const stored = readStoredCodeMode();
    if (stored !== null) setMode(stored);
    return onCodeModeChange(setMode);
  }, [showChoice]);

  const tab = shown.find((c) => c.language === language);
  if (tab === undefined) throw new Error(`snippet ${block.id} has no ${language} code`);
  const tabs = shown.map((c) => ({ value: c.language, label: SDK_LABELS[c.language] }));
  return (
    <figure className="on-screen my-6 border-2 border-ink bg-screen-frame" data-snippet={block.id}>
      {showChoice && <ModeChoice mode={mode} canLocal onSelect={storeCodeMode} reasonId={`${id}-local-unavailable`} />}
      <CodeBlock
        code={tab.code}
        language={SDK_CODE_NAMES[language]}
        bar={<ScreenTabs idBase={id} tabs={tabs} selected={language} onSelect={storeCodeLanguage} controls={`${id}-panel`} />}
        framed={false}
        className=""
        panel={{ id: `${id}-panel`, labelledBy: `${id}-tab-${language}` }}
      />
      {shownLocal && <LocalNote language={language} />}
    </figure>
  );
}
