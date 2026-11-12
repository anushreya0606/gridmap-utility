"""
app.py  -  website for the Feeder / DTR / Consumer mapping checks.

    pip install -r requirements.txt
    python app.py                                   # local computer
    python app.py --host 0.0.0.0 --port 8000        # reachable on network

Uploaded outage events are kept in store/events.sqlite, runs in jobs/ - both survive restarts.
See DEPLOY.md for deployment options.

Unbiased by design:
  * The same written rules apply to every feeder, DTR, and consumer;
  * Every threshold is visible, configurable, and recorded with results;
  * "No data" is never counted as a mismatch;
  * Counts are shown both with and without suspect breakdowns;
  * Results are deterministic (same input produces identical output).
"""
from __future__ import annotations
import argparse
import json
import os
import pickle
import secrets
import shutil
import sqlite3
import threading
import time
import traceback
import re
import uuid
from pathlib import Path

import pandas as pd
from markupsafe import escape
from flask import (Flask, request, redirect, url_for, render_template, send_from_directory,
                   abort, jsonify, session)

import topology_check as tc
import breakdown_check as bc
import build_hierarchy as bh
from run_check import load_reports, write_report
import provenance
import excel_open

# Workspace data directory; set GRIDMAP_DATA to a persistent disk path in deployment
DATA_DIR = Path(os.environ.get("GRIDMAP_DATA", Path(__file__).resolve().parent))
# PUBLIC mode: every browser gets an isolated private workspace via cookie, purged after KEEP_DAYS
PUBLIC = os.environ.get("GRIDMAP_PUBLIC", "0") == "1"
KEEP_DAYS = int(os.environ.get("GRIDMAP_KEEP_DAYS", "7"))
DATA_DIR.mkdir(parents=True, exist_ok=True)


class WS:
    """One workspace: its runs (jobs/) and its stored events + MI (store/)."""
    def __init__(self, root: Path):
        self.root = Path(root)
        self.base = self.root / "jobs"
        self.store = self.root / "store"
        for d in (self.base, self.store):
            d.mkdir(parents=True, exist_ok=True)
        self.db = self.store / "events.sqlite"
        self.mi_latest = self.store / "mi_latest.xlsx"
        self.mi_meta = self.store / "mi_latest.json"

        # Seed initial store from template store/ if available and newly initialized
        seed_store = _here / "store"
        if seed_store.exists():
            if not self.mi_latest.exists() and (seed_store / "mi_latest.xlsx").exists():
                try:
                    shutil.copy2(seed_store / "mi_latest.xlsx", self.mi_latest)
                    if (seed_store / "mi_latest.json").exists():
                        shutil.copy2(seed_store / "mi_latest.json", self.mi_meta)
                except Exception:
                    pass
            if not self.db.exists() and (seed_store / "events.sqlite").exists():
                try:
                    shutil.copy2(seed_store / "events.sqlite", self.db)
                except Exception:
                    pass

    def touch(self):
        try:
            (self.root / ".last_used").write_text(str(time.time()))
        except OSError:
            pass


def cur_ws() -> WS:
    if not PUBLIC:
        return WS(DATA_DIR)
    wid = request.args.get("ws") or session.get("wid")
    if not wid or not re.fullmatch(r"[0-9a-f]{32}", wid):
        wid = secrets.token_hex(16)
        session["wid"] = wid
        session.permanent = True
    elif "wid" not in session:
        session["wid"] = wid
        session.permanent = True
    return WS(DATA_DIR / "workspaces" / wid)


def cleanup_workspaces():
    if not PUBLIC:
        return
    root = DATA_DIR / "workspaces"
    if not root.exists():
        return
    limit = time.time() - KEEP_DAYS * 86400
    for w in root.iterdir():
        f = w / ".last_used"
        try:
            last = float(f.read_text()) if f.exists() else w.stat().st_mtime
        except (OSError, ValueError):
            last = 0
        if last < limit and not any(j.get("ws") == str(w) and j.get("state") == "running" for j in JOBS.values()):
            for jid in [k for k, j in JOBS.items() if j.get("ws") == str(w)]:
                JOBS.pop(jid, None)
            shutil.rmtree(w, ignore_errors=True)


_here = Path(__file__).resolve().parent
app = Flask(__name__, template_folder=str(_here / "templates"), static_folder=str(_here / "static"))
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024   # 1 GB upload limit
_secret_file = DATA_DIR / ".secret_key"
if not _secret_file.exists():
    _secret_file.write_text(secrets.token_hex(32))
app.secret_key = os.environ.get("SECRET_KEY") or _secret_file.read_text().strip()
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * max(KEEP_DAYS, 1)
_is_https = os.environ.get("GRIDMAP_PUBLIC", "0") == "1" or os.environ.get("SPACE_ID") is not None
app.config["SESSION_COOKIE_SAMESITE"] = "None" if _is_https else "Lax"
app.config["SESSION_COOKIE_SECURE"] = True if _is_https else False
JOBS: dict[str, dict] = {}
RUN_LOCK = threading.Lock()                     # single analysis at a time to keep RAM bounded
TABLES = {"consumer": "consumer_events", "dtr": "dtr_events", "feeder": "feeder_events"}


# ------------------------------------------------------------------ stored events
def db(ws: WS):
    con = sqlite3.connect(ws.db, timeout=60)
    for kind, t in TABLES.items():
        extra = ", consumer_number TEXT" if kind == "consumer" else ""
        con.execute(f"CREATE TABLE IF NOT EXISTS {t} (meter_id TEXT, start TEXT, restore TEXT{extra}, "
                    f"source_file TEXT, run_id TEXT, PRIMARY KEY (meter_id, start))")
        con.execute(f"CREATE INDEX IF NOT EXISTS ix_{t}_start ON {t}(start)")
    con.commit()
    return con


def store_events(ws: WS, ev: dict, run_id: str) -> dict:
    """Add newly uploaded events to the store; duplicates (same meter + start) are skipped."""
    added = {}
    con = db(ws)
    try:
        for kind, t in TABLES.items():
            e = ev[kind]
            if e.empty:
                added[kind] = 0
                continue
            cols = ["meter_id", "start", "restore"] + (["consumer_number"] if kind == "consumer" else [])
            rows = e[cols].copy()
            rows["start"] = rows["start"].dt.strftime("%Y-%m-%d %H:%M:%S")
            rows["restore"] = rows["restore"].dt.strftime("%Y-%m-%d %H:%M:%S")
            rows = rows.astype(object).where(rows.notna(), None)
            before = con.total_changes
            con.executemany(f"INSERT OR IGNORE INTO {t} ({','.join(cols)}, source_file, run_id) "
                            f"VALUES ({','.join('?' * (len(cols) + 2))})",
                            [tuple(r) + ("upload", run_id) for r in rows.itertuples(index=False, name=None)])
            con.commit()
            added[kind] = con.total_changes - before
    finally:
        con.close()
    return added


def load_stored(ws: WS, days: int | None, date_from: str | None = None, date_to: str | None = None) -> dict:
    con = db(ws)
    try:
        out = {}
        for kind, t in TABLES.items():
            q, args = f"SELECT * FROM {t}", []
            cond = []
            if days:
                cond.append(f"start >= datetime((SELECT MAX(start) FROM {t}), ?)")
                args.append(f"-{int(days)} days")
            if date_from:
                cond.append("start >= ?"); args.append(date_from + " 00:00:00")
            if date_to:
                cond.append("start <= ?"); args.append(date_to + " 23:59:59")
            if cond:
                q += " WHERE " + " AND ".join(cond)
            df = pd.read_sql(q, con, params=args)
            df["kind"] = kind
            for c in ("start", "restore"):
                df[c] = pd.to_datetime(df[c], errors="coerce").astype("datetime64[ns]")
            if kind != "consumer":
                df["consumer_number"] = None
            out[kind] = df
        return out
    finally:
        con.close()


