# Jeff Pixel

Pixel capitals drawn on the square-cell grid of the Jeff logo (`apps/hub/app/assets/jeff_logo_v4.svg`): 7 cells tall,
2-cell vertical strokes, 1-cell horizontal strokes, square corners, letters 2 cells apart. The logo's JEFF set in this
font is pixel-identical to the logo.

- `glyphs.py`: every glyph as a cell bitmap (the source of the font). A-Z, 0-9, space, `. , : ; ! ? ' " - _ ( ) / & + # @ % * = < > [ ]`
  and U+25AE (the logo's block cursor). Lowercase letters map to the capitals.
- `build.py`: turns the bitmaps into `JeffPixel.ttf` and `JeffPixel.woff2` and checks them.
- `specimen.html`: every glyph and sample headings at 2, 3, 4 and 6 px cells (serve this folder over http to view).
- `LICENSE`: SIL Open Font License 1.1, Reserved Font Name "Jeff Pixel".

## Sizes

One cell is 100 of the 1000 units per em, so **font-size = 10 x cell px** draws whole-pixel cells: 20px (2 px cells),
30px (3), 40px (4), 60px (6). The default line height (`normal`) is 1.1em = 11 cells: 2 cells above the capitals,
2 below the baseline. Keep the text's left edge and baseline on whole pixels for crisp edges.

## Rebuild

From the repository root:

    uv run --no-project --with fonttools,brotli python packages/brand/fonts/jeff-pixel/build.py

The script merges each glyph's filled cells into non-overlapping outlines (outer contours clockwise, holes
anticlockwise), writes both files, reloads them and fails loud if the glyph count, the character map, an advance width
or any cell's coverage differs from `glyphs.py`.
