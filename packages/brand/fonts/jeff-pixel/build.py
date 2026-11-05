"""Build Jeff Pixel (JeffPixel.ttf and JeffPixel.woff2) from the cell bitmaps in glyphs.py, then check the result.

Run from anywhere:  uv run --no-project --with fonttools,brotli python packages/brand/fonts/jeff-pixel/build.py

Geometry: one cell is 100 font units and the em is 1000 units (10 cells), so a font-size of 10 x N px draws cells of
exactly N px. Capitals are 7 cells tall on the baseline; the line box is 9 cells above the baseline and 2 below
(ascent 900, descent 200), leaving 2 cells of room above the capitals and 2 below the baseline (one of them for the
comma's descender). Each glyph carries 1 cell of side bearing on each side, so neighbouring letters sit 2 cells
apart, as in the logo.

Outlines: the filled cells of a glyph are merged into their outer boundary (and the boundaries of any holes), so a
glyph is a set of non-overlapping closed polygons, outer ones clockwise and holes anticlockwise, as TrueType wants.
"""

from pathlib import Path

from fontTools.agl import UV2AGL
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.recordingPen import RecordingPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

from glyphs import GLYPHS, SPACE_CELLS

HERE = Path(__file__).resolve().parent
CELL = 100
UPM = 1000
CAP_ROWS = 7
SIDE_BEARING = 1  # cells on each side of a glyph's ink
ASCENT = 900
DESCENT = 200
FAMILY = 'Jeff Pixel'
VERSION = '1.000'
COPYRIGHT = 'Copyright 2026 Mathias Strasser, with Reserved Font Name "Jeff Pixel".'


def glyph_name(ch: str) -> str:
    name = UV2AGL.get(ord(ch))
    if name is None:
        return f'uni{ord(ch):04X}'
    return name


def check_bitmap(ch: str, rows: list[str]) -> None:
    if len(rows) not in (CAP_ROWS, CAP_ROWS + 1):
        raise ValueError(f'glyph {ch!r} has {len(rows)} rows; it must have 7, or 8 with a descender')
    widths = {len(r) for r in rows}
    if len(widths) != 1:
        raise ValueError(f'glyph {ch!r} has rows of different widths: {sorted(widths)}')
    bad = {c for r in rows for c in r} - {'#', '.'}
    if bad:
        raise ValueError(f'glyph {ch!r} uses characters other than "#" and ".": {sorted(bad)}')


def filled_cells(rows: list[str]) -> set[tuple[int, int]]:
    """Filled cells as (x, y) of their bottom-left corner, in cells, y up, baseline at y = 0."""
    return {
        (col + SIDE_BEARING, CAP_ROWS - 1 - row)
        for row, line in enumerate(rows)
        for col, c in enumerate(line)
        if c == '#'
    }


Point = tuple[int, int]


def trace_outlines(cells: set[tuple[int, int]]) -> list[list[Point]]:
    """The boundary of the union of the cells, as closed loops of corner points (in cells).

    Every cell contributes its four edges, clockwise (y up); an edge shared by two filled cells appears once in each
    direction and cancels. What remains chains into loops: outer boundaries clockwise, holes anticlockwise. Where two
    cells touch only at a corner, the trace turns right, so the two shapes become separate loops meeting at a point.
    """
    edges: set[tuple[Point, Point]] = set()
    for x, y in cells:
        corners = [(x, y), (x, y + 1), (x + 1, y + 1), (x + 1, y)]
        for i in range(4):
            a, b = corners[i], corners[(i + 1) % 4]
            if (b, a) in edges:
                edges.remove((b, a))
            else:
                edges.add((a, b))
    outgoing: dict[Point, list[Point]] = {}
    for a, b in edges:
        outgoing.setdefault(a, []).append(b)

    loops: list[list[Point]] = []
    while outgoing:
        start = min(outgoing)
        loop = [start]
        prev, cur = start, outgoing[start].pop()
        if not outgoing[start]:
            del outgoing[start]
        while cur != start:
            loop.append(cur)
            options = outgoing[cur]
            if len(options) == 1:
                nxt = options.pop()
            else:
                d = (cur[0] - prev[0], cur[1] - prev[1])
                right = (d[1], -d[0])
                nxt = (cur[0] + right[0], cur[1] + right[1])
                if nxt not in options:
                    raise RuntimeError(f'outline trace found no right turn at {cur}: {options}')
                options.remove(nxt)
            if not options:
                del outgoing[cur]
            prev, cur = cur, nxt
        loops.append(drop_collinear(loop))
    return loops


def drop_collinear(loop: list[Point]) -> list[Point]:
    out = []
    n = len(loop)
    for i in range(n):
        p, q, r = loop[i - 1], loop[i], loop[(i + 1) % n]
        if (q[0] - p[0]) * (r[1] - q[1]) != (q[1] - p[1]) * (r[0] - q[0]):
            out.append(q)
    return out


def draw_glyph(rows: list[str]):
    pen = TTGlyphPen(None)
    for loop in trace_outlines(filled_cells(rows)):
        pen.moveTo((loop[0][0] * CELL, loop[0][1] * CELL))
        for x, y in loop[1:]:
            pen.lineTo((x * CELL, y * CELL))
        pen.closePath()
    return pen.glyph()


def notdef_rows() -> list[str]:
    return ['#####', '##.##', '#.#.#', '#..##', '#.#.#', '##.##', '#####']