def stored_overview(ws: WS) -> pd.DataFrame:
    con = db(ws)
    try:
        parts = []
        for kind, t in TABLES.items():
            d = pd.read_sql(f"SELECT date(start) AS day, COUNT(*) AS n FROM {t} GROUP BY day", con)
            d["kind"] = kind
            parts.append(d)
        d = pd.concat(parts)
        if d.empty:
            return pd.DataFrame(columns=["Day", "Consumer events", "DTR events", "Feeder events"])
        p = d.pivot_table(index="day", columns="kind", values="n", aggfunc="sum", fill_value=0)
        p = p.reindex(columns=["consumer", "dtr", "feeder"], fill_value=0).reset_index()
        p.columns = ["Day", "Consumer events", "DTR events", "Feeder events"]
        return p.sort_values("Day", ascending=False)
    finally:
        con.close()


# ------------------------------------------------------------------ persistence
def save_job(job_id: str):
    j = dict(JOBS[job_id])
    try:
        with open(Path(j["folder"]) / "job.pkl", "wb") as fh:
            pickle.dump(j, fh)
    except Exception:
        pass


def load_saved_jobs():
    files = list((DATA_DIR / "jobs").glob("*/job.pkl")) + list((DATA_DIR / "workspaces").glob("*/jobs/*/job.pkl"))
    for f in files:
        try:
            with open(f, "rb") as fh:
                j = pickle.load(fh)
            # Jobs copied from another machine keep that machine's absolute paths; re-root them here.
            if not Path(j.get("folder") or "").is_dir():
                j["folder"] = str(f.parent)
                j["ws"] = str(f.parent.parent.parent)
            j.setdefault("ws", str(f.parent.parent.parent))
            if j.get("state") == "running":
                j["state"] = "error"
                j["error"] = "The server was restarted while this analysis was running."
            JOBS[f.parent.name] = j
        except Exception:
            continue


# ------------------------------------------------------------------ settings
SETTINGS = [
    # key, label, default, type, help
    ("tol", "Timing tolerance (minutes)", 2, int,
     "An outage matches if it starts within ± tolerance of the feeder breakdown window."),
    ("suspect_pct", "Suspect breakdown below (% of feeder's consumers)", 10, float,
     "If fewer than this % of a feeder's consumers went off, the breakdown is flagged as suspect."),
    ("max_events", "Faulty meter threshold (events in one day)", 40, int,
     "Meters reporting more events than this in a day are excluded as flapping/faulty."),
    ("min_alt_recall", "Wrong-mapping: must follow other source (%)", 60, float,
     "A consumer is flagged only if it followed at least this % of the suggested source's outages..."),
    ("max_rec_recall", "Wrong-mapping: follows recorded source at most (%)", 40, float,
     "... and at most this % of its recorded source's outages."),
]


def parse_settings(form) -> dict:
    out = {}
    for key, _, default, typ, _ in SETTINGS:
        try:
            out[key] = typ(form.get(key, default))
        except (TypeError, ValueError):
            out[key] = default
    return out


# ------------------------------------------------------------------ breakdown checks database
def write_checks_sqlite(bres: dict, db_path: Path):
    if db_path.exists():
        return
    con = sqlite3.connect(db_path)
    try:
        for key, tab_name in [("fd_c", "feeder_consumer"), ("fd_dt", "feeder_dtr"), ("dt_c", "dtr_consumer")]:
            df = bres.get(key)
            if df is not None and not df.empty:
                d = df.copy()
                d.columns = [str(c).replace(' ', '_').replace('(', '').replace(')', '').replace('&', 'and').replace('/', '_') for c in d.columns]
                for c in d.columns:
                    if pd.api.types.is_datetime64_any_dtype(d[c]):
                        d[c] = d[c].dt.strftime("%Y-%m-%d %H:%M:%S")
                d.to_sql(tab_name, con, if_exists="replace", index=False)
        con.execute("CREATE INDEX IF NOT EXISTS ix_fc_cons ON feeder_consumer(Consumer_No)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_fc_meter ON feeder_consumer(Meter_ID)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_fc_feeder ON feeder_consumer(Feeder_code)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_fc_dt ON feeder_consumer(DT_code)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_fc_result ON feeder_consumer(Result)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_fd_feeder ON feeder_dtr(Feeder_code)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_fd_dt ON feeder_dtr(DT_code)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_fd_result ON feeder_dtr(Result)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_dc_cons ON dtr_consumer(Consumer_No)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_dc_meter ON dtr_consumer(Meter_ID)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_dc_dt ON dtr_consumer(DT_code)")
        con.commit()
    finally:
        con.close()


def ensure_checks_sqlite(folder: Path):
    db_path = folder / "checks.sqlite"
    if db_path.exists():
        return db_path
    xlsx = folder / "Breakdown_Mapping_Check.xlsx"
    if not xlsx.exists():
        return None
    from openpyxl import load_workbook
    wb = load_workbook(xlsx, read_only=True)
    con = sqlite3.connect(db_path)
    try:
        for sheet, tab in [("Feeder_Consumer_Breakdowns", "feeder_consumer"),
                           ("Feeder_DTR_Breakdowns", "feeder_dtr"),
                           ("DTR_Consumer_Breakdowns", "dtr_consumer")]:
            if sheet not in wb.sheetnames:
                continue
            ws = wb[sheet]
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                continue
            hdr = [str(c).replace(' ', '_').replace('(', '').replace(')', '').replace('&', 'and').replace('/', '_') for c in rows[0]]
            con.execute(f"CREATE TABLE IF NOT EXISTS {tab} (" + ", ".join(f"[{c}] TEXT" for c in hdr) + ")")
            con.executemany(f"INSERT INTO {tab} VALUES (" + ", ".join(["?"] * len(hdr)) + ")",
                            [[str(v) if v is not None else "" for v in r] for r in rows[1:]])
        con.execute("CREATE INDEX IF NOT EXISTS ix_fc_cons ON feeder_consumer(Consumer_No)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_fc_meter ON feeder_consumer(Meter_ID)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_fc_feeder ON feeder_consumer(Feeder_code)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_fc_dt ON feeder_consumer(DT_code)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_fc_result ON feeder_consumer(Result)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_fd_feeder ON feeder_dtr(Feeder_code)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_fd_dt ON feeder_dtr(DT_code)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_fd_result ON feeder_dtr(Result)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_dc_cons ON dtr_consumer(Consumer_No)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_dc_meter ON dtr_consumer(Meter_ID)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_dc_dt ON dtr_consumer(DT_code)")
        con.commit()
    finally:
        con.close()
    return db_path


