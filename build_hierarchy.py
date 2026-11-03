"""
build_hierarchy.py  -  Excel in the form  Feeder -> DTRs -> Consumers -> time

Two sheets, each grouped (click the 1 / 2 / 3 buttons or +/- at the left in Excel to collapse/expand):

  Feeder_Outage_View   For every feeder outage:  feeder (off/on time)
                         -> each DTR on that feeder (its off/on time)
                           -> each consumer on that DTR (its off/on time, or DID NOT GO OFF)
                       Uses the RECORDED mapping, so the odd consumer out is visible in red.

  Mapping_Tree         The CORRECTED mapping: Feeder -> DTR -> Consumer, with each consumer's
                       outage times, and whether the mapping is recorded, corrected, or needs a field check.

    python build_hierarchy.py --mi "Genus Consumer MI Data.xlsx" --data data --out Feeder_DTR_Consumer.xlsx
"""
from __future__ import annotations
import argparse
from pathlib import Path
import numpy as np
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment

import topology_check as tc
from run_check import load_reports

FILL = {"FEEDER": PatternFill("solid", fgColor="1F4E78"), "DTR": PatternFill("solid", fgColor="DDEBF7"),
        "bad": PatternFill("solid", fgColor="F8CBAD"), "warn": PatternFill("solid", fgColor="FFE699"),
        "good": PatternFill("solid", fgColor="E2EFDA")}
T = lambda x: "" if pd.isna(x) else pd.Timestamp(x).strftime("%d-%m-%Y %H:%M")


