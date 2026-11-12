"""
xlsx_goto.py - return a copy of an .xlsx that opens on a chosen sheet and cell.

Used by the "click a record -> open Excel at that cell" feature. The app is a
browser app, so it cannot drive the user's desktop Excel. It can, however,
hand back a workbook whose saved view state (active tab, selected cell,
scroll position) already points at the record. Excel restores that view
when the file is opened.

Only the small view-state XML parts are rewritten (workbook.xml and each
sheet's <sheetViews> header). Cell data, styles, filters, and freeze panes are
copied unchanged, and nothing is round-tripped through openpyxl, so large
workbooks stay fast and lossless.
"""
from __future__ import annotations

import io
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import quoteattr

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"

_CELL_RE = re.compile(r"^([A-Z]{1,3})([0-9]{1,7})$")
_ATTR_RE = re.compile(r'([A-Za-z_:][\w:.-]*)\s*=\s*("[^"]*"|\'[^\']*\')')


def col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


def _sheet_parts(z: zipfile.ZipFile) -> list[tuple[str, str]]:
    """[(sheet name, zip path of its XML)] in workbook tab order."""
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    targets = {r.get("Id"): r.get("Target") for r in rels.iter(f"{{{NS_PKG}}}Relationship")}
    out = []
    for sh in wb.iter(f"{{{NS_MAIN}}}sheet"):
        t = targets.get(sh.get(f"{{{NS_REL}}}id"), "")
        # openpyxl writes absolute targets ("/xl/worksheets/sheet1.xml"),
        # Excel writes relative ones ("worksheets/sheet1.xml").
        path = t.lstrip("/") if t.startswith("/") else posixpath.normpath(posixpath.join("xl", t))
        out.append((sh.get("name"), path))
    return out


def _attrs(tag: str) -> dict[str, str]:
    return {k: v[1:-1] for k, v in _ATTR_RE.findall(tag)}


def _tag(name: str, attrs: dict[str, str], close: bool) -> str:
    body = "".join(f" {k}={quoteattr(v)}" for k, v in attrs.items())
    return f"<{name}{body}{'/' if close else ''}>"


# Horizontal scroll heuristic, in Excel column-width units (~characters at 100% zoom).
FIT_WIDTH = 60      # if everything up to the target fits in this, don't scroll right at all
CONTEXT_WIDTH = 25  # otherwise keep up to this much width of columns left of the target


def _left_col(head: str, col: int, first: int) -> int:
    """First scrollable column to show so that `col` is on screen, using the sheet's
    <cols> widths (wide text columns make 'column H' far off-screen)."""
    if col <= first:
        return first
    fmt = re.search(r"<sheetFormatPr\b[^>]*/?>", head)
    fa = _attrs(fmt.group(0)) if fmt else {}
    try:
        default = float(fa.get("defaultColWidth") or float(fa.get("baseColWidth", 8)) + 0.43)
    except ValueError:
        default = 8.43
    widths: dict[int, float] = {}
    for m in re.finditer(r"<col\b[^>]*/?>", head):
        a = _attrs(m.group(0))
        try:
            lo, hi = int(a["min"]), int(a["max"])
            w = 0.0 if a.get("hidden") in ("1", "true") else float(a.get("width", default))
        except (KeyError, ValueError):
            continue
        for c in range(max(lo, first), min(hi, col) + 1):
            widths[c] = w

    def width(c: int) -> float:
        return widths.get(c, default)

    if sum(width(c) for c in range(first, col + 1)) <= FIT_WIDTH:
        return first
    left, ctx = col, 0.0
    while left - 1 >= first and ctx + width(left - 1) <= CONTEXT_WIDTH:
        left -= 1
        ctx += width(left)
    return left


