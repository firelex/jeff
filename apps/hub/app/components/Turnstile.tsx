import { useEffect, useRef } from 'react';

declare global {
  interface Window {
    turnstile?: {
      render: (element: HTMLElement, options: { sitekey: string; callback: (token: string) => void;
        'expired-callback': () => void; 'error-callback': () => void }) => string;
      remove: (widgetId: string) => void;
    };
  }
}

const SCRIPT = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';

// Cloudflare Turnstile's widget: calls onToken with a token once the visitor passes, and with null when it expires
// or fails. A token works for one check only, so the form draws a fresh widget (a new React key) after each send. The
// widget is removed from Turnstile when the component goes (or redraws), so no old widget keeps running behind it.
export function Turnstile({ siteKey, onToken }: { siteKey: string; onToken: (token: string | null) => void }) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let widgetId: string | null = null;
    const draw = () => {
      if (box.current === null) return;
      if (window.turnstile === undefined) throw new Error(`Cloudflare Turnstile's script loaded from ${SCRIPT} but did not define window.turnstile`);
      widgetId = window.turnstile.render(box.current, { sitekey: siteKey, callback: onToken, 'expired-callback': () => onToken(null),
        'error-callback': () => onToken(null) });
    };
    const remove = () => {
      if (widgetId === null) return;
      if (window.turnstile === undefined) throw new Error(`Cloudflare Turnstile's widget ${widgetId} is drawn, but window.turnstile is gone`);
      window.turnstile.remove(widgetId);
      widgetId = null;
    };
    if (window.turnstile !== undefined) { draw(); return remove; }
    let script = document.querySelector<HTMLScriptElement>(`script[src="${SCRIPT}"]`);
    if (script === null) {
      script = document.createElement('script');
      script.src = SCRIPT;
      script.async = true;
      document.head.appendChild(script);
    }
    script.addEventListener('load', draw);
    return () => { script.removeEventListener('load', draw); remove(); };
  }, [siteKey, onToken]);
  return <div ref={box} data-testid="turnstile" />;
}
