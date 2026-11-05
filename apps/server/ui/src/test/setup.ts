import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';

// vitest.config.ts does not enable the global test API, so Testing Library's own auto-cleanup (which looks for a
// global `afterEach`) never registers; do it explicitly.
afterEach(() => {
  cleanup();
  localStorage.clear();
  window.location.hash = '';
});

// jsdom has no layout engine: Range.getClientRects() is unimplemented, and CodeMirror 6 (the JsonEditor) calls it
// from its measuring pass. An empty rect list makes that pass a no-op instead of throwing.
if (typeof Range !== 'undefined' && !Range.prototype.getClientRects) {
  Range.prototype.getClientRects = function (this: Range) {
    return { length: 0, item: () => null, [Symbol.iterator]: () => [][Symbol.iterator]() } as unknown as DOMRectList;
  };
}

// jsdom has no scrolling either: Element.prototype.scrollTo is unimplemented, and the documentation page calls it
// (on the scrollable <main>) to jump to the top of a page it has just switched to. A no-op stands in for it.
if (typeof Element !== 'undefined' && !Element.prototype.scrollTo) {
  Element.prototype.scrollTo = function () {};
}
