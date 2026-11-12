"""
provenance.py - trace an ID back to the exact input file, worksheet, row and cell it came from.

For each run we index the ID columns of every source the analysis actually reads:
  * the MI workbook's three master sheets (Total Consumer MI Data / Total DTR MI / Total Feeder MI;
    see topology_check.load_master), and
  * every outage report in the run's data/ folder (first sheet of an .xlsx, or the .csv;
    see topology_check.read_any / classify_report).

The index lives in <job folder>/provenance.sqlite:
  sources(src, file, path, sheet, header_row, role)
  hits(value, src, row, col, used, start)
`row` / `col` are 1-based Excel coordinates in the original file, so "row 5,123" is the row the
user sees when they open the file themselves. `used` marks the row the analysis took when the
MI has duplicates (load_master keeps the LAST row per key). `start` is the outage start time
('YYYY-MM-DD HH:MM') on outage rows, so a breakdown label "<feeder> @ dd-mm HH:MM" can be
traced to its exact feeder-outage row.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import re
import sqlite3
import threading
from pathlib import Path

INDEX_NAME = "provenance.sqlite"
VERSION = "3"

# normalised header -> ID kind
ID_HEADERS = {"accountnumber": "consumer", "consumernumber": "consumer",
              "newmeterserialno": "meter", "meterid": "meter",
              "dtcode": "dt", "feedercode": "feeder"}
START_HEADERS = ("eventoccurred",)
# MI sheet -> header of the key load_master de-duplicates on (keep="last")
MI_SHEETS = {"Total Consumer MI Data": "accountnumber", "Total DTR MI": "newmeterserialno",
             "Total Feeder MI": "newmeterserialno"}
REPORT_HEADERS = ("consumernumber", "dtname", "feedercode")   # topology_check.classify_report
# Each source's own IDs (not foreign keys such as the Feeder Code on every consumer row,
# which would bloat the index and point a feeder at 300k consumer rows).
OWN_IDS = {"Total Consumer MI Data": {"consumer", "meter"}, "Total DTR MI": {"dt", "meter"},
           "Total Feeder MI": {"feeder", "meter"},
           "consumer": {"consumer", "meter"}, "dtr": {"meter"}, "feeder": {"feeder", "meter"}}


def report_kind(header) -> str | None:
    """Same rule as topology_check.classify_report."""
    h = {hnorm(c) for c in header if c is not None}
    return ("consumer" if "consumernumber" in h else "dtr" if "dtname" in h
            else "feeder" if "feedercode" in h else None)

_BREAKDOWN_RE = re.compile(r"^\s*(\S+)\s*@\s*(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})\s*$")
_locks: dict[str, threading.Lock] = {}
_status: dict[str, str] = {}
_glock = threading.Lock()


def hnorm(h) -> str:
    return re.sub(r"[^a-z0-9]", "", str(h or "").lower())


def norm_id(v):
    """Canonical form of an ID cell: int when it is a plain number, else upper-case text."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if v == v and v.is_integer() else None
    s = str(v).strip().lstrip("'").strip()
    if not s or s.lower() in ("nan", "none", "nat"):
        return None
    if re.fullmatch(r"\d{1,3}(,\d{3})+", s):
        s = s.replace(",", "")
    if re.fullmatch(r"\d+\.0+", s):
        s = s.split(".")[0]
    if s.isdigit() and len(s) <= 18 and (len(s) == 1 or s[0] != "0"):
        return int(s)
    return s.upper()


_TIME_FMTS = ("%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
              "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M")


def norm_time(v) -> str | None:
    if isinstance(v, dt.datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    s = str(v or "").strip()
    if not s:
        return None
    for f in _TIME_FMTS:
        try:
            return dt.datetime.strptime(s, f).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
    return None


def index_path(folder) -> Path:
    return Path(folder) / INDEX_NAME


def is_ready(folder) -> bool:
    p = index_path(folder)
    if not p.exists():
        return False
    try:
        con = sqlite3.connect(p)
        try:
            return con.execute("SELECT v FROM meta WHERE k='version'").fetchone() == (VERSION,)
        finally:
            con.close()
    except sqlite3.Error:
        return False


def status(folder) -> str:
    return _status.get(str(folder), "")


# ------------------------------------------------------------------ reading sources
def _xlsx_rows(path: Path, sheet: str | None):
    """(sheet name, iterator of (excel_row, values)) for one sheet; first sheet when sheet is None."""
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet] if sheet else wb.worksheets[0]
    try:
        ws.reset_dimensions()   # some writers store a wrong <dimension>; read every row
    except AttributeError:
        pass

    def gen():
        try:
            # read_only iter_rows yields every row from 1, padding blank ones, so the
            # enumerate index is the Excel row number.
            for r, vals in enumerate(ws.iter_rows(values_only=True), 1):
                yield r, vals
        finally:
            wb.close()
    return ws.title, gen()