# ------------------------------------------------------------------ background job
def run_job(job_id: str):
    job = JOBS[job_id]
    folder = Path(job["folder"])
    ws = WS(job["ws"])
    log = lambda m: job["log"].append(f"{time.strftime('%H:%M:%S')}  {m}")
    s = job["settings"]
    if not RUN_LOCK.acquire(blocking=False):
        log("Another analysis is in progress; queuing to start when it finishes...")
        RUN_LOCK.acquire()
    try:
        log("Reading MI master workbook...")
        if job["mi_uploaded"]:
            mi_path = folder / "mi" / job["mi_name"]
        else:
            mi_path = ws.mi_latest
            log(f"Using stored MI master: {job['mi_name']}")
        cm, dm, fm = tc.load_master(mi_path)
        if job["mi_uploaded"]:
            shutil.copyfile(mi_path, ws.mi_latest)
            ws.mi_meta.write_text(json.dumps({"name": job["mi_name"], "uploaded": job["created"]}))
        log(f"MI loaded: {len(cm):,} consumers, {dm['dt_code'].nunique():,} DTRs, {fm['feeder_code'].nunique():,} feeders")
        log("Reading outage reports...")
        found, raw = [], {}
        ev_new = load_reports(folder / "data", log=lambda m: found.append(m.strip()), raw_rows=raw)
        job["files"] = found or ["(stored events only)"]
        added = store_events(ws, ev_new, job_id)
        job["added"] = added
        log("Stored new events: " + ", ".join(f"{k} {v:,}" for k, v in added.items()))
        scope = job["scope"]
        if scope == "upload":
            ev = ev_new
        else:
            ev = load_stored(ws, job.get("days") if scope == "days" else None,
                             job.get("date_from") if scope == "range" else None,
                             job.get("date_to") if scope == "range" else None)
            raw = {k: len(v) for k, v in ev.items()}
            raw.update({k + "_ok": len(v) for k, v in ev.items()})
            log("Loaded from event store: " + ", ".join(f"{k} {len(v):,}" for k, v in ev.items()))
        for k in ev:
            log(f"{k} events to process: {len(ev[k]):,}")
        job["checks"] = tc.data_checks(cm, dm, fm, ev, raw)
        for _, c in job["checks"].iterrows():
            if c["Status"] != "OK":
                log(f"{c['Status']}: {c['Area']} - {c['Check']}: {c['Value']}. {c['What it means']}")
        errs = job["checks"][job["checks"]["Status"] == "ERROR"]
        if len(errs) or ev["consumer"].empty or ev["feeder"].empty:
            raise ValueError("The uploaded data cannot be analysed: " + "; ".join(
                f"{c['Area']} - {c['Check']}: {c['What it means'] or c['Value']}" for _, c in errs.iterrows())
                + (" Required outage reports must contain standard columns (consumer_number / dt_name / feeder_code, meter_id, event_occurred, event_restored)."
                   if ev["consumer"].empty or ev["feeder"].empty else ""))

        log("Step 1/3: Breakdown-time check (Feeder->DTR, Feeder->Consumer, DTR->Consumer)...")
        bres = bc.check(ev, cm, dm, fm, s["tol"], s["max_events"], s["suspect_pct"] / 100)
        bc.write(bres, folder / "Breakdown_Mapping_Check.xlsx")
        try:
            write_checks_sqlite(bres, folder / "checks.sqlite")
        except Exception as _e:
            log(f"Notice: checks indexing: {_e}")
        feeders, tree, levels = bc.feeder_tables(bres)
        job["breakdown"] = {"summary": bres["summary"], "feeders": feeders, "tree": tree, "levels": levels,
                            "status": bres["breakdowns"], "n": bres["n_breakdowns"]}

        log("Step 2/3: Mapping check from outage patterns...")
        st = tc.Settings(window_min=s["tol"], max_events_per_day=s["max_events"],
                         min_alt_recall=s["min_alt_recall"] / 100, max_rec_recall=s["max_rec_recall"] / 100)
        r = tc.analyse(ev["consumer"], ev["dtr"], ev["feeder"], cm, dm, fm, st, log=log)
        write_report(r, folder / "Mapping_Report.xlsx", cm, run_label=time.strftime("%Y-%m-%d %H:%M"))
        job["mapping"] = {"summary": r["summary"], "dtr_check": r["dtr_check"], "patterns": r["patterns"],
                          "suspects": r["suspects"], "wrong": r["wrong"]}

        log("Step 3/3: Feeder -> DTR -> Consumer -> time hierarchy workbook...")
        bh.build(r, ev, cm, dm, fm, folder / "Feeder_DTR_Consumer.xlsx", s["tol"])
        log("Writing combined results workbook (All_Results.xlsx)...")
        write_combined(job, folder / "All_Results.xlsx")
        job["state"] = "done"
        log("All checks and workbooks completed successfully.")
        start_provenance(job)   # index source rows for click-to-trace, in the background
    except Exception as e:
        job["state"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"
        log("ERROR: " + job["error"])
        job["trace"] = traceback.format_exc()
    finally:
        RUN_LOCK.release()
        save_job(job_id)


# ------------------------------------------------------------------ write combined workbook
SUMMARY_GAP = 4  # rows from one Summary block's title to the next = len(df) + SUMMARY_GAP


def combined_summary_blocks(job: dict) -> list[tuple[str, pd.DataFrame]]:
    """The (title, table) blocks stacked on the Summary sheet of All_Results.xlsx."""
    s, b, m = job["settings"], job["breakdown"], job["mapping"]
    ms = m["summary"]
    bs = b["summary"]
    rules = pd.DataFrame([(lab, s[k]) for k, lab, *_ in SETTINGS] + [
        ("Run time", job["created"]), ("MI file", job["mi_name"]), ("Outage files", " | ".join(job["files"]))],
        columns=["Setting", "Value"])
    dtr_tab = pd.DataFrame([
        ("Correct feeder", ms["dtr_correct"]), ("Wrong feeder", ms["dtr_wrong"]),
        ("Conflicting evidence", ms["dtr_conflict"]), ("Explained by feeder-meter issue", ms["dtr_meter_issue"]),
        ("Unclear", ms["dtr_unclear"]), ("Not checkable (no usable outage data)", ms["dtr_not_checkable"]),
        ("Total DTRs", ms["dtr_total"])], columns=["DTR -> feeder result", "DTRs"])
    cons_tab = pd.DataFrame([
        ("Consumers with outage data", ms["consumers_with_events"]),
        ("Flagged as wrongly mapped - High confidence", ms["flagged_high"]),
        ("Flagged - Medium confidence", ms["flagged_medium"]),
        ("Flagged - Low confidence (one-sided evidence)", ms["flagged_low"]),
        ("Faulty meters excluded", ms["faulty_meters_excluded"]),
        ("Consumers not found in MI", ms["consumers_not_in_MI"])], columns=["Consumer mapping", "Count"])
    return [("1. Match / mismatch within feeder breakdown time - ALL breakdowns",
             bs[~bs["Check"].str.contains("confirmed")]),
            ("2. Same, CONFIRMED breakdowns only", bs[bs["Check"].str.contains("confirmed")].dropna(axis=1, how="all")),
            ("3. Is each DTR on the right feeder?", dtr_tab),
            ("4. Consumers that look wrongly mapped", cons_tab),
            ("5. Rules used for this run", rules)]


def summary_title_rows(blocks) -> list[int]:
    """1-based Excel row of each Summary block's title. Its header is the next row, data after that."""
    rows, r0 = [], 0
    for _, df in blocks:
        rows.append(r0 + 1)
        r0 += len(df) + SUMMARY_GAP
    return rows


def write_combined(job: dict, out: Path):
    from openpyxl import load_workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    from run_check import COLS_WRONG

    b, m = job["breakdown"], job["mapping"]
    wrong = m["wrong"][list(COLS_WRONG)].rename(columns=COLS_WRONG)
    blocks = combined_summary_blocks(job)
    titles = summary_title_rows(blocks)

    tmp = out.with_suffix(".tmp.xlsx")
    with pd.ExcelWriter(tmp, engine="openpyxl") as xw:
        for t, (title, df) in zip(titles, blocks):
            pd.DataFrame([[title]]).to_excel(xw, sheet_name="Summary", startrow=t - 1, index=False, header=False)
            df.to_excel(xw, sheet_name="Summary", startrow=t, index=False)
        for name, df in [("Data_Checks", job.get("checks", pd.DataFrame())), ("Feeder_Summary", b["feeders"]), ("Feeder_DTR_Counts", b["tree"]),
                         ("Breakdown_Status", b["status"]), ("DTR_Feeder_Check", m["dtr_check"]),
                         ("Wrong_Consumers", wrong), ("Mismatch_Patterns", m["patterns"]),
                         ("Suspect_Source_Meters", m["suspects"])]:
            (df if len(df) else pd.DataFrame({"(none)": []})).to_excel(xw, sheet_name=name, index=False)

    wb = load_workbook(tmp)
    hdr, f_hdr, f_norm = PatternFill("solid", fgColor="1F4E78"), Font(name="Arial", bold=True, color="FFFFFF"), Font(name="Arial", size=10)
    fills = {"MATCH": "C6EFCE", "MISMATCH": "FFC7CE", "PARTIAL": "FFEB9C", "SUSPECT BREAKDOWN": "FFC7CE",
             "Confirmed": "C6EFCE", "CORRECT": "C6EFCE", "CONFLICT": "FFEB9C", "High": "F8CBAD", "Medium": "FFE699",
             "OK": "C6EFCE", "WARNING": "FFEB9C", "ERROR": "FFC7CE"}
    fills = {k: PatternFill("solid", fgColor=v) for k, v in fills.items()}
    pct_names = {"Match %", "DTR match %", "Consumer match %", "DT->Consumer match %", "Own consumers that went off (share)"}
    for ws in wb.worksheets:
        header_rows = [1] if ws.title != "Summary" else [t + 1 for t in titles]
        pct_cols = set()
        for hr in header_rows:
            for c in ws[hr]:
                if c.value is not None:
                    c.font, c.fill = f_hdr, hdr
                    c.alignment = Alignment(wrap_text=True, vertical="center")
                    if c.value in pct_names:
                        pct_cols.add(c.column)
        for row in ws.iter_rows(min_row=2):
            for c in row:
                if c.row in header_rows:
                    continue
                c.font = f_norm
                if c.column in pct_cols and isinstance(c.value, (int, float)):
                    c.number_format = "0.0%"
                if isinstance(c.value, str) and c.value.strip() in fills:
                    c.fill = fills[c.value.strip()]
        for i, col in enumerate(ws.iter_cols(max_row=min(ws.max_row, 300)), 1):
            w = max((len(str(c.value)) for c in col if c.value is not None), default=8)
            ws.column_dimensions[get_column_letter(i)].width = min(max(10, w + 2), 50)
        if ws.title != "Summary":
            ws.freeze_panes = "A2"
            if ws.max_row > 1:
                ws.auto_filter.ref = ws.dimensions
    sm = wb["Summary"]
    for t in titles:
        sm.cell(t, 1).font = Font(name="Arial", bold=True, size=12, color="1F4E78")
    sm.column_dimensions["A"].width = 52
    ws = wb["Feeder_DTR_Counts"]
    for i, lv in enumerate(b["levels"], start=2):
        if lv == 0:
            for c in ws[i]:
                c.font = Font(name="Arial", size=10, bold=True)
                c.fill = PatternFill("solid", fgColor="DDEBF7")
        else:
            ws.row_dimensions[i].outline_level = 1
    ws.sheet_properties.outlinePr.summaryBelow = False
    wb.save(tmp)
    tmp.replace(out)
    return out


# ------------------------------------------------------------------ table renderer
def table(df: pd.DataFrame, pct_cols=(), max_rows=None, cls="", link_col=None, job_id=None) -> str:
    if df is None or len(df) == 0:
        return "<p class='muted'>(none)</p>"
    d = df.copy()
    # Strip any personal data columns if present
    for bad_col in ("Consumer Name", "Consumer Address", "Address", "Name"):
        if bad_col in d.columns:
            d = d.drop(columns=[bad_col])
    if max_rows:
        d = d.head(max_rows)
    for c in d.columns:
        if c not in pct_cols and pd.api.types.is_float_dtype(d[c]):
            v = d[c].dropna()
            if len(v) and (v == v.round()).all():
                d[c] = d[c].map(lambda x: "" if pd.isna(x) else f"{int(x):,}" if abs(x) >= 1000 else str(int(x)))
    for c in pct_cols:
        if c in d:
            d[c] = d[c].map(lambda v: "" if pd.isna(v) else f"{v:.1%}")
    d = d.astype(object).where(d.notna(), "")
    html = d.to_html(index=False, escape=True, classes=f"data-table {cls}", border=0)
    if link_col and job_id and link_col in d:
        for v in d[link_col].astype(str).unique():
            safe = str(escape(v))
            html = html.replace(f"<td>{safe}</td>",
                                f"<td><a href='{url_for('feeder', job_id=job_id, code=v)}' style='font-weight:700;'>{safe}</a></td>", 1)
    for word, c in (("OK", "ok"), ("WARNING", "warn"), ("ERROR", "bad"), ("MATCH", "ok"),
                    ("MISMATCH", "bad"), ("PARTIAL", "warn"), ("SUSPECT BREAKDOWN", "bad"),
                    ("Confirmed", "ok"), ("CORRECT", "ok"), ("CONFLICT", "warn"),
                    ("High", "bad"), ("Medium", "warn"), ("Low", "info")):
        html = html.replace(f"<td>{word}</td>", f"<td class='{c}'>{word}</td>")
    return html


def sample_ids(j: dict) -> list[tuple[str, str]]:
    """Quick-query examples taken from this run's own results (no IDs hard-coded in the code)."""
    out = []

    def first(df, col):
        if isinstance(df, pd.DataFrame) and col in df.columns:
            v = df[col].dropna().astype(str).str.strip()
            v = v[(v != "") & (v.str.lower() != "nan")]
            return v.iloc[0] if len(v) else None
        return None
    m, b = j.get("mapping", {}), j.get("breakdown", {})
    for label, df, col in (("Meter", m.get("wrong"), "meter_id"), ("Consumer", m.get("wrong"), "consumer_number"),
                           ("DTR", m.get("dtr_check"), "dt_code"), ("Feeder", b.get("feeders"), "Feeder code")):
        v = first(df, col)
        if v:
            out.append((label, v))
    return out


def scope_text(j):
    sc = j.get("scope", "upload")
    return {"upload": "uploaded files only", "all": "all stored events",
            "days": f"stored events, last {j.get('days')} days",
            "range": f"stored events {j.get('date_from') or '…'} to {j.get('date_to') or '…'}"}.get(sc, sc)


def my_job(job_id: str) -> dict:
    j = JOBS.get(job_id)
    if not j or j.get("ws") != str(cur_ws().root):
        abort(404)
    return j


# ------------------------------------------------------------------ routes
@app.route("/")
def index():
    ws = cur_ws()
    meta = json.loads(ws.mi_meta.read_text()) if ws.mi_meta.exists() and ws.mi_latest.exists() else None
    ov = stored_overview(ws)
    store_note = f"{len(ov)} day(s) stored, latest {ov['Day'].iat[0]}" if len(ov) else "nothing stored yet"
    return render_template("index.html", meta=meta, store_note=store_note, settings=SETTINGS,
                           is_public=PUBLIC, keep_days=KEEP_DAYS)


@app.route("/run", methods=["POST"])
def run():
    mi = request.files.get("mi")
    data = [f for f in request.files.getlist("data") if f and f.filename]
    scope = request.form.get("scope", "upload")
    has_mi = bool(mi and mi.filename)
    ws = cur_ws()
    ws.touch()
    cleanup_workspaces()
    is_ajax = (request.headers.get("X-Requested-With") == "XMLHttpRequest" or
               request.accept_mimetypes.best == "application/json")

    if not has_mi and not ws.mi_latest.exists():
        if is_ajax:
            return jsonify({"error": "No MI master file is stored yet; please upload one."}), 400
        return render_template("job_error.html", job_id="init",
                               job={"created": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "error": "No MI master file is stored yet. Please upload an MI workbook.",
                                    "log": []})

    if scope == "upload" and not data:
        if is_ajax:
            return jsonify({"error": "Please upload outage reports, or choose to analyse stored events."}), 400
        return render_template("job_error.html", job_id="init",
                               job={"created": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "error": "No outage files uploaded. Please upload reports or select stored scope.",
                                    "log": []})

    job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:12]
    folder = ws.base / job_id
    (folder / "mi").mkdir(parents=True)
    (folder / "data").mkdir()
    if has_mi:
        mi_name = Path(mi.filename).name
        mi.save(folder / "mi" / mi_name)
    else:
        mi_name = json.loads(ws.mi_meta.read_text())["name"] if ws.mi_meta.exists() else "stored MI"
    for f in data:
        f.save(folder / "data" / Path(f.filename).name)
    try:
        days = max(1, int(request.form.get("days", 7)))
    except ValueError:
        days = 7

    JOBS[job_id] = {"state": "running", "log": [], "settings": parse_settings(request.form), "mi_name": mi_name,
                    "mi_uploaded": has_mi, "scope": scope, "days": days,
                    "date_from": request.form.get("date_from") or None, "date_to": request.form.get("date_to") or None,
                    "created": time.strftime("%Y-%m-%d %H:%M:%S"), "files": [],
                    "folder": str(folder), "ws": str(ws.root)}
    save_job(job_id)
    threading.Thread(target=run_job, args=(job_id,), daemon=True).start()

    if is_ajax:
        return jsonify({"redirect": url_for("job", job_id=job_id)})
    return redirect(url_for("job", job_id=job_id))


