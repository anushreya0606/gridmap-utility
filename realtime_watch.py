"""
realtime_watch.py  -  keep the mapping report up to date as new outage reports arrive.

How it works
  1. Drop new consumer / DTR / feeder outage reports (.csv or .xlsx) into  incoming/
  2. Every --interval seconds the watcher picks up new files, stores their events in
     store/events.sqlite (duplicates ignored), and moves the file to processed/
  3. If anything new arrived, it re-runs the full check on all stored events and
     rewrites the Excel report. A Flag_History sheet records when each consumer was
     first flagged and how many runs in a row it has stayed flagged, so one noisy
     update does not flip the answer.

Run (Windows PowerShell or Linux):
    python realtime_watch.py --mi Genus_Consumer_MI_Data.xlsx --interval 300
    python realtime_watch.py --mi Genus_Consumer_MI_Data.xlsx --once      # process what's there, then exit
"""
from __future__ import annotations
import argparse
import shutil
import sqlite3
import time
from pathlib import Path
import pandas as pd

import topology_check as tc
from run_check import write_report
import build_hierarchy
import breakdown_check

TABLES = {"consumer": "consumer_events", "dtr": "dtr_events", "feeder": "feeder_events"}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def init_db(db: Path):
    con = sqlite3.connect(db)
    for kind, t in TABLES.items():
        extra = ", consumer_number TEXT" if kind == "consumer" else ""
        con.execute(f"CREATE TABLE IF NOT EXISTS {t} (meter_id TEXT, start TEXT, restore TEXT{extra}, "
                    f"source_file TEXT, PRIMARY KEY (meter_id, start))")
    con.execute("CREATE TABLE IF NOT EXISTS flag_history (consumer_number TEXT PRIMARY KEY, first_flagged TEXT, "
                "last_flagged TEXT, runs_flagged INTEGER, consecutive_runs INTEGER, last_confidence TEXT, "
                "last_suggested_dt TEXT, last_suggested_feeder TEXT, still_flagged INTEGER)")
    con.commit()
    return con


def file_is_stable(p: Path, wait=2.0) -> bool:
    """Skip files that are still being copied/written."""
    try:
        s1 = p.stat().st_size
        time.sleep(wait)
        return s1 == p.stat().st_size and s1 > 0
    except OSError:
        return False


