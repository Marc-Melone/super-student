"""Reading order for text laid out on a page (PDF text blocks, lines read from a scan)."""

from __future__ import annotations


def column_order(items: list, width_of, text_of, width: float) -> list:
    """Reading order for text pieces (x0, y0, x1, y1, ...) that may sit in columns.

    Anything spanning most of the width breaks the page into bands. Inside a band, if the pieces form
    separate columns of running text (gaps between them, and pieces long enough to be prose), each column is
    read top to bottom before the next; otherwise (tables, forms, one column) rows are read top to bottom,
    left to right, so a table row stays together."""
    out: list = []
    band: list = []

    def flush() -> None:
        if not band:
            return
        spans: list = []
        for x0, x1 in sorted((it[0], it[2]) for it in band):
            if spans and x0 <= spans[-1][1] + 0.02 * width:
                spans[-1][1] = max(spans[-1][1], x1)
            else:
                spans.append([x0, x1])
        words = sorted(len(str(text_of(it)).split()) for it in band)
        prose = words[len(words) // 2] >= 6
        if len(spans) > 1 and prose:
            def column(it) -> int:
                center = (it[0] + it[2]) / 2
                return next((i for i, (a, b) in enumerate(spans) if a - 1 <= center <= b + 1), 0)
            out.extend(sorted(band, key=lambda it: (column(it), it[1], it[0])))
        else:
            # rows: pieces whose tops are within half a line of each other read left to right
            heights = sorted(max(0.0, it[3] - it[1]) for it in band)
            tol = max(1e-9, 0.5 * heights[len(heights) // 2])
            rows: list = []
            for it in sorted(band, key=lambda it: it[1]):
                if rows and it[1] - rows[-1][0][1] <= tol:
                    rows[-1].append(it)
                else:
                    rows.append([it])
            for row in rows:
                out.extend(sorted(row, key=lambda it: it[0]))
        band.clear()

    for it in sorted(items, key=lambda it: (it[1], it[0])):
        if width_of(it) > 0.6 * width:
            flush()
            out.append(it)
        else:
            band.append(it)
    flush()
    return out