@app.route("/job/<job_id>/status")
def status(job_id):
    j = my_job(job_id)
    return jsonify(state=j["state"], log=j["log"][-50:])


@app.route("/job/<job_id>")
def job(job_id):
    j = my_job(job_id)
    if j["state"] == "running":
        return render_template("job_progress.html", job_id=job_id, job=j, scope_text=scope_text(j))
    if j["state"] == "error":
        return render_template("job_error.html", job_id=job_id, job=j)

    s, b, m = j["settings"], j["breakdown"], j["mapping"]
    bs = b["summary"]
    main3 = bs[~bs["Check"].str.contains("confirmed")].copy()
    conf3 = bs[bs["Check"].str.contains("confirmed")][["Check", "Breakdown checks (rows)", "MATCH", "MISMATCH",
                                                        "No data (not counted)", "Match %"]].copy()
    st_counts = b["status"]["Breakdown status"].value_counts()
    ms = m["summary"]

    judged_dtrs = ms["dtr_correct"] + ms["dtr_wrong"] + ms["dtr_conflict"]
    cons_tab = pd.DataFrame([
        ("Consumers with outage data", ms["consumers_with_events"]),
        ("Flagged as wrongly mapped - High confidence", ms["flagged_high"]),
        ("Flagged - Medium confidence", ms["flagged_medium"]),
        ("Flagged - Low confidence (one-sided evidence)", ms["flagged_low"]),
        ("Faulty meters excluded", ms["faulty_meters_excluded"]),
        ("Consumers not found in MI", ms["consumers_not_in_MI"])], columns=["Consumer mapping", "Count"])

    # Extract exact breakdown headline numbers for KPI deck
    def get_cnt(df, check_name):
        sub = df[df["Check"].str.startswith(check_name)]
        if sub.empty:
            return 0, 0, None
        return int(sub["MATCH"].iat[0]), int(sub["MISMATCH"].iat[0]), sub["Match %"].iat[0]

    all_fd_dt_m, all_fd_dt_mm, all_fd_dt_pct = get_cnt(main3, "Feeder -> DTR")
    all_fd_c_m, all_fd_c_mm, all_fd_c_pct = get_cnt(main3, "Feeder -> Consumer")
    all_dt_c_m, all_dt_c_mm, all_dt_c_pct = get_cnt(main3, "DTR -> Consumer")

    conf_fd_dt_m, conf_fd_dt_mm, conf_fd_dt_pct = get_cnt(conf3, "Feeder -> DTR")
    conf_fd_c_m, conf_fd_c_mm, conf_fd_c_pct = get_cnt(conf3, "Feeder -> Consumer")
    conf_dt_c_m, conf_dt_c_mm, conf_dt_c_pct = get_cnt(conf3, "DTR -> Consumer")

    # Render HTML tables (ID cells are click-to-trace to their source rows; see /trace)
    table_html_main = table(main3, pct_cols=["Match %"])
    table_html_conf = table(conf3, pct_cols=["Match %"])
    table_html_feeders = table(b["feeders"],
                               pct_cols=["DTR match %", "Consumer match %", "DT->Consumer match %"],
                               link_col="Feeder code", job_id=job_id)
    table_html_dtr_check = table(m["dtr_check"].head(50), max_rows=50)
    table_html_cons_tab = table(cons_tab)
    table_html_patterns = table(m["patterns"], max_rows=15)
    table_html_checks = table(j.get("checks", pd.DataFrame()))
    table_html_status = table(b["status"], pct_cols=["Own consumers that went off (share)"])

    # SVG Chart preparation for top feeders
    chart_feeders = []
    chart_grid_y = []
    chart_height = 100
    if "feeders" in b and not b["feeders"].empty:
        fdf = b["feeders"].copy().sort_values("Consumers on feeder", ascending=False).head(10)
        max_c = max(fdf["Consumers on feeder"].max(), 1)
        bar_max_w = 780
        curr_y = 20
        row_h = 28
        for _, r in fdf.iterrows():
            fc = str(r["Feeder code"])
            fn = str(r["Feeder name"]) if pd.notna(r["Feeder name"]) else fc
            cm = int(r.get("Consumer MATCH", 0))
            cmm = int(r.get("Consumer MISMATCH", 0))
            cp = int(r.get("Consumer PARTIAL", 0))
            tot = max(cm + cmm + cp, 1)
            w_scale = (tot / max_c) * bar_max_w
            m_w = (cm / tot) * w_scale
            mm_w = (cmm / tot) * w_scale
            p_w = (cp / tot) * w_scale
            chart_feeders.append({
                "code": fc, "name": fn, "match": cm, "mismatch": cmm, "partial": cp,
                "m_w": round(m_w, 1), "mm_w": round(mm_w, 1), "p_w": round(p_w, 1),
                "y": curr_y
            })
            curr_y += row_h
        chart_height = curr_y + 15

    return render_template("job_results.html", job_id=job_id, job=j, scope_text=scope_text(j),
                           samples=sample_ids(j),
                           breakdown=b, mapping=m, judged_dtrs=judged_dtrs,
                           breakdown_status_counts=st_counts,
                           all_fd_dt_m=all_fd_dt_m, all_fd_dt_mm=all_fd_dt_mm,
                           all_fd_c_m=all_fd_c_m, all_fd_c_mm=all_fd_c_mm,
                           all_dt_c_m=all_dt_c_m, all_dt_c_mm=all_dt_c_mm,
                           conf_fd_dt_m=conf_fd_dt_m, conf_fd_dt_mm=conf_fd_dt_mm, conf_fd_dt_pct=conf_fd_dt_pct,
                           conf_fd_c_m=conf_fd_c_m, conf_fd_c_mm=conf_fd_c_mm, conf_fd_c_pct=conf_fd_c_pct,
                           conf_dt_c_m=conf_dt_c_m, conf_dt_c_mm=conf_dt_c_mm, conf_dt_c_pct=conf_dt_c_pct,
                           table_html_main=table_html_main, table_html_conf=table_html_conf,
                           table_html_feeders=table_html_feeders, table_html_dtr_check=table_html_dtr_check,
                           table_html_cons_tab=table_html_cons_tab, table_html_patterns=table_html_patterns,
                           table_html_checks=table_html_checks, table_html_status=table_html_status,
                           chart_feeders=chart_feeders, chart_height=chart_height, chart_grid_y=chart_grid_y)


