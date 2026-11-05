import { useEffect, useRef, useState } from 'react';
import { BUTTON_SECONDARY } from '../styles';

// The new key, shown this once (the hub keeps only its hash), with a copy button. It lives only in the page's
// memory: never in an address, in storage or in a log (the page drops it on pagehide, so the back-forward cache
// does not keep it). When it appears, focus moves to its heading, which names the key and is described by the sentence
// under it, so a screen reader reads both on the focus (a live region filled as it is added to the page is not
// reliably announced). The copy button's "Copied." goes into a status region that is on the page from the start.
export function CreatedKey({ name, keyText }: { name: string; keyText: string }) {
  const [copied, setCopied] = useState<{ ok: boolean; text: string } | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (heading.current === null) throw new Error('the new key\'s heading is not on the page');
    heading.current.focus();
  }, [keyText]);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(keyText);
      setCopied({ ok: true, text: 'Copied.' });
    } catch (error) {
      // The clipboard refused (no permission, an older browser): the customer is told, and copies it by hand.
      setCopied({ ok: false, text: `Could not copy (${(error as Error).message}); select the key and copy it by hand.` });
    }
  };
  return (
    <section className="mt-4 border-2 border-primary bg-primary-light p-4" aria-labelledby="new-key-heading">
      <h3 id="new-key-heading" ref={heading} tabIndex={-1} aria-describedby="new-key-once"
        className="font-mono text-sm font-medium text-text">{`Your new key "${name}"`}</h3>
      <p id="new-key-once" className="mt-1 text-sm text-text">This is the only time it is shown: copy it now.</p>
      <code className="mt-3 block break-all border border-ink bg-panel px-3 py-2 font-mono text-sm text-text">{keyText}</code>
      <button type="button" className={`${BUTTON_SECONDARY} mt-3 px-4 py-1.5`} onClick={() => void copy()}>Copy</button>
      <p role="status" className="mt-2 text-sm text-soft empty:hidden">{copied !== null && copied.ok ? copied.text : ''}</p>
      {copied !== null && !copied.ok && <p role="alert" className="mt-2 text-sm text-danger">{copied.text}</p>}
    </section>
  );
}
