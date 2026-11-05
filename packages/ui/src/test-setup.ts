import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';

// vitest.config.ts does not enable the global test API, so Testing Library's own
// auto-cleanup (which looks for a global `afterEach`) never registers; do it explicitly.
afterEach(() => {
  cleanup();
});

// jsdom has no real layout engine: Range.getClientRects() is unimplemented, which
// CodeMirror 6 calls from its requestAnimationFrame-driven measuring pass. Stand in
// with an empty rect list so that pass is a no-op instead of throwing. Guarded because
// this file also runs for the Node-environment Tailwind-compile test, which has no DOM.
if (typeof Range !== 'undefined' && !Range.prototype.getClientRects) {
  Range.prototype.getClientRects = function (this: Range) {
    return { length: 0, item: () => null, [Symbol.iterator]: () => [][Symbol.iterator]() } as unknown as DOMRectList;
  };
}