@app.route("/job/<job_id>/feeder/<code>")
def feeder(job_id, code):
    j = my_job(job_id)
    if j.get("state") != "done":
        abort(404)
    t = j["breakdown"]["tree"]
    sub = t[t["Feeder code"].astype(str) == str(code)]
    if sub.empty:
        abort(404)
    top = sub[sub["Level"] == "FEEDER"].dropna(axis=1, how="all")
    dts = sub[sub["Level"] != "FEEDER"].drop(columns=["Level", "Feeder code", "Feeder name", "Breakdowns",
                                                     "Suspect breakdowns", "Counts based on", "DTRs on feeder",
                                                     "DTR MATCH", "DTR PARTIAL", "DTR MISMATCH", "DTR no meter",
                                                     "DTR match %"], errors="ignore")
    table_html_top = table(top.drop(columns=["Level"], errors="ignore"),
                           pct_cols=["DTR match %", "Consumer match %", "DT->Consumer match %"])
    table_html_dtrs = table(dts, pct_cols=["Consumer match %", "DT->Consumer match %"])
    return render_template("feeder_detail.html", job_id=job_id, code=code, top=top,
                           table_html_top=table_html_top, table_html_dtrs=table_html_dtrs)


@app.route("/job/<job_id>/download/<name>")
def download(job_id, name):
    j = my_job(job_id)
    if name not in ("All_Results.xlsx", "Breakdown_Mapping_Check.xlsx", "Mapping_Report.xlsx",
                    "Feeder_DTR_Consumer.xlsx"):
        abort(404)
    return send_from_directory(j["folder"], name, as_attachment=True)