def _csv_rows(path: Path):
    def gen():
        with open(path, encoding="utf-8", errors="ignore", newline="") as fh:
            for r, vals in enumerate(csv.reader(fh), 1):   # one Excel row per CSV record
                yield r, vals
    return None, gen()


def _find_header(rows, wanted, limit=30):
    """Consume rows until one has a header in `wanted`; return (row number, values) or (None, None)."""
    for r, vals in rows:
        if any(hnorm(v) in wanted for v in vals if v is not None):
            return r, vals
        if r >= limit:
            break
    return None, None


REF_CAP = 10   # rows kept per ID for columns that only reference it (e.g. DT Code on consumer rows)


def _index_source(con, src: int, rows, header, kinds: set, dedup: str | None, progress):
    own = {i for i, h in enumerate(header) if ID_HEADERS.get(hnorm(h)) in kinds}
    ref = {i for i, h in enumerate(header) if hnorm(h) in ID_HEADERS and i not in own}
    start_i = next((i for i, h in enumerate(header) if hnorm(h) in START_HEADERS), None)
    dedup_i = next((i for i, h in enumerate(header) if hnorm(h) == dedup), None) if dedup else None
    last_row: dict = {}
    ref_n: dict = {}
    batch, n = [], 0
    for r, vals in rows:
        n += 1
        start = None
        if start_i is not None and start_i < len(vals):
            start = norm_time(vals[start_i])
        used = 1 if dedup_i is None else 0
        for i in own:
            if i < len(vals):
                v = norm_id(vals[i])
                if v is not None:
                    batch.append((v, src, r, i + 1, used, start, 0))
        for i in ref:
            if i < len(vals):
                v = norm_id(vals[i])
                if v is not None:
                    k = (v, i)
                    c = ref_n[k] = ref_n.get(k, 0) + 1
                    if c <= REF_CAP:
                        batch.append((v, src, r, i + 1, used, start, 1))
        if dedup_i is not None and dedup_i < len(vals):
            k = norm_id(vals[dedup_i])
            if k is not None:
                last_row[k] = r
        if len(batch) >= 50_000:
            con.executemany("INSERT OR IGNORE INTO hits VALUES (?,?,?,?,?,?,?)", batch)
            batch.clear()
            progress(n)
    if batch:
        con.executemany("INSERT OR IGNORE INTO hits VALUES (?,?,?,?,?,?,?)", batch)
    con.executemany("INSERT OR REPLACE INTO reftotals VALUES (?,?,?,?)",
                    [(v, src, i + 1, c) for (v, i), c in ref_n.items() if c > REF_CAP])
    if dedup_i is not None and last_row:
        con.execute("CREATE TEMP TABLE IF NOT EXISTS used_rows (row INTEGER PRIMARY KEY)")
        con.execute("DELETE FROM used_rows")
        con.executemany("INSERT OR IGNORE INTO used_rows VALUES (?)", [(r,) for r in last_row.values()])
        con.execute("UPDATE hits SET used=1 WHERE src=? AND row IN (SELECT row FROM used_rows)", (src,))
    return n


def sources_for(folder: Path, mi_path: Path | None) -> list[tuple[Path, str | None, str]]:
    """[(file, sheet or None, role)] that the analysis reads, in display order."""
    out = []
    if mi_path and Path(mi_path).exists():
        out += [(Path(mi_path), s, "MI master") for s in MI_SHEETS]
    data = Path(folder) / "data"
    if data.is_dir():
        for p in sorted(data.iterdir()):
            if p.suffix.lower() in (".csv", ".xlsx") and not p.name.startswith("~$"):
                out.append((p, None, "outage report"))
    return out


