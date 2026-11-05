// The JEFF wordmark with its block cursor, drawn inline (not as an <img>) so the letters take the surrounding text
// colour (currentColor) and the cursor the violet accent. The cells are the owner's logo (app/assets/
// jeff_logo_v4.svg, a 12-unit pixel grid) cropped to the letters: each '#' below is one 12x12 cell, and the
// cursor is a 7x7-cell square after them. test/logo.test.tsx checks these rows still match the asset. The cursor
// blinks (app.css: .jeff-logo-cursor) and stops for prefers-reduced-motion.
//
// The SVG's own <style> is not copied in: a <style> inside inline SVG applies to the whole page, so its classes
// (.ink, .cursor) would leak; the blink lives in app.css under a name of its own instead.

export const CELL = 12;

export const LETTER_ROWS = [
  '..#####..#######..#######..#######',
  '.....##..##.......##.......##.....',
  '.....##..##.......##.......##.....',
  '.....##..######...######...######.',
  '##...##..##.......##.......##.....',
  '##...##..##.......##.......##.....',
  '.#####...#######..##.......##.....',
];

// The cursor's position and size, in cells, on the same grid.
export const CURSOR = { col: 36, row: 0, size: 7 };

const WIDTH = (CURSOR.col + CURSOR.size) * CELL;
const HEIGHT = LETTER_ROWS.length * CELL;

export function Logo({ className = '' }: { className?: string }) {
  const cells: { col: number; row: number }[] = [];
  LETTER_ROWS.forEach((line, row) => {
    [...line].forEach((ch, col) => {
      if (ch === '#') cells.push({ col, row });
    });
  });
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      role="img"
      aria-label="Jeff"
      className={className}
      shapeRendering="crispEdges"
    >
      <g fill="currentColor">
        {cells.map(({ col, row }) => (
          <rect key={`${col}-${row}`} x={col * CELL} y={row * CELL} width={CELL} height={CELL} />
        ))}
      </g>
      {/* The cursor is drawn from the same cells as the letters, not one large square: at a browser zoom or density
          where a cell is a fraction of a pixel, crisp edges snap each shape on its own, and one 7-cell square could
          end a pixel short of the letters beside it. */}
      <g className="jeff-logo-cursor">
        {Array.from({ length: CURSOR.size * CURSOR.size }, (_, i) => (
          <rect key={i} x={(CURSOR.col + (i % CURSOR.size)) * CELL} y={(CURSOR.row + Math.floor(i / CURSOR.size)) * CELL}
            width={CELL} height={CELL} />
        ))}
      </g>
    </svg>
  );
}