# ------------------------------------------------------------------ trace an ID to its source row
def job_mi_path(j: dict) -> Path:
    """The MI workbook a run read: its own uploaded copy, else the workspace's stored MI."""
    if j.get("mi_uploaded"):
        return Path(j["folder"]) / "mi" / j["mi_name"]
    return WS(j["ws"]).mi_latest


def start_provenance(j: dict):
    provenance.build_async(j["folder"], job_mi_path(j), j.get("mi_name"))


def open_locally() -> bool:
    """Can the server open Excel for this viewer? Only when they sit at the server's own PC."""
    return (not PUBLIC and request.remote_addr in ("127.0.0.1", "::1") and excel_open.available())


@app.route("/job/<job_id>/trace")
def trace_source(job_id):
    """Where in the run's input files does ?q= (an ID or a breakdown label) come from?"""
    j = my_job(job_id)
    if j.get("state") != "done":
        abort(404)
    folder = Path(j["folder"])
    if not provenance.is_ready(folder):
        start_provenance(j)
        return jsonify({"state": "building", "status": provenance.status(folder) or "starting"})
    res = provenance.trace(folder, request.args.get("q", "")[:200])
    res.update(state="ready", local_excel=open_locally())
    return jsonify(res)


def _source_target(job_id):
    j = my_job(job_id)
    folder = Path(j["folder"])
    if not provenance.is_ready(folder):
        abort(409)
    try:
        src, row, col = (int(request.values.get(k, "")) for k in ("src", "row", "col"))
    except ValueError:
        abort(400)
    info = provenance.source(folder, src)
    if not info or not info["path"].exists() or not (1 <= row <= 1_048_576 and 1 <= col <= 16_384):
        abort(404)
    return info, src, row, col


@app.route("/job/<job_id>/source/open", methods=["POST"])
def source_open(job_id):
    """Open the original input file in this PC's Excel at the source row (local use), or tell
    the browser to download a copy that opens there (remote use)."""
    info, src, row, col = _source_target(job_id)
    dl = url_for("source_file", job_id=job_id, src=src, row=row, col=col)
    if not open_locally():
        if not info["path"].resolve().is_relative_to(Path(my_job(job_id)["folder"]).resolve()):
            return jsonify({"opened": False, "error": "This MI file is the workspace's stored copy, not a file "
                            "uploaded with this run, so it is not offered as a download. The row is listed above."})
        return jsonify({"opened": False, "download": dl})
    try:
        where = excel_open.open_at(info["path"], info["sheet"], row, col)
    except Exception as e:
        return jsonify({"opened": False, "download": dl, "error": str(e)[-300:]})
    return jsonify({"opened": True, "where": where[3:], "path": str(info["path"])})


@app.route("/job/<job_id>/source/file")
def source_file(job_id):
    """A copy of the input file; .xlsx copies are saved so Excel opens at the source cell."""
    from flask import Response
    from urllib.parse import quote
    from xlsx_goto import goto_cell, col_letter
    info, _, row, col = _source_target(job_id)
    path = info["path"]
    if not path.resolve().is_relative_to(Path(my_job(job_id)["folder"]).resolve()):
        abort(403)   # stored / seeded MI is not this run's upload: never offer it as a download
    if path.suffix.lower() != ".xlsx":
        return send_from_directory(path.parent, path.name, as_attachment=True, download_name=info["file"])
    data = goto_cell(path, info["sheet"], f"{col_letter(col)}{row}")
    return Response(data, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(path.name),
                             "Cache-Control": "no-store"})