def build(folder, mi_path, mi_label: str | None = None) -> Path:
    """(Re)build the provenance index of a run. Safe to call from a background thread."""
    folder = Path(folder)
    key = str(folder)
    with _glock:
        lock = _locks.setdefault(key, threading.Lock())
    with lock:
        if is_ready(folder):
            return index_path(folder)
        tmp = folder / (INDEX_NAME + ".tmp")
        tmp.unlink(missing_ok=True)
        con = sqlite3.connect(tmp)
        try:
            con.executescript("""
                PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
                CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
                CREATE TABLE sources (src INTEGER PRIMARY KEY, file TEXT, path TEXT, sheet TEXT,
                                      header_row INTEGER, role TEXT, headers TEXT);
                CREATE TABLE hits (value, src INTEGER, row INTEGER, col INTEGER, used INTEGER,
                                   start TEXT, ref INTEGER, PRIMARY KEY (value, src, row, col)) WITHOUT ROWID;
                CREATE TABLE reftotals (value, src INTEGER, col INTEGER, n INTEGER,
                                        PRIMARY KEY (value, src, col)) WITHOUT ROWID;
            """)
            src = 0
            for path, sheet, role in sources_for(folder, mi_path):
                label = path.name if role != "MI master" or not mi_label else mi_label
                try:
                    if path.suffix.lower() == ".csv":
                        title, rows = _csv_rows(path)
                    else:
                        title, rows = _xlsx_rows(path, sheet)
                except (KeyError, IndexError, OSError, ValueError) as e:
                    _status[key] = f"skipped {path.name}: {e}"
                    continue
                wanted = set(ID_HEADERS) | (set() if role == "MI master" else set(REPORT_HEADERS))
                hr, header = _find_header(rows, wanted)
                kind = sheet if role == "MI master" else (report_kind(header) if header else None)
                if hr is None or kind is None:
                    rows.close()   # not a report the analysis would have used
                    continue
                src += 1
                try:
                    rel = str(path.relative_to(folder))
                except ValueError:
                    rel = str(path)
                con.execute("INSERT INTO sources VALUES (?,?,?,?,?,?,?)",
                            (src, label, rel, title, hr, role,
                             json.dumps([None if h is None else str(h) for h in header])))
                where = f"{label}" + (f" / {title}" if title else "")
                _status[key] = f"indexing {where}"
                _index_source(con, src, rows, header, OWN_IDS[kind], MI_SHEETS.get(sheet) if role == "MI master" else None,
                              lambda n, w=where: _status.__setitem__(key, f"indexing {w}: {n:,} rows"))
                con.commit()
            con.execute("INSERT INTO meta VALUES ('version', ?)", (VERSION,))
            con.commit()
        finally:
            con.close()
        tmp.replace(index_path(folder))
        _status[key] = "ready"
        return index_path(folder)


def build_async(folder, mi_path, mi_label: str | None = None) -> None:
    key = str(folder)
    with _glock:
        lock = _locks.setdefault(key, threading.Lock())
        if lock.locked() or is_ready(folder):
            return
        _status[key] = "starting"

    def run():
        try:
            build(folder, mi_path, mi_label)
        except Exception as e:  # never crash the server thread; report it on the next trace
            _status[key] = f"error: {e}"
    threading.Thread(target=run, daemon=True, name=f"provenance-{Path(folder).name}").start()


# ------------------------------------------------------------------ lookup
def trace(folder, text: str, per_source: int = 50) -> dict:
    """Where does `text` (an ID or a breakdown label) appear in the run's input files?"""
    raw = str(text or "").strip()
    m = _BREAKDOWN_RE.match(raw)
    when = None
    if m:
        value = norm_id(m.group(1))
        when = f"%-{int(m.group(3)):02d}-{int(m.group(2)):02d} {int(m.group(4)):02d}:{m.group(5)}"
    else:
        value = norm_id(raw)
    out = {"query": raw, "value": None if value is None else str(value), "breakdown": bool(m), "groups": []}
    if value is None:
        return out
    con = sqlite3.connect(index_path(folder))
    try:
        srcs = {r[0]: r for r in con.execute("SELECT src, file, path, sheet, header_row, role, headers FROM sources")}
        q = "SELECT src, ref, row, col, used, start FROM hits WHERE value=?"
        args = [value]
        if when:
            q += " AND start LIKE ?"
            args.append(when)
        q += " ORDER BY ref, src, used DESC, row"
        groups: dict[tuple, list] = {}
        for s, rf, r, c, u, st in con.execute(q, args):
            groups.setdefault((s, rf), []).append((r, c, u, st))
        totals = {} if when else {
            (s, c): n for s, c, n in con.execute("SELECT src, col, n FROM reftotals WHERE value=?", (value,))}
    finally:
        con.close()
    from xlsx_goto import col_letter
    for (s, rf), hits in groups.items():
        _, file, path, sheet, hr, role, headers = srcs[s]
        headers = json.loads(headers)
        rows = [{"row": r, "col": c, "cell": f"{col_letter(c)}{r}",
                 "column": headers[c - 1] if c - 1 < len(headers) else "", "used": bool(u), "start": st}
                for r, c, u, st in hits[:per_source]]
        total = len(hits)
        if rf:
            total = max(total, sum(n for (ts, _), n in totals.items() if ts == s))
        out["groups"].append({"src": s, "file": file, "sheet": sheet, "header_row": hr, "role": role,
                              "reference": bool(rf), "csv": path.lower().endswith(".csv"), "total": total,
                              "used_row": next((h[0] for h in hits if h[2]), None)
                              if role == "MI master" and not rf else None,
                              "rows": rows})
    return out


def source(folder, src: int) -> dict | None:
    con = sqlite3.connect(index_path(folder))
    try:
        r = con.execute("SELECT file, path, sheet, header_row, role FROM sources WHERE src=?", (int(src),)).fetchone()
    finally:
        con.close()
    if not r:
        return None
    p = Path(r[1])
    return {"file": r[0], "path": p if p.is_absolute() else Path(folder) / p, "sheet": r[2],
            "header_row": r[3], "role": r[4]}