def minutes(a, b):
    return "" if pd.isna(a) or pd.isna(b) else int((b - a).total_seconds() // 60)


def corrected_mapping(res: pd.DataFrame, cm: pd.DataFrame) -> pd.DataFrame:
    """Final feeder/DT per consumer. High/Medium flags are applied; Low ones only noted."""
    m = res[["consumer_number", "meter_id", "rec_feeder", "rec_dt", "suggested_feeder", "suggested_dt",
             "dtr_status", "feeder_status", "dtr_confidence", "feeder_confidence", "reason"]].copy()
    flagged = (m["dtr_status"].astype(str).str.startswith(("WRONG", "LIKELY")) |
               m["feeder_status"].astype(str).str.startswith(("WRONG", "LIKELY")))
    conf = m[["dtr_confidence", "feeder_confidence"]].apply(
        lambda r: "High" if "High" in r.values else "Medium" if "Medium" in r.values else
        "Low" if "Low" in r.values else None, axis=1)
    m["feeder"], m["dt"], m["mapping"] = m["rec_feeder"], m["rec_dt"], "Recorded mapping OK"
    m.loc[m["dtr_status"] == "NOT EVALUABLE", "mapping"] = "Recorded (not enough outages to check)"
    m.loc[m["dtr_status"].astype(str).str.startswith("GROUP") |
          m["feeder_status"].astype(str).str.startswith("GROUP"), "mapping"] = "Recorded (source meter suspect)"
    apply = flagged & conf.isin(["High", "Medium"])
    amb = m["suggested_dt"].astype(str).str.startswith("AMBIGUOUS")
    m.loc[apply, "feeder"] = m.loc[apply, "suggested_feeder"]
    m.loc[apply & ~amb, "dt"] = m.loc[apply & ~amb, "suggested_dt"]
    m.loc[apply & amb, "dt"] = "DT UNKNOWN - field check"
    m.loc[apply & ~amb, "mapping"] = "CORRECTED (" + conf[apply & ~amb] + ")"
    m.loc[apply & amb, "mapping"] = "FEEDER CORRECTED, DT needs field check (" + conf[apply & amb] + ")"
    low = flagged & (conf == "Low")
    m.loc[low, "mapping"] = "Recorded (weak sign of error - check)"
    return m


F_HDR = Font(name="Arial", bold=True, color="FFFFFF")
F_FEEDER = Font(name="Arial", size=10, bold=True, color="FFFFFF")
F_BOLD = Font(name="Arial", size=10, bold=True)
F_NORM = Font(name="Arial", size=10)


def write_rows(ws, header, rows, widths):
    ws.append(header)
    hfill = PatternFill("solid", fgColor="404040")
    for c in ws[1]:
        c.font, c.fill = F_HDR, hfill
        c.alignment = Alignment(wrap_text=True, vertical="center")
    r = 1
    for vals, level, style in rows:
        ws.append(vals)
        r += 1
        if level:
            ws.row_dimensions[r].outline_level = level
        font = F_FEEDER if style == "FEEDER" else F_BOLD if level == 0 else F_NORM
        fill = FILL.get(style)
        whole = style in ("FEEDER", "DTR", "warn")
        for col in range(1, len(vals) + 1):
            c = ws.cell(r, col)
            c.font = font
            if fill is not None and (whole or col in (1, 5, 7, 10)):
                c.fill = fill
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = w
    ws.freeze_panes = "A2"
    ws.sheet_properties.outlinePr.summaryBelow = False   # +/- button sits on the parent row


def build(r: dict, ev: dict, cm, dm, fm, out, W: int = 2):
    """Write the Feeder -> DTR -> Consumer -> time workbook from an analyse() result."""
    res = r["results"]
    faulty = set(r["faulty"]["consumer_number"])

    fd_name = cm.groupby("rec_feeder")["rec_feeder_name"].first().combine_first(
        fm.drop_duplicates("feeder_code").set_index("feeder_code")["feeder_name"])
    dt_name = cm.groupby("rec_dt")["rec_dt_name"].first().combine_first(
        dm.drop_duplicates("dt_code").set_index("dt_code")["dt_name"])

    cev = ev["consumer"][~ev["consumer"]["consumer_number"].isin(faulty)].reset_index(drop=True)
    cev = cev.assign(cid=cev["consumer_number"], cev=range(len(cev)))
    fev = ev["feeder"].merge(fm[["meter_id", "feeder_code"]], on="meter_id").sort_values("start").reset_index(drop=True)
    fev = fev.assign(sid=fev["feeder_code"], sev=range(len(fev)))
    dev = ev["dtr"].merge(dm[["meter_id", "dt_code"]], on="meter_id").reset_index(drop=True)
    dev = dev.assign(cid=dev["dt_code"], cev=range(len(dev)))

    # which consumer / DTR outage matches each feeder outage (start and restore within W minutes)
    cp = tc.match_events(cev, fev, W).merge(cev[["cev", "start", "restore"]], on="cev")
    cp = cp.sort_values("start").drop_duplicates(["sev", "cid"])
    dp = tc.match_events(dev, fev, W).merge(dev[["cev", "start", "restore"]], on="cev")
    dp = dp.sort_values("start").drop_duplicates(["sev", "cid"])
    c_hit = {(s, c): (st, rs) for s, c, st, rs in cp[["sev", "cid", "start", "restore"]].itertuples(index=False)}
    d_hit = {(s, d): (st, rs) for s, d, st, rs in dp[["sev", "cid", "start", "restore"]].itertuples(index=False)}

    reporting = set(cev["consumer_number"])
    rec = cm[cm["consumer_number"].isin(reporting)]
    flag = res.set_index("consumer_number")
    wrong = r["wrong"].set_index("consumer_number")
    metered_dt = set(dev["dt_code"])

    # ---------------- sheet 1: per feeder outage, recorded mapping
    rows = []
    for _, e in fev.iterrows():
        fc, sev = e["feeder_code"], e["sev"]
        cons = rec[rec["rec_feeder"] == fc]
        if cons.empty:
            continue
        off_n = sum((sev, c) in c_hit for c in cons["consumer_number"])
        rows.append(([f"FEEDER {fc}", fd_name.get(fc, ""), "", "", "", "", T(e["start"]), T(e["restore"]),
                      minutes(e["start"], e["restore"]),
                      f"{off_n} of {len(cons)} reporting consumers went off", ""], 0, "FEEDER"))
        for dt, g in cons.groupby("rec_dt", sort=True):
            n_off = sum((sev, c) in c_hit for c in g["consumer_number"])
            if dt in metered_dt:
                dh = d_hit.get((sev, dt))
                d_off, d_on = dh if dh else (None, None)
                d_note = "DTR meter went off with feeder" if dh else "DTR meter did NOT go off"
            else:
                d_off = d_on = None
                d_note = "no DTR meter"
            style = "DTR" if n_off >= len(g) / 2 else "warn"
            rows.append((["  DTR", "", dt, dt_name.get(dt, ""), "", "", T(d_off), T(d_on), minutes(d_off, d_on),
                          f"{n_off} of {len(g)} consumers went off", d_note], 1, style))
            for _, c in g.sort_values("consumer_number").iterrows():
                cn = c["consumer_number"]
                h = c_hit.get((sev, cn))
                note = ""
                if cn in wrong.index:
                    note = f"FLAGGED ({wrong.at[cn, 'confidence']}): suggested feeder {wrong.at[cn, 'suggested_feeder']}, DT {wrong.at[cn, 'suggested_dt']}"
                if h:
                    rows.append((["    Consumer", "", "", "", cn, c["meter_id"], T(h[0]), T(h[1]), minutes(*h),
                                  "Went off with feeder", note], 2, "good"))
                else:
                    rows.append((["    Consumer", "", "", "", cn, c["meter_id"], "", "", "",
                                  "DID NOT GO OFF", note], 2, "bad"))

    wb = Workbook()
    ws = wb.active
    ws.title = "Feeder_Outage_View"
    write_rows(ws, ["Level", "Feeder name", "DT code", "DT name", "Consumer No", "Meter ID",
                    "Off at", "On at", "Duration (min)", "Status", "Note (from mapping check)"],
               rows, [16, 26, 16, 30, 15, 11, 17, 17, 10, 34, 70])

    # ---------------- sheet 2: corrected mapping tree with each consumer's outage times
    cm2 = corrected_mapping(res, cm)
    cev_s = cev.sort_values("start")
    times = cev_s.groupby("consumer_number").apply(
        lambda g: "; ".join(f"{s:%H:%M}-{'' if pd.isna(x) else format(x, '%H:%M')}" for s, x in zip(g["start"], g["restore"])),
        include_groups=False)
    cm2["times"] = cm2["consumer_number"].map(times)
    cm2["n"] = cm2["consumer_number"].map(cev.groupby("consumer_number").size())
    cm2["date"] = cm2["consumer_number"].map(cev.groupby("consumer_number")["start"].min().dt.strftime("%d-%m-%Y"))

    # add every MI consumer of the same circle(s) that had no outage data, under its RECORDED feeder/DT
    circ_col = "Circle Name" if "Circle Name" in cm.columns else None
    if circ_col:
        rep_circ = cm.loc[cm["consumer_number"].isin(reporting), circ_col].value_counts(normalize=True)
        circles = rep_circ[rep_circ >= 0.05].index
        rest = cm[cm[circ_col].isin(circles) & ~cm["consumer_number"].isin(cm2["consumer_number"])]
        if len(rest):
            extra = pd.DataFrame({
                "consumer_number": rest["consumer_number"].values, "meter_id": rest["meter_id"].values,
                "rec_feeder": rest["rec_feeder"].values, "rec_dt": rest["rec_dt"].values,
                "feeder": rest["rec_feeder"].fillna("NO FEEDER CODE IN MI").values,
                "dt": rest["rec_dt"].fillna("NO DT CODE IN MI").values,
                "mapping": np.where(rest["consumer_number"].isin(faulty), "Recorded (faulty meter - not checked)",
                                    "Recorded (no outage data - not checked)")})
            cm2 = pd.concat([cm2, extra], ignore_index=True)
    cm2["feeder"] = cm2["feeder"].fillna("NO FEEDER CODE IN MI")
    cm2["dt"] = cm2["dt"].fillna("NO DT CODE IN MI")
    checked = ~cm2["mapping"].str.contains("not checked|not enough outages", regex=True)
    rows = []
    for fc, gf in cm2.groupby("feeder", sort=True):
        n_chk = int(checked[gf.index].sum())
        rows.append(([f"FEEDER {fc}", fd_name.get(fc, ""), "", "", "", "",
                      f"{gf['dt'].nunique()} DTRs, {len(gf)} consumers ({n_chk} checked)", "", "", "", ""], 0, "FEEDER"))
        for dt, gd in gf.groupby("dt", sort=True):
            n_corr = gd["mapping"].str.startswith(("CORRECTED", "FEEDER CORRECTED")).sum()
            rows.append((["  DTR", "", dt, dt_name.get(dt, ""), "", "",
                          f"{len(gd)} consumers ({int(checked[gd.index].sum())} checked)" + (f", {n_corr} moved here" if n_corr else ""),
                          "", "", "", ""], 1, "DTR"))
            for _, c in gd.sort_values("consumer_number").iterrows():
                moved = c["mapping"].startswith(("CORRECTED", "FEEDER CORRECTED"))
                weak = c["mapping"].startswith("Recorded (weak") or "suspect" in c["mapping"]
                was = (f"{c['rec_feeder']} / {c['rec_dt']}" if moved else "")
                if "not checked" in c["mapping"]:
                    weak = False
                rows.append((["    Consumer", "", "", "", c["consumer_number"], c["meter_id"], c["mapping"], was,
                              c["date"] if pd.notna(c["date"]) else "", int(c["n"]) if pd.notna(c["n"]) else "",
                              c["times"] if pd.notna(c["times"]) else ""],
                             2, "warn" if moved else "bad" if weak else None))
    ws2 = wb.create_sheet("Mapping_Tree")
    write_rows(ws2, ["Level", "Feeder name", "DT code", "DT name", "Consumer No", "Meter ID",
                     "Mapping", "Was (feeder / DT)", "Date", "Outages", "Outage times (off-on)"],
               rows, [16, 26, 28, 30, 15, 11, 44, 30, 11, 9, 80])

    # ---------------- how to read
    ws3 = wb.create_sheet("How_to_read", 0)
    for line in [
        "Feeder -> DTR -> Consumer -> time",
        "",
        "Grouped rows: use the 1 / 2 / 3 buttons at the top-left of each sheet (1 = feeders only, 2 = + DTRs, 3 = everything), or the +/- on the left.",
        "",
        "Feeder_Outage_View: every feeder outage, with the DTRs and consumers RECORDED on that feeder.",
        "   Green = consumer went off within ±2 min of the feeder (off AND on time).  Red = DID NOT GO OFF.",
        "   Yellow DTR row = fewer than half of its consumers went off.",
        "   Only consumers whose meter reported at least one event that day are listed.",
        "   Only 24 feeders had a feeder-meter outage on this day, so only those appear here.",
        "",
        "Mapping_Tree: EVERY consumer of the circle in the MI file. Consumers with outage data are placed under the CORRECTED",
        "   feeder and DTR; consumers with no outage that day stay under the RECORDED feeder/DT, marked 'no outage data - not checked'.",
        "   Feeder and DTR rows show how many of their consumers could actually be checked.",
        "   Yellow = moved (High/Medium confidence).  Red = weak sign of error or suspect source meter; kept as recorded.",
        "   'DT UNKNOWN - field check' = feeder is clear, but several DTRs on it behaved identically that day.",
        "",
        "Corrections are suggestions from outage timing; verify a sample in the field before updating records.",
    ]:
        ws3.append([line])
    ws3["A1"].font = Font(name="Arial", bold=True, size=14)
    for row in ws3.iter_rows(min_row=2):
        row[0].font = Font(name="Arial", size=10)
    ws3.column_dimensions["A"].width = 130

    tmp = Path(out).with_suffix(".tmp.xlsx")
    wb.save(tmp)
    tmp.replace(out)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mi", required=True); ap.add_argument("--data", required=True)
    ap.add_argument("--out", default="Feeder_DTR_Consumer.xlsx"); ap.add_argument("--window", type=int, default=2)
    a = ap.parse_args()
    cm, dm, fm = tc.load_master(a.mi)
    ev = load_reports(Path(a.data))
    r = tc.analyse(ev["consumer"], ev["dtr"], ev["feeder"], cm, dm, fm, tc.Settings(window_min=a.window))
    build(r, ev, cm, dm, fm, a.out, a.window)
    print("written:", a.out)


if __name__ == "__main__":
    main()
