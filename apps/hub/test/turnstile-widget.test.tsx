// The Turnstile widget component (app/components/Turnstile.tsx): it draws Cloudflare's widget once the script is there,
// and removes it from Turnstile, by the id render gave, when it goes or is redrawn.
import { render } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { Turnstile } from '../app/components/Turnstile';

afterEach(() => { delete window.turnstile; document.head.querySelectorAll('script').forEach((s) => s.remove()); });

function standIn() {
  const drawn: string[] = [];
  const removed: string[] = [];
  window.turnstile = { render: () => { drawn.push(`w${drawn.length + 1}`); return drawn[drawn.length - 1]; },
    remove: (id) => { removed.push(id); } };
  return { drawn, removed };
}

it('removes its widget, by the id render gave, when it goes and when it is drawn afresh', () => {
  const { drawn, removed } = standIn();
  const onToken = () => {};
  const view = render(<Turnstile key={0} siteKey="site" onToken={onToken} />);
  expect(drawn).toEqual(['w1']);
  view.rerender(<Turnstile key={1} siteKey="site" onToken={onToken} />);
  expect(drawn).toEqual(['w1', 'w2']);
  expect(removed).toEqual(['w1']);
  view.unmount();
  expect(removed).toEqual(['w1', 'w2']);
});

it('draws once the script loads, and removes what it drew', () => {
  const onToken = () => {};
  const view = render(<Turnstile siteKey="site" onToken={onToken} />);
  const script = document.head.querySelector('script');
  expect(script?.getAttribute('src')).toMatch(/^https:\/\/challenges\.cloudflare\.com\/turnstile\/v0\/api\.js/);
  const { drawn, removed } = standIn();
  script?.dispatchEvent(new Event('load'));
  expect(drawn).toEqual(['w1']);
  view.unmount();
  expect(removed).toEqual(['w1']);
});

it('removes nothing when the script never loaded', () => {
  const view = render(<Turnstile siteKey="site" onToken={() => {}} />);
  expect(() => view.unmount()).not.toThrow();
});