def build() -> tuple[Path, Path]:
    for ch, rows in GLYPHS.items():
        check_bitmap(ch, rows)

    order = ['.notdef', 'space'] + [glyph_name(ch) for ch in GLYPHS]
    if len(set(order)) != len(order):
        raise ValueError('two glyphs share a name')
    cmap = {ord(' '): 'space'}
    for ch in GLYPHS:
        cmap[ord(ch)] = glyph_name(ch)
        if 'A' <= ch <= 'Z':
            cmap[ord(ch.lower())] = glyph_name(ch)  # lowercase draws the capitals

    bitmaps = {'.notdef': notdef_rows(), **{glyph_name(ch): rows for ch, rows in GLYPHS.items()}}
    glyf = {name: draw_glyph(rows) for name, rows in bitmaps.items()}
    glyf['space'] = TTGlyphPen(None).glyph()
    advances = {name: (len(rows[0]) + 2 * SIDE_BEARING) * CELL for name, rows in bitmaps.items()}
    advances['space'] = SPACE_CELLS * CELL

    fb = FontBuilder(UPM, isTTF=True)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap(cmap)
    fb.setupGlyf(glyf)
    glyf_table = fb.font['glyf']
    metrics = {}
    for name in order:
        g = glyf_table[name]
        lsb = g.xMin if g.numberOfContours else 0
        metrics[name] = (advances[name], lsb)
    fb.setupHorizontalMetrics(metrics)
    fb.setupHorizontalHeader(ascent=ASCENT, descent=-DESCENT, lineGap=0)
    fb.setupNameTable({
        'copyright': COPYRIGHT,
        'familyName': FAMILY,
        'styleName': 'Regular',
        'uniqueFontIdentifier': f'{FAMILY} Regular {VERSION}',
        'fullName': f'{FAMILY} Regular',
        'version': f'Version {VERSION}',
        'psName': 'JeffPixel-Regular',
        'designer': 'Mathias Strasser',
        'description': 'Pixel capitals drawn on the square-cell grid of the Jeff logo.',
        'licenseDescription': 'This Font Software is licensed under the SIL Open Font License, Version 1.1.',
        'licenseInfoURL': 'https://openfontlicense.org',
    })
    fb.setupOS2(
        sTypoAscender=ASCENT, sTypoDescender=-DESCENT, sTypoLineGap=0,
        usWinAscent=ASCENT, usWinDescent=DESCENT,
        sCapHeight=CAP_ROWS * CELL, sxHeight=CAP_ROWS * CELL,
        fsSelection=0x40 | 0x80,  # REGULAR, USE_TYPO_METRICS
        achVendID='JEFF', usWeightClass=400, version=4,
    )
    fb.setupPost()
    fb.font['head'].fontRevision = float(VERSION)

    ttf = HERE / 'JeffPixel.ttf'
    woff2 = HERE / 'JeffPixel.woff2'
    fb.save(str(ttf))
    font = TTFont(str(ttf))
    font.flavor = 'woff2'
    font.save(str(woff2))
    return ttf, woff2


def winding_at(loops: list[list[tuple[float, float]]], px: float, py: float) -> int:
    """Nonzero winding number of the outlines around a point (y up)."""
    w = 0
    for loop in loops:
        n = len(loop)
        for i in range(n):
            (x0, y0), (x1, y1) = loop[i], loop[(i + 1) % n]
            if y0 <= py < y1 or y1 <= py < y0:
                x = x0 + (py - y0) * (x1 - x0) / (y1 - y0)
                if x > px:
                    w += 1 if y1 > y0 else -1
    return w


def verify(path: Path) -> int:
    font = TTFont(str(path))
    expected_count = len(GLYPHS) + 2
    order = font.getGlyphOrder()
    if len(order) != expected_count:
        raise AssertionError(f'{path.name}: {len(order)} glyphs, expected {expected_count}')
    cmap = font.getBestCmap()
    wanted = {' '} | set(GLYPHS) | {c.lower() for c in GLYPHS if 'A' <= c <= 'Z'}
    missing = sorted(c for c in wanted if ord(c) not in cmap)
    if missing:
        raise AssertionError(f'{path.name}: cmap lacks {missing}')
    if font['head'].unitsPerEm != UPM:
        raise AssertionError(f'{path.name}: unitsPerEm {font["head"].unitsPerEm}')
    glyph_set = font.getGlyphSet()
    hmtx = font['hmtx']
    for ch, rows in GLYPHS.items():
        name = cmap[ord(ch)]
        adv, _ = hmtx[name]
        if adv != (len(rows[0]) + 2 * SIDE_BEARING) * CELL:
            raise AssertionError(f'{ch!r}: advance {adv}')
        rec = RecordingPen()
        glyph_set[name].draw(rec)
        loops, cur = [], []
        for op, args in rec.value:
            if op == 'moveTo':
                cur = [args[0]]
            elif op == 'lineTo':
                cur.append(args[0])
            elif op in ('closePath', 'endPath'):
                loops.append(cur)
            else:
                raise AssertionError(f'{ch!r}: unexpected outline operation {op}')
        # Every cell's centre must be wound exactly once if filled and not at all if empty: this checks the shape,
        # the winding direction and that no two contours overlap.
        for row, line in enumerate(rows):
            for col, c in enumerate(line):
                px = (col + SIDE_BEARING + 0.5) * CELL
                py = (CAP_ROWS - 1 - row + 0.5) * CELL
                w = winding_at(loops, px, py)
                want = -1 if c == '#' else 0  # clockwise loops wind -1 with this ray rule
                if w != want:
                    raise AssertionError(f'{ch!r} row {row} col {col}: winding {w}, expected {want}')
    return len(order)


if __name__ == '__main__':
    ttf, woff2 = build()
    for p in (ttf, woff2):
        n = verify(p)
        print(f'{p.name}: {p.stat().st_size} bytes, {n} glyphs, checks passed')