@app.route("/job/<job_id>/raw_explorer")
def raw_explorer(job_id):
    j = my_job(job_id)
    ws = WS(j["ws"])
    folder = Path(j["folder"])
    ensure_checks_sqlite(folder)

    kind = request.args.get("kind", "consumer_events")
    search = request.args.get("search", "").strip()
    result_filter = request.args.get("result", "ALL").strip()
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    try:
        limit = max(10, min(100, int(request.args.get("limit", 50))))
    except ValueError:
        limit = 50
    offset = (page - 1) * limit

    rows, cols, total = [], [], 0

    if kind == "breakdown_checks":
        db_path = folder / "checks.sqlite"
        if db_path.exists():
            con = sqlite3.connect(db_path)
            try:
                where, params = [], []
                if result_filter and result_filter != "ALL":
                    where.append("Result = ?")
                    params.append(result_filter)
                if search:
                    where.append("(Meter_ID LIKE ? OR Consumer_No LIKE ? OR Feeder_code LIKE ? OR DT_code LIKE ? OR Breakdown LIKE ?)")
                    s = f"%{search}%"
                    params.extend([s, s, s, s, s])
                w_str = (" WHERE " + " AND ".join(where)) if where else ""
                cur = con.cursor()
                cur.execute(f"SELECT count(*) FROM feeder_consumer{w_str}", params)
                total = cur.fetchone()[0]

                cur.execute(f"SELECT Breakdown, Breakdown_status, Feeder_code, Feeder_name, DT_code, Consumer_No, Meter_ID, Feeder_off, Feeder_on, Consumer_off, Consumer_on, Off_diff_min, Result, Exact_timing_off_and_on_within_tol FROM feeder_consumer{w_str} LIMIT ? OFFSET ?", params + [limit, offset])
                raw_data = cur.fetchall()
                cols = ["Breakdown", "Status", "Feeder Code", "Feeder Name", "DT Code", "Consumer No", "Meter ID", "Feeder Off", "Feeder On", "Consumer Off", "Consumer On", "Diff (min)", "Result", "Exact Timing"]
                rows = [list(r) for r in raw_data]
            finally:
                con.close()
    else:
        tbl = TABLES.get(kind.replace("_events", ""))
        if tbl:
            con = db(ws)
            try:
                where, params = [], []
                if search:
                    if "consumer" in kind:
                        where.append("(meter_id LIKE ? OR consumer_number LIKE ?)")
                        s = f"%{search}%"
                        params.extend([s, s])
                    else:
                        where.append("meter_id LIKE ?")
                        params.append(f"%{search}%")
                w_str = (" WHERE " + " AND ".join(where)) if where else ""
                cur = con.cursor()
                cur.execute(f"SELECT count(*) FROM {tbl}{w_str}", params)
                total = cur.fetchone()[0]

                if "consumer" in kind:
                    cur.execute(f"SELECT meter_id, consumer_number, start, restore FROM {tbl}{w_str} ORDER BY start DESC LIMIT ? OFFSET ?", params + [limit, offset])
                    cols = ["Meter ID", "Consumer No", "Outage Start", "Outage Restore", "Duration (min)"]
                else:
                    cur.execute(f"SELECT meter_id, start, restore FROM {tbl}{w_str} ORDER BY start DESC LIMIT ? OFFSET ?", params + [limit, offset])
                    cols = [("Feeder" if "feeder" in kind else "DTR") + " Meter ID", "Outage Start", "Outage Restore", "Duration (min)"]

                raw_data = cur.fetchall()
                rows = []
                for r in raw_data:
                    r_list = list(r)
                    st, rest = r_list[-2], r_list[-1]
                    dur = ""
                    if st and rest:
                        try:
                            t1 = pd.to_datetime(st)
                            t2 = pd.to_datetime(rest)
                            dur = f"{round((t2 - t1).total_seconds() / 60, 1):,}"
                        except Exception:
                            pass
                    rows.append(r_list + [dur])
            finally:
                con.close()

    pages = max(1, (total + limit - 1) // limit)
    return jsonify({
        "kind": kind,
        "rows": rows,
        "columns": cols,
        "total": total,
        "page": page,
        "pages": pages,
        "limit": limit
    })


@app.route("/job/<job_id>/cross_check")
def cross_check(job_id):
    j = my_job(job_id)
    ws = WS(j["ws"])
    folder = Path(j["folder"])
    ensure_checks_sqlite(folder)

    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"found": False, "error": "Please enter a Consumer Number, Meter ID, DTR Code, or Feeder Code."})

    db_path = folder / "checks.sqlite"
    if not db_path.exists():
        return jsonify({"found": False, "error": "Breakdown checks database is compiling. Please try again in a moment."})

    con_chk = sqlite3.connect(db_path)
    con_ev = db(ws)
    try:
        cur_chk = con_chk.cursor()
        cur_ev = con_ev.cursor()

        # 1. Try finding as Consumer
        cur_chk.execute("""
            SELECT Breakdown, Breakdown_status, Feeder_code, Feeder_name, Feeder_off, Feeder_on, 
                   DT_code, Consumer_No, Meter_ID, Consumer_off, Consumer_on, Off_diff_min, 
                   Result, Exact_timing_off_and_on_within_tol 
            FROM feeder_consumer 
            WHERE Meter_ID = ? OR Consumer_No = ?
        """, (q, q))
        chk_rows = cur_chk.fetchall()

        if chk_rows:
            cols = [d[0] for d in cur_chk.description]
            chk_list = [dict(zip(cols, r)) for r in chk_rows]
            c0 = chk_list[0]
            meter_id = str(c0.get("Meter_ID") or q)
            consumer_no = str(c0.get("Consumer_No") or q)

            cur_ev.execute("SELECT start, restore FROM consumer_events WHERE meter_id = ? OR consumer_number = ? ORDER BY start", (meter_id, consumer_no))
            raw_events_tuples = cur_ev.fetchall()
            raw_events = []
            for s, r in raw_events_tuples:
                dur = ""
                if s and r:
                    try:
                        dur = f"{round((pd.to_datetime(r) - pd.to_datetime(s)).total_seconds() / 60, 1)} min"
                    except Exception:
                        pass
                raw_events.append({"start": s, "restore": r, "duration": dur})

            wrong = j.get("mapping", {}).get("wrong", pd.DataFrame())
            w_hit = wrong[(wrong["meter_id"] == meter_id) | (wrong["consumer_number"] == consumer_no)] if isinstance(wrong, pd.DataFrame) and not wrong.empty else pd.DataFrame()

            if not w_hit.empty:
                w_row = w_hit.iloc[0]
                conf = str(w_row.get("confidence", "Flagged"))
                sug_f = str(w_row.get("suggested_feeder", ""))
                sug_dt = str(w_row.get("suggested_dt", ""))
                verdict = f"Flagged as Wrong Mapping ({conf} Confidence) &rarr; Suggested Feeder: {sug_f}, Suggested DT: {sug_dt}"
                verdict_badge = "bad" if conf == "High" else "warn"
                reason = str(w_row.get("reason", ""))
            else:
                verdict = "Recorded Mapping Verified & Confirmed (Follows Recorded Source)"
                verdict_badge = "ok"
                reason = "Outage telemetry aligns with the recorded feeder and DTR network topology."

            n_tot = len(chk_list)
            n_m = sum(1 for c in chk_list if c.get("Result") == "MATCH")
            n_mm = sum(1 for c in chk_list if c.get("Result") == "MISMATCH")
            n_nd = n_tot - n_m - n_mm
            conf_list = [c for c in chk_list if c.get("Breakdown_status") == "Confirmed"]
            conf_m = sum(1 for c in conf_list if c.get("Result") == "MATCH")
            conf_tot = sum(1 for c in conf_list if c.get("Result") in ("MATCH", "MISMATCH"))
            conf_pct = f"{round((conf_m / conf_tot) * 100, 1)}%" if conf_tot > 0 else "N/A"

            return jsonify({
                "found": True,
                "type": "consumer",
                "identifier": f"Consumer {consumer_no} (Meter ID: {meter_id})",
                "recorded_feeder": f"{c0.get('Feeder_code')} &mdash; {c0.get('Feeder_name')}",
                "recorded_dt": str(c0.get("DT_code") or "N/A"),
                "verdict": verdict,
                "verdict_badge": verdict_badge,
                "reason": reason,
                "raw_events_count": len(raw_events),
                "raw_events": raw_events,
                "breakdown_checks": chk_list,
                "stats": {
                    "total_checks": n_tot,
                    "matched": n_m,
                    "mismatched": n_mm,
                    "no_data": n_nd,
                    "confirmed_match_pct": conf_pct
                }
            })

        # 2. Try finding as DTR
        cur_chk.execute("""
            SELECT Breakdown, Breakdown_status, Feeder_code, Feeder_name, Feeder_off, Feeder_on, 
                   DT_code, DT_name, DTR_off, DTR_on, Off_diff_min, 
                   Result, Exact_timing_off_and_on_within_tol 
            FROM feeder_dtr 
            WHERE DT_code = ?
        """, (q,))
        dtr_rows = cur_chk.fetchall()

        if dtr_rows:
            cols = [d[0] for d in cur_chk.description]
            dtr_list = [dict(zip(cols, r)) for r in dtr_rows]
            d0 = dtr_list[0]
            dt_code = str(d0.get("DT_code"))
            dt_name = str(d0.get("DT_name") or "")

            dtr_check = j.get("mapping", {}).get("dtr_check", pd.DataFrame())
            d_hit = dtr_check[dtr_check["dt_code"] == dt_code] if isinstance(dtr_check, pd.DataFrame) and not dtr_check.empty else pd.DataFrame()
            if not d_hit.empty:
                dr = d_hit.iloc[0]
                vrd = str(dr.get("verdict", "UNCLEAR"))
                sug = str(dr.get("suggested_feeder") or "")
                verdict = f"DTR Verdict: {vrd}" + (f" (Suggested Feeder: {sug})" if sug and sug != "None" else "")
                verdict_badge = "ok" if "CORRECT" in vrd else "bad" if "WRONG" in vrd else "warn"
                reason = str(dr.get("consumer_detail") or dr.get("meter_detail") or "")
            else:
                verdict = "DTR Evaluated"
                verdict_badge = "info"
                reason = ""

            cur_ev.execute("SELECT start, restore FROM dtr_events WHERE meter_id = ? ORDER BY start", (dt_code,))
            raw_events_tuples = cur_ev.fetchall()
            raw_events = []
            for s, r in raw_events_tuples:
                dur = ""
                if s and r:
                    try:
                        dur = f"{round((pd.to_datetime(r) - pd.to_datetime(s)).total_seconds() / 60, 1)} min"
                    except Exception:
                        pass
                raw_events.append({"start": s, "restore": r, "duration": dur})

            n_tot = len(dtr_list)
            n_m = sum(1 for c in dtr_list if c.get("Result") == "MATCH")
            n_mm = sum(1 for c in dtr_list if c.get("Result") == "MISMATCH")
            n_nd = n_tot - n_m - n_mm
            conf_list = [c for c in dtr_list if c.get("Breakdown_status") == "Confirmed"]
            conf_m = sum(1 for c in conf_list if c.get("Result") == "MATCH")
            conf_tot = sum(1 for c in conf_list if c.get("Result") in ("MATCH", "MISMATCH"))
            conf_pct = f"{round((conf_m / conf_tot) * 100, 1)}%" if conf_tot > 0 else "N/A"

            return jsonify({
                "found": True,
                "type": "dtr",
                "identifier": f"Distribution Transformer (DTR): {dt_code} ({dt_name})",
                "recorded_feeder": f"{d0.get('Feeder_code')} &mdash; {d0.get('Feeder_name')}",
                "recorded_dt": f"{dt_code} ({dt_name})",
                "verdict": verdict,
                "verdict_badge": verdict_badge,
                "reason": reason,
                "raw_events_count": len(raw_events),
                "raw_events": raw_events,
                "breakdown_checks": dtr_list,
                "stats": {
                    "total_checks": n_tot,
                    "matched": n_m,
                    "mismatched": n_mm,
                    "no_data": n_nd,
                    "confirmed_match_pct": conf_pct
                }
            })

        # 3. Try finding as Feeder
        cur_chk.execute("""
            SELECT DISTINCT Breakdown, Breakdown_status, Feeder_code, Feeder_name, Feeder_off, Feeder_on, Breakdown_min 
            FROM feeder_dtr 
            WHERE Feeder_code = ?
        """, (q,))
        f_bds = cur_chk.fetchall()

        if f_bds:
            f0 = f_bds[0]
            f_code = str(f0[2])
            f_name = str(f0[3])
            bd_list = []
            for b in f_bds:
                bd_list.append({
                    "Breakdown": b[0],
                    "Breakdown_status": b[1],
                    "Feeder_code": b[2],
                    "Feeder_name": b[3],
                    "Feeder_off": b[4],
                    "Feeder_on": b[5],
                    "Off_diff_min": "0",
                    "Result": "BREAKDOWN WINDOW",
                    "Exact_timing_off_and_on_within_tol": f"{b[6]} min"
                })

            cur_ev.execute("SELECT start, restore FROM feeder_events WHERE meter_id = ? ORDER BY start", (f_code,))
            raw_events_tuples = cur_ev.fetchall()
            raw_events = [{"start": s, "restore": r, "duration": f"{round((pd.to_datetime(r) - pd.to_datetime(s)).total_seconds() / 60, 1)} min" if s and r else ""} for s, r in raw_events_tuples]

            return jsonify({
                "found": True,
                "type": "feeder",
                "identifier": f"Feeder: {f_code} ({f_name})",
                "recorded_feeder": f"{f_code} &mdash; {f_name}",
                "recorded_dt": "Feeder Level Entity",
                "verdict": f"Feeder with {len(f_bds)} Evaluated Breakdowns",
                "verdict_badge": "info",
                "reason": f"{len(f_bds)} feeder-level breakdown intervals analyzed across DTRs and consumers.",
                "raw_events_count": len(raw_events),
                "raw_events": raw_events,
                "breakdown_checks": bd_list,
                "stats": {
                    "total_checks": len(f_bds),
                    "matched": sum(1 for b in f_bds if b[1] == "Confirmed"),
                    "mismatched": sum(1 for b in f_bds if b[1] == "SUSPECT BREAKDOWN"),
                    "no_data": 0,
                    "confirmed_match_pct": f"{len(f_bds)} Breakdowns"
                }
            })

        return jsonify({
            "found": False,
            "error": f"No records found for identifier '{q}'. Please check the Consumer Number, Meter ID, or DTR Code."
        })
    finally:
        con_chk.close()
        con_ev.close()


