// The home page's headline, set in Jeff Pixel, the font drawn on the logo's own grid (app.css: .font-pixel). It is
// real text, so screen readers, search and copy-paste get it as it reads. Each entry of HEADLINE_LINES is one line on
// a wide screen; on a phone a line wraps between its words.
import { Fragment } from 'react';

export const HEADLINE_LINES = ['A New Software', 'Primitive'];
export const HEADLINE = HEADLINE_LINES.join(' ');

export function HeroHeadline() {
  // 3 / 4 / 5 px cells at 30 / 40 / 50 px. The line height is the font's own line box (1.1em: 11 cells), so the
  // baselines stay on whole pixels and the lines sit 4 cells apart. 50 px, not 60, at the widest: the first line is
  // 168 cells wide, and 6 px cells would not fit the 992 px column.
  return (
    <h1 className="font-pixel text-pixel-30 leading-[1.1] text-text sm:text-pixel-40 lg:text-pixel-50">
      {HEADLINE_LINES.map((line, i) => (
        <Fragment key={line}>
          {i > 0 ? ' ' : null}
          <span className="block">{line}</span>
        </Fragment>
      ))}
    </h1>
  );
}