def _target_sheet_views(head: str, row: int, col: int) -> str:
    """Build a <sheetViews> block that selects (row, col) and scrolls it into view,
    keeping the sheet's existing view options and freeze panes."""
    m = re.search(r"<sheetView\b[^>]*?/?>", head)
    view = _attrs(m.group(0)) if m else {}
    view.pop("tabSelected", None)
    view.pop("topLeftCell", None)
    view.setdefault("workbookViewId", "0")
    view["tabSelected"] = "1"

    pane_m = re.search(r"<pane\b[^>]*/>", head)
    pane = _attrs(pane_m.group(0)) if pane_m else {}
    frozen = pane.get("state") in ("frozen", "frozenSplit")
    x_split = int(float(pane.get("xSplit", 0) or 0)) if frozen else 0
    y_split = int(float(pane.get("ySplit", 0) or 0)) if frozen else 0

    # Leave a few rows / columns of context above and left of the target.
    top_row = max(y_split + 1, row - 4)
    left_col = _left_col(head, col, x_split + 1)
    cell = f"{col_letter(col)}{row}"
    top_left = f"{col_letter(left_col)}{top_row}"

    parts = []
    if frozen:
        pane["topLeftCell"] = top_left
        # The pane's topLeftCell only scrolls the bottom-right pane. With rows frozen
        # (and no columns), Excel takes the horizontal scroll of the whole window from the
        # sheetView's topLeftCell, so it must carry the target column too, or the sheet
        # opens at column A with the target off-screen. Likewise the row for columns-only.
        if y_split and not x_split:
            view["topLeftCell"] = f"{col_letter(left_col)}1"
        elif x_split and not y_split:
            view["topLeftCell"] = f"A{top_row}"
        active_pane = pane.get("activePane") or (
            "bottomRight" if x_split and y_split else "bottomLeft" if y_split else "topRight")
        pane["activePane"] = active_pane
        parts.append(_tag("pane", pane, close=True))
        parts.append(_tag("selection", {"pane": active_pane, "activeCell": cell, "sqref": cell}, close=True))
    else:
        view["topLeftCell"] = top_left
        parts.append(_tag("selection", {"activeCell": cell, "sqref": cell}, close=True))

    return "<sheetViews>" + _tag("sheetView", view, close=False) + "".join(parts) + "</sheetView></sheetViews>"


def _rewrite_sheet(xml: bytes, target: tuple[int, int] | None) -> bytes:
    """Rewrite only the part of a sheet's XML before <sheetData>."""
    i = xml.find(b"<sheetData")
    if i < 0:
        return xml
    head, tail = xml[:i].decode("utf-8"), xml[i:]
    if target is None:
        new_head = re.sub(r'\stabSelected\s*=\s*"[^"]*"', "", head)
    else:
        block = _target_sheet_views(head, *target)
        if re.search(r"<sheetViews\b", head):
            new_head = re.sub(r"<sheetViews\b.*?</sheetViews>|<sheetViews\b[^>]*/>", lambda _: block,
                              head, count=1, flags=re.S)
        else:
            # Schema order: sheetPr, dimension, sheetViews, sheetFormatPr, cols, sheetData
            m = re.search(r"<sheetFormatPr\b|<cols\b", head)
            at = m.start() if m else len(head)
            new_head = head[:at] + block + head[at:]
    return new_head.encode("utf-8") + tail


def _rewrite_workbook(xml: bytes, tab_index: int) -> bytes:
    s = xml.decode("utf-8")

    def fix(m: re.Match) -> str:
        a = _attrs(m.group(0))
        a["activeTab"] = str(tab_index)
        # Keep the active tab visible in the tab strip.
        if int(a.get("firstSheet", "0") or 0) > tab_index:
            a["firstSheet"] = str(tab_index)
        return _tag("workbookView", a, close=True)

    if re.search(r"<workbookView\b", s):
        s = re.sub(r"<workbookView\b[^>]*/>", fix, s, count=1)
    else:
        s = re.sub(r"(<workbookPr\b[^>]*/>|<workbookPr\b.*?</workbookPr>)",
                   lambda m: m.group(0) + f'<bookViews><workbookView activeTab="{tab_index}"/></bookViews>',
                   s, count=1, flags=re.S)
    return s.encode("utf-8")


def header_column(path, sheet: str, header_row: int, header: str) -> int | None:
    """1-based column index of `header` in `header_row` of `sheet`, or None."""
    from openpyxl import load_workbook
    want = str(header).strip()
    if not want:
        return None
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        if sheet not in wb.sheetnames:
            return None
        for row in wb[sheet].iter_rows(min_row=header_row, max_row=header_row, values_only=True):
            for i, v in enumerate(row, 1):
                if v is not None and str(v).strip() == want:
                    return i
    finally:
        wb.close()
    return None


def goto_cell(src, sheet: str, cell: str) -> bytes:
    """Return the bytes of `src` (.xlsx path or file object) set to open at sheet!cell."""
    m = _CELL_RE.match(cell.upper())
    if not m:
        raise ValueError(f"bad cell reference: {cell!r}")
    row, col = int(m.group(2)), col_index(m.group(1))
    if not (1 <= row <= 1_048_576 and 1 <= col <= 16_384):
        raise ValueError(f"cell out of range: {cell!r}")

    out = io.BytesIO()
    with zipfile.ZipFile(src) as zin:
        parts = _sheet_parts(zin)
        names = [n for n, _ in parts]
        if sheet not in names:
            raise KeyError(f"no sheet named {sheet!r}")
        tab = names.index(sheet)
        sheet_paths = {p: (n == sheet) for n, p in parts}
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                data = zin.read(info.filename)
                if info.filename == "xl/workbook.xml":
                    data = _rewrite_workbook(data, tab)
                elif info.filename in sheet_paths:
                    data = _rewrite_sheet(data, (row, col) if sheet_paths[info.filename] else None)
                zout.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
    return out.getvalue()