@app.route("/history")
def history():
    runs = []
    me = str(cur_ws().root)
    for jid, j in sorted(((k, v) for k, v in JOBS.items() if v.get("ws") == me),
                         key=lambda x: x[1]["created"], reverse=True):
        period, bd, pct = "", None, ""
        if j.get("state") == "done":
            ch = j.get("checks", pd.DataFrame())
            if isinstance(ch, pd.DataFrame) and not ch.empty:
                per = ch[(ch["Area"] == "consumer outages") & (ch["Check"] == "Period covered")]["Value"]
                period = per.iat[0] if len(per) else ""
            if "breakdown" in j:
                bd = j["breakdown"].get("n")
                fc = j["breakdown"].get("summary", pd.DataFrame())
                if isinstance(fc, pd.DataFrame) and not fc.empty:
                    v = fc.loc[fc["Check"] == "Feeder -> Consumer", "Match %"]
                    pct = f"{v.iat[0]:.1%}" if len(v) and pd.notna(v.iat[0]) else ""
        cls = {"done": "ok", "error": "bad", "running": "warn"}.get(j.get("state"), "")
        runs.append({
            "id": jid, "created": j.get("created"), "state": j.get("state"), "status_cls": cls,
            "scope": scope_text(j), "period": period, "breakdowns": bd, "match_pct": pct
        })
    return render_template("history.html", runs=runs)


@app.route("/job/<job_id>/delete", methods=["POST"])
def delete_job(job_id):
    j = my_job(job_id)
    if j.get("state") != "running":
        JOBS.pop(job_id, None)
        shutil.rmtree(j["folder"], ignore_errors=True)
    return redirect(url_for("history"))


@app.route("/stored", methods=["GET", "POST"])
def stored():
    msg = ""
    ws = cur_ws()
    if request.method == "POST":
        day = request.form.get("day", "")
        con = db(ws)
        try:
            if request.form.get("action") == "clear_all":
                for t in TABLES.values():
                    con.execute(f"DELETE FROM {t}")
                msg = "All stored outage events have been cleared."
            elif day:
                for t in TABLES.values():
                    con.execute(f"DELETE FROM {t} WHERE date(start) = ?", (day,))
                msg = f"Outage events for {escape(day)} were deleted."
            con.commit()
        finally:
            con.close()
    ov = stored_overview(ws)
    meta = json.loads(ws.mi_meta.read_text()) if ws.mi_meta.exists() else None
    daily_rows = []
    total_consumer = int(ov["Consumer events"].sum()) if len(ov) else 0
    total_dtr = int(ov["DTR events"].sum()) if len(ov) else 0
    total_feeder = int(ov["Feeder events"].sum()) if len(ov) else 0
    for r in ov.itertuples(index=False, name=None):
        daily_rows.append({"day": r[0], "consumer": int(r[1]), "dtr": int(r[2]), "feeder": int(r[3])})
    return render_template("stored.html", days_count=len(ov), total_consumer=total_consumer,
                           total_dtr=total_dtr, total_feeder=total_feeder, daily_rows=daily_rows,
                           meta=meta, msg=msg)


@app.route("/method")
def method():
    return render_template("method.html", keep_days=KEEP_DAYS)


load_saved_jobs()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"),
                    help="127.0.0.1 = local machine; 0.0.0.0 = reachable from network")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5000)))
    a = ap.parse_args()
    if a.host != "127.0.0.1":
        print("NOTE: Anyone reachable on this network can access the web application.")
    url = f"http://127.0.0.1:{a.port}"
    try:
        from waitress import serve
        print(f"Starting production server on {a.host}:{a.port} with waitress (8 threads)...")
        print(f"Open: {url}")
        serve(app, host=a.host, port=a.port, threads=8, max_request_body_size=1024 ** 3)
    except ImportError:
        print(f"Starting Flask development server on {a.host}:{a.port}...")
        print(f"Open: {url}")
        app.run(host=a.host, port=a.port, debug=False, threaded=True)