def ingest(con, incoming: Path, processed: Path) -> int:
    new_rows = 0
    for p in sorted(incoming.iterdir()):
        if p.suffix.lower() not in (".csv", ".xlsx", ".xls") or p.name.startswith("~$"):
            continue
        if not file_is_stable(p):
            continue
        try:
            df = tc.read_any(p)
            kind = tc.classify_report(df)
            if kind is None:
                log(f"unknown report type, left in place: {p.name}")
                continue
            ev = tc.normalise_events(df, kind)
        except Exception as e:
            log(f"could not read {p.name}: {e}")
            continue
        ev = ev.assign(start=ev["start"].dt.strftime("%Y-%m-%d %H:%M:%S"),
                       restore=ev["restore"].dt.strftime("%Y-%m-%d %H:%M:%S"), source_file=p.name)
        cols = ["meter_id", "start", "restore"] + (["consumer_number"] if kind == "consumer" else []) + ["source_file"]
        before = con.total_changes
        con.executemany(f"INSERT OR IGNORE INTO {TABLES[kind]} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        ev[cols].where(ev[cols].notna(), None).itertuples(index=False, name=None))
        con.commit()
        added = con.total_changes - before
        new_rows += added
        log(f"{kind:8s} {p.name}: {added:,} new events")
        processed.mkdir(exist_ok=True)
        shutil.move(str(p), processed / p.name)
    return new_rows


def load_events(con, kind: str, days: int | None):
    q = f"SELECT * FROM {TABLES[kind]}"
    if days:
        q += f" WHERE start >= datetime((SELECT MAX(start) FROM {TABLES[kind]}), '-{int(days)} days')"
    df = pd.read_sql(q, con)
    df["kind"] = kind
    for c in ("start", "restore"):
        df[c] = pd.to_datetime(df[c], errors="coerce").astype("datetime64[ns]")
    if kind != "consumer":
        df["consumer_number"] = None
    return df


def update_history(con, wrong: pd.DataFrame, now: str) -> pd.DataFrame:
    hist = pd.read_sql("SELECT * FROM flag_history", con).set_index("consumer_number")
    flagged = set(wrong["consumer_number"])
    w = wrong.set_index("consumer_number")
    for c in flagged:
        if c in hist.index:
            h = hist.loc[c]
            hist.loc[c, ["last_flagged", "runs_flagged", "consecutive_runs"]] = [
                now, int(h["runs_flagged"]) + 1, int(h["consecutive_runs"]) + 1 if h["still_flagged"] else 1]
        else:
            hist.loc[c, ["first_flagged", "last_flagged", "runs_flagged", "consecutive_runs"]] = [now, now, 1, 1]
        hist.loc[c, ["last_confidence", "last_suggested_dt", "last_suggested_feeder"]] = [
            w.at[c, "confidence"], w.at[c, "suggested_dt"], w.at[c, "suggested_feeder"]]
    hist["still_flagged"] = hist.index.isin(flagged).astype(int)
    hist.loc[~hist.index.isin(flagged), "consecutive_runs"] = 0
    out = hist.reset_index()
    con.execute("DELETE FROM flag_history")
    out.to_sql("flag_history", con, if_exists="append", index=False)
    con.commit()
    return out.sort_values(["still_flagged", "consecutive_runs"], ascending=False)


def run_once(con, mi, report: Path, days, st: tc.Settings, tree: Path | None = None):
    cm, dm, fm = mi
    ev = {k: load_events(con, k, days) for k in TABLES}
    if ev["consumer"].empty:
        log("no consumer events yet")
        return
    r = tc.analyse(ev["consumer"], ev["dtr"], ev["feeder"], cm, dm, fm, st, log=log)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    hist = update_history(con, r["wrong"], now)
    try:
        write_report(r, report, cm, history=hist, run_label=now)
        log(f"report updated: {report}  ({r['summary']['flagged_consumers']:,} flagged)")
    except PermissionError:   # Windows: report is open in Excel
        alt = report.with_name(f"{report.stem}_{time.strftime('%Y%m%d_%H%M%S')}.xlsx")
        write_report(r, alt, cm, history=hist, run_label=now)
        log(f"{report.name} is open (locked); wrote {alt.name} instead")
    bd = report.with_name("Breakdown_Mapping_Check.xlsx")
    try:
        breakdown_check.write(breakdown_check.check(ev, cm, dm, fm, st.window_min), bd)
        log(f"breakdown match/mismatch check updated: {bd}")
    except PermissionError:
        alt = bd.with_name(f"{bd.stem}_{time.strftime('%Y%m%d_%H%M%S')}.xlsx")
        breakdown_check.write(breakdown_check.check(ev, cm, dm, fm, st.window_min), alt)
        log(f"{bd.name} is open (locked); wrote {alt.name} instead")
    if tree is not None:
        try:
            build_hierarchy.build(r, ev, cm, dm, fm, tree, st.window_min)
            log(f"feeder->DTR->consumer view updated: {tree}")
        except PermissionError:
            alt = tree.with_name(f"{tree.stem}_{time.strftime('%Y%m%d_%H%M%S')}.xlsx")
            build_hierarchy.build(r, ev, cm, dm, fm, alt, st.window_min)
            log(f"{tree.name} is open (locked); wrote {alt.name} instead")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mi", required=True, help="Genus_Consumer_MI_Data.xlsx")
    ap.add_argument("--base", default=".", help="folder holding incoming/, processed/, store/")
    ap.add_argument("--report", default="Mapping_Report.xlsx")
    ap.add_argument("--interval", type=int, default=300, help="seconds between checks")
    ap.add_argument("--days", type=int, default=None, help="only use the last N days of events (default: all)")
    ap.add_argument("--window", type=int, default=2)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--tree", default="Feeder_DTR_Consumer.xlsx",
                    help="also write the Feeder->DTR->Consumer->time workbook ('' to skip)")
    a = ap.parse_args()

    base = Path(a.base)
    incoming, processed, store = base / "incoming", base / "processed", base / "store"
    for d in (incoming, processed, store):
        d.mkdir(parents=True, exist_ok=True)
    con = init_db(store / "events.sqlite")
    st = tc.Settings(window_min=a.window)
    report = base / a.report
    tree = base / a.tree if a.tree else None

    mi_path = Path(a.mi)
    mi_mtime = mi_path.stat().st_mtime
    log("loading MI master ...")
    mi = tc.load_master(mi_path)
    log(f"watching {incoming.resolve()}  (Ctrl+C to stop)")
    while True:
        if mi_path.stat().st_mtime != mi_mtime:           # MI file was updated -> reload and re-check
            mi_mtime = mi_path.stat().st_mtime
            mi = tc.load_master(mi_path)
            log("MI master reloaded")
            run_once(con, mi, report, a.days, st, tree)
        if ingest(con, incoming, processed) > 0:
            run_once(con, mi, report, a.days, st, tree)
        if a.once:
            break
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
