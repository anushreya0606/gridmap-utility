"""
breakdown_check.py  -  mapping check inside each FEEDER BREAKDOWN window, with MATCH / MISMATCH counts.

For every feeder breakdown (feeder meter off -> on) and the RECORDED mapping (MI file):

  1. Feeder -> DTR       : did each DTR recorded on that feeder go off during the breakdown?
  2. Feeder -> Consumer  : did each consumer recorded on that feeder go off during the breakdown?
  3. DTR -> Consumer     : for each DTR that went off in the breakdown, did its recorded consumers
                           go off during the DTR's own off-time (which lies inside the feeder breakdown)?

MATCH rule ("within breakdown time"):
    the element has an outage that STARTS between  (breakdown off time - tol)  and  (breakdown on time + tol)
    tol = 2 minutes by default (meter clocks are not perfectly in sync).
An extra column "Exact timing" also says whether off AND on time are both within +/- tol of the feeder's.

Elements with no data are NOT counted as mismatch:
    DTR with no DTR meter                        -> "NO DTR METER"
    consumer whose meter sent no events that day -> "NO METER DATA"

    python breakdown_check.py --mi "Genus Consumer MI Data.xlsx" --data data --out Breakdown_Mapping_Check.xlsx
"""
from __future__ import annotations
import argparse
from pathlib import Path
import numpy as np
import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

import topology_check as tc
from run_check import load_reports


def _first_in_window(ev: pd.DataFrame, key: str, ids, w0, w1):
    """For each id, the first outage starting inside [w0, w1] (or None)."""
    sub = ev[ev[key].isin(ids) & (ev["start"] >= w0) & (ev["start"] <= w1)]
    sub = sub.sort_values("start").drop_duplicates(key)
    return {k: (s, r) for k, s, r in sub[[key, "start", "restore"]].itertuples(index=False)}


def _exact(a_s, a_r, b_s, b_r, tol):
    if pd.isna(a_s) or pd.isna(b_s):
        return ""
    ok = abs(a_s - b_s) <= tol and (pd.isna(a_r) or pd.isna(b_r) or abs(a_r - b_r) <= tol)
    return "Yes" if ok else "No"


def _mins(a, b):
    return "" if pd.isna(a) or pd.isna(b) else round((a - b).total_seconds() / 60, 1)


def check(ev: dict, cm: pd.DataFrame, dm: pd.DataFrame, fm: pd.DataFrame, tol_min: int = 2,
          max_events_per_day: int = 40, suspect_share: float = 0.10) -> dict:
    tol = pd.Timedelta(minutes=tol_min)
    cev = ev["consumer"].copy()
    per_day = cev.groupby(["consumer_number", cev["start"].dt.date]).size().groupby(level=0).max()
    faulty = set(per_day[per_day > max_events_per_day].index)
    reporting = set(cev["consumer_number"]) - faulty
    cev = cev[cev["consumer_number"].isin(reporting)]

    dev = ev["dtr"].merge(dm[["meter_id", "dt_code"]], on="meter_id", how="inner")
    metered_dt = set(dm["dt_code"].dropna())             # DTRs that have a DTR meter installed
    fev = ev["feeder"].merge(fm[["meter_id", "feeder_code", "feeder_name"]], on="meter_id", how="inner")
    fev = fev.sort_values(["feeder_code", "start"]).reset_index(drop=True)

    # recorded mapping
    dt_feeder = dm.dropna(subset=["dt_code"]).drop_duplicates("dt_code").set_index("dt_code")["feeder_code"]
    dt_feeder = dt_feeder.combine_first(
        cm.dropna(subset=["rec_dt"]).groupby("rec_dt")["rec_feeder"].agg(lambda x: x.mode().iat[0]))
    dt_name = dm.dropna(subset=["dt_code"]).drop_duplicates("dt_code").set_index("dt_code")["dt_name"].combine_first(
        cm.groupby("rec_dt")["rec_dt_name"].first())
    dt_tab = dt_feeder.rename("feeder").reset_index().rename(columns={"index": "dt_code"})
    dt_tab.columns = ["dt_code", "feeder"]
    cons_of_fd = cm.groupby("rec_feeder")
    cons_of_dt = cm.groupby("rec_dt")

    fd_dt_rows, fd_c_rows, dt_c_rows = [], [], []
    for _, e in fev.iterrows():
        fc, S, E = e["feeder_code"], e["start"], e["restore"]
        w0, w1 = S - tol, (E if pd.notna(E) else S) + tol
        bid = f"{fc} @ {S:%d-%m %H:%M}"
        base = {"Breakdown": bid, "Feeder code": fc, "Feeder name": e["feeder_name"],
                "Feeder off": S, "Feeder on": E,
                "Breakdown (min)": _mins(E, S)}

        # ---- 1. feeder -> DTR
        dts = dt_tab.loc[dt_tab["feeder"] == fc, "dt_code"].tolist()
        dhit = _first_in_window(dev, "dt_code", dts, w0, w1)
        dt_status = {}
        for dt in sorted(dts):
            if dt not in metered_dt:
                st = "NO DTR METER"
                s = r = None
            elif dt in dhit:
                s, r = dhit[dt]
                st = "MATCH"
            else:
                s = r = None
                st = "MISMATCH"
            dt_status[dt] = (st, s, r)
            fd_dt_rows.append({**base, "DT code": dt, "DT name": dt_name.get(dt), "DTR off": s, "DTR on": r,
                               "Off diff (min)": _mins(s, S), "Result": st,
                               "Exact timing (off & on within tol)": _exact(s, r, S, E, tol) if st == "MATCH" else ""})

        # ---- 2. feeder -> consumer
        if fc in cons_of_fd.groups:
            cons = cons_of_fd.get_group(fc)
            chit = _first_in_window(cev, "consumer_number", set(cons["consumer_number"]), w0, w1)
            for _, c in cons.iterrows():
                cn = c["consumer_number"]
                if cn not in reporting:
                    st, s, r = ("FAULTY METER" if cn in faulty else "NO METER DATA"), None, None
                elif cn in chit:
                    (s, r), st = chit[cn], "MATCH"
                else:
                    st, s, r = "MISMATCH", None, None
                fd_c_rows.append({**base, "DT code": c["rec_dt"], "Consumer No": cn, "Meter ID": c["meter_id"],
                                  "Consumer off": s, "Consumer on": r, "Off diff (min)": _mins(s, S),
                                  "Result": st,
                                  "Exact timing (off & on within tol)": _exact(s, r, S, E, tol) if st == "MATCH" else ""})

        # ---- 3. DTR -> consumer (only DTRs whose meter went off in this breakdown)
        for dt, (st_dt, ds, dr) in dt_status.items():
            if st_dt != "MATCH" or dt not in cons_of_dt.groups:
                continue
            cons = cons_of_dt.get_group(dt)
            v0, v1 = ds - tol, (dr if pd.notna(dr) else ds) + tol
            chit = _first_in_window(cev, "consumer_number", set(cons["consumer_number"]), v0, v1)
            for _, c in cons.iterrows():
                cn = c["consumer_number"]
                if cn not in reporting:
                    st, s, r = ("FAULTY METER" if cn in faulty else "NO METER DATA"), None, None
                elif cn in chit:
                    (s, r), st = chit[cn], "MATCH"
                else:
                    st, s, r = "MISMATCH", None, None
                dt_c_rows.append({"Breakdown": bid, "Feeder code": fc, "DT code": dt, "DT name": dt_name.get(dt),
                                  "DTR off": ds, "DTR on": dr, "Consumer No": cn, "Meter ID": c["meter_id"],
                                  "Consumer off": s, "Consumer on": r, "Off diff vs DTR (min)": _mins(s, ds),
                                  "Result": st,
                                  "Exact timing (off & on within tol)": _exact(s, r, ds, dr, tol) if st == "MATCH" else ""})

    fd_dt, fd_c, dt_c = pd.DataFrame(fd_dt_rows), pd.DataFrame(fd_c_rows), pd.DataFrame(dt_c_rows)

    # a breakdown that almost none of the feeder's own consumers saw is itself suspect
    # (feeder meter on another feeder, or a meter-side event rather than a real outage)
    judged = fd_c[fd_c["Result"].isin(["MATCH", "MISMATCH"])] if len(fd_c) else fd_c
    share = judged.groupby("Breakdown")["Result"].agg(lambda x: (x == "MATCH").mean()) if len(judged) else pd.Series(dtype=float)
    bstat = pd.Series(np.where(share < suspect_share, "SUSPECT BREAKDOWN", "Confirmed"), index=share.index)
    for df in (fd_dt, fd_c, dt_c):
        if len(df):
            df.insert(1, "Breakdown status", df["Breakdown"].map(bstat).fillna("Not verifiable"))

    # ---- per-element totals across all breakdowns (an element can be in several breakdowns)
    def per_element(df, keys):
        if df.empty:
            return pd.DataFrame()
        d = df[df["Result"].isin(["MATCH", "MISMATCH"])]
        g = d.groupby(keys)["Result"].agg(breakdowns="size", matched=lambda x: int((x == "MATCH").sum()))
        g["mismatched"] = g["breakdowns"] - g["matched"]
        g["Overall"] = np.where(g["mismatched"] == 0, "MATCH",
                                np.where(g["matched"] == 0, "MISMATCH", "PARTIAL"))
        return g.reset_index().rename(columns={"breakdowns": "Breakdowns checked", "matched": "Matched",
                                               "mismatched": "Mismatched"})

    el_fd_dt = per_element(fd_dt, ["Feeder code", "DT code"])
    el_fd_c = per_element(fd_c, ["Feeder code", "Consumer No"])
    el_dt_c = per_element(dt_c, ["DT code", "Consumer No"])

    def counts(df):
        vc = df["Result"].value_counts() if len(df) else pd.Series(dtype=int)
        m, mm = int(vc.get("MATCH", 0)), int(vc.get("MISMATCH", 0))
        nodata = int(len(df) - m - mm)
        return m, mm, nodata, (m / (m + mm) if m + mm else None)

    def el_counts(df):
        if df.empty:
            return 0, 0, 0
        vc = df["Overall"].value_counts()
        return int(vc.get("MATCH", 0)), int(vc.get("PARTIAL", 0)), int(vc.get("MISMATCH", 0))

    summary = []
    for name, ev_df, el_df, unit in [("Feeder -> DTR", fd_dt, el_fd_dt, "DTRs"),
                                     ("Feeder -> Consumer", fd_c, el_fd_c, "consumers"),
                                     ("DTR -> Consumer", dt_c, el_dt_c, "consumers")]:
        m, mm, nd, pct = counts(ev_df)
        em, ep, emm = el_counts(el_df)
        summary.append({"Check": name, "Breakdown checks (rows)": len(ev_df), "MATCH": m, "MISMATCH": mm,
                        "No data (not counted)": nd, "Match %": pct,
                        "Unique elements checked": em + ep + emm,
                        "Always matched": em, "Partly matched": ep, "Never matched": emm})
    for name, ev_df in [("Feeder -> DTR", fd_dt), ("Feeder -> Consumer", fd_c), ("DTR -> Consumer", dt_c)]:
        ok = ev_df[ev_df["Breakdown status"] == "Confirmed"] if len(ev_df) else ev_df
        m, mm, nd, pct = counts(ok)
        summary.append({"Check": name + "  (confirmed breakdowns only)", "Breakdown checks (rows)": len(ok),
                        "MATCH": m, "MISMATCH": mm, "No data (not counted)": nd, "Match %": pct})
    summary = pd.DataFrame(summary)
    bd_tab = pd.DataFrame({"Breakdown": share.index, "Own consumers that went off (share)": share.values,
                           "Breakdown status": bstat.values}).sort_values("Own consumers that went off (share)")

    feeders = fev.groupby(["feeder_code", "feeder_name"]).size().rename("Breakdowns").reset_index()
    return {"summary": summary, "fd_dt": fd_dt, "fd_c": fd_c, "dt_c": dt_c,
            "el_fd_dt": el_fd_dt, "el_fd_c": el_fd_c, "el_dt_c": el_dt_c,
            "feeders": feeders, "breakdowns": bd_tab, "tol": tol_min, "n_breakdowns": len(fev),
            "suspect_share": suspect_share}


def _overall(df, keys):
    """Per element across all breakdowns: MATCH (every time), PARTIAL, MISMATCH (never), or the no-data label."""
    if df.empty:
        return pd.DataFrame(columns=keys + ["overall", "went_off", "judged"])
    j = df.assign(_m=(df["Result"] == "MATCH").astype(int), _j=df["Result"].isin(["MATCH", "MISMATCH"]).astype(int))
    g = j.groupby(keys).agg(went_off=("_m", "sum"), judged=("_j", "sum"), label=("Result", "first")).reset_index()
    g["overall"] = np.where(g["judged"] == 0, g["label"],
                            np.where(g["went_off"] == g["judged"], "MATCH",
                                     np.where(g["went_off"] == 0, "MISMATCH", "PARTIAL")))
    return g.drop(columns="label")


def _cnt(s: pd.Series):
    vc = s.value_counts()
    m, p, mm = int(vc.get("MATCH", 0)), int(vc.get("PARTIAL", 0)), int(vc.get("MISMATCH", 0))
    return m, p, mm, int(len(s) - m - p - mm), (m / (m + p + mm) if m + p + mm else None)


def feeder_tables(r: dict):
    """'Feeder X has N DTRs, of which M match and K mismatch' - per feeder, and per feeder -> DTR."""
    fd_dt, fd_c, dt_c = r["fd_dt"], r["fd_c"], r["dt_c"]
    # use only confirmed breakdowns for a feeder; if ALL of its breakdowns are suspect, use them and say so
    stat = pd.concat([d[["Feeder code", "Breakdown", "Breakdown status"]] for d in (fd_dt, fd_c) if len(d)]).drop_duplicates()
    has_ok = set(stat.loc[stat["Breakdown status"] != "SUSPECT BREAKDOWN", "Feeder code"])
    keep = lambda d: d[(d["Breakdown status"] != "SUSPECT BREAKDOWN") | ~d["Feeder code"].isin(has_ok)] if len(d) else d
    fd_dt, fd_c, dt_c = keep(fd_dt), keep(fd_c), keep(dt_c)
    o_dt = _overall(fd_dt, ["Feeder code", "DT code"])
    o_fc = _overall(fd_c, ["Feeder code", "DT code", "Consumer No"])
    o_dc = _overall(dt_c, ["Feeder code", "DT code", "Consumer No"])
    names = fd_dt.drop_duplicates("Feeder code").set_index("Feeder code")["Feeder name"] if len(fd_dt) else pd.Series(dtype=str)
    if len(fd_c):
        names = names.combine_first(fd_c.drop_duplicates("Feeder code").set_index("Feeder code")["Feeder name"])
    dtn = fd_dt.drop_duplicates("DT code").set_index("DT code")["DT name"] if len(fd_dt) else pd.Series(dtype=str)
    bd = stat

    feeders, tree = [], []
    for fc in sorted(set(o_dt["Feeder code"]) | set(o_fc["Feeder code"])):
        b = bd[bd["Feeder code"] == fc]
        dts = o_dt[o_dt["Feeder code"] == fc]
        fcs = o_fc[o_fc["Feeder code"] == fc]
        dcs = o_dc[o_dc["Feeder code"] == fc]
        dm_, dp_, dmm, dnd, dpct = _cnt(dts["overall"])
        cm_, cp_, cmm, cnd, cpct = _cnt(fcs["overall"])
        xm, xp, xmm, xnd, xpct = _cnt(dcs["overall"])
        row = {"Feeder code": fc, "Feeder name": names.get(fc),
               "Breakdowns": b["Breakdown"].nunique(),
               "Suspect breakdowns": int((b.drop_duplicates("Breakdown")["Breakdown status"] == "SUSPECT BREAKDOWN").sum()),
               "Counts based on": ("confirmed breakdowns" if fc in has_ok else "SUSPECT breakdowns only - treat with care"),
               "DTRs on feeder": len(dts), "DTR MATCH": dm_, "DTR PARTIAL": dp_, "DTR MISMATCH": dmm,
               "DTR no meter": dnd, "DTR match %": dpct,
               "Consumers on feeder": len(fcs), "Consumer MATCH": cm_, "Consumer PARTIAL": cp_,
               "Consumer MISMATCH": cmm, "Consumer no data": cnd, "Consumer match %": cpct,
               "DT->Consumer MATCH": xm, "DT->Consumer PARTIAL": xp, "DT->Consumer MISMATCH": xmm,
               "DT->Consumer match %": xpct}
        feeders.append(row)
        tree.append(({**row, "Level": "FEEDER"}, 0))
        all_dt = sorted(set(dts["DT code"]) | set(fcs["DT code"].dropna()))
        for dt in all_dt:
            d = dts[dts["DT code"] == dt]
            c = fcs[fcs["DT code"] == dt]
            x = dcs[dcs["DT code"] == dt]
            cm2, cp2, cmm2, cnd2, cpct2 = _cnt(c["overall"])
            xm2, xp2, xmm2, xnd2, xpct2 = _cnt(x["overall"])
            res = d["overall"].iat[0] if len(d) else "NOT ON FEEDER (DTR MI)"
            off = f"{int(d['went_off'].iat[0])}/{int(d['judged'].iat[0])}" if len(d) and d["judged"].iat[0] else ""
            tree.append(({"Level": "  DTR", "Feeder code": fc, "DT code": dt, "DT name": dtn.get(dt),
                          "DTR result": res, "DTR went off (breakdowns)": off,
                          "Consumers on feeder": len(c), "Consumer MATCH": cm2, "Consumer PARTIAL": cp2,
                          "Consumer MISMATCH": cmm2, "Consumer no data": cnd2, "Consumer match %": cpct2,
                          "DT->Consumer MATCH": xm2, "DT->Consumer PARTIAL": xp2,
                          "DT->Consumer MISMATCH": xmm2, "DT->Consumer match %": xpct2}, 1))
    feeders = pd.DataFrame(feeders)
    cols = ["Level", "Feeder code", "Feeder name", "Breakdowns", "Suspect breakdowns", "Counts based on", "DT code", "DT name",
            "DTR result", "DTR went off (breakdowns)", "DTRs on feeder", "DTR MATCH", "DTR PARTIAL",
            "DTR MISMATCH", "DTR no meter", "DTR match %", "Consumers on feeder", "Consumer MATCH",
            "Consumer PARTIAL", "Consumer MISMATCH", "Consumer no data", "Consumer match %",
            "DT->Consumer MATCH", "DT->Consumer PARTIAL", "DT->Consumer MISMATCH", "DT->Consumer match %"]
    tree_df = pd.DataFrame([t for t, _ in tree]).reindex(columns=cols)
    levels = [lv for _, lv in tree]
    return feeders, tree_df, levels


def write(r: dict, out: Path, include_rows: bool = True):
    tmp = Path(out).with_suffix(".tmp.xlsx")
    notes = pd.DataFrame({"How to read": [
        f"Feeder breakdowns checked: {r['n_breakdowns']} (on {len(r['feeders'])} feeders that have a feeder-meter outage in the data).",
        "Mapping checked = the RECORDED mapping in the MI file.",
        f"MATCH = the DTR/consumer had an outage starting inside the breakdown window (feeder off - {r['tol']} min  to  feeder on + {r['tol']} min).",
        "DTR -> Consumer uses the DTR's own off/on time as the window, for DTRs that went off in the breakdown.",
        "MISMATCH = it has data that day but did NOT go off in that window -> likely mapped to the wrong feeder/DTR.",
        "NO DTR METER / NO METER DATA / FAULTY METER = cannot be judged; NOT counted as match or mismatch.",
        "Exact timing = off AND on time both within the tolerance of the feeder's (or DTR's) - the strongest kind of match.",
        "Per-element sheets: across all breakdowns, Always matched / Partly matched / Never matched.",
        "Feeder_Summary: one row per feeder - how many DTRs / consumers it has and how many MATCH, PARTIAL, MISMATCH.",
        "Feeder_DTR_Counts: the same, opened up per DTR (use the 1/2 buttons at the top-left to collapse/expand).",
        "   MATCH = went off in every breakdown of the feeder; PARTIAL = in some; MISMATCH = in none.",
        "   DT->Consumer counts only DTRs whose meter went off in the breakdown.",
        "   Suspect breakdowns are left out of these counts when the feeder has at least one confirmed breakdown.",
        f"SUSPECT BREAKDOWN = fewer than {int(r['suspect_share']*100)}% of the feeder's own consumers went off. The breakdown record itself is doubtful",
        "   (feeder meter on another feeder, or a meter-side event), so its mismatches say little about the mapping.",
        "   Summary therefore shows the counts twice: all breakdowns, and confirmed breakdowns only.",
    ]})
    feeders, tree, levels = feeder_tables(r)
    sheets = {"Summary": r["summary"], "Feeder_Summary": feeders, "Feeder_DTR_Counts": tree,
              "Notes": notes, "Breakdown_Status": r["breakdowns"],
              "Feeder_DTR_by_DTR": r["el_fd_dt"], "Feeder_Consumer_by_Cons": r["el_fd_c"],
              "DTR_Consumer_by_Cons": r["el_dt_c"]}
    if include_rows:
        sheets.update({"Feeder_DTR_Breakdowns": r["fd_dt"], "Feeder_Consumer_Breakdowns": r["fd_c"],
                       "DTR_Consumer_Breakdowns": r["dt_c"]})
    with pd.ExcelWriter(tmp, engine="openpyxl", datetime_format="dd-mm-yyyy hh:mm") as xw:
        for name, df in sheets.items():
            (df if len(df) else pd.DataFrame({"(none)": []})).to_excel(xw, sheet_name=name, index=False)

    wb = load_workbook(tmp)
    hdr = PatternFill("solid", fgColor="1F4E78")
    fills = {"MATCH": PatternFill("solid", fgColor="C6EFCE"), "MISMATCH": PatternFill("solid", fgColor="FFC7CE"),
             "PARTIAL": PatternFill("solid", fgColor="FFEB9C")}
    f_hdr, f_norm = Font(name="Arial", bold=True, color="FFFFFF"), Font(name="Arial", size=10)
    for ws in wb.worksheets:
        for c in ws[1]:
            c.font, c.fill = f_hdr, hdr
            c.alignment = Alignment(wrap_text=True, vertical="center")
        res_col = next((c.column for c in ws[1] if c.value in ("Result", "Overall")), None)
        for row in ws.iter_rows(min_row=2):
            for c in row:
                c.font = f_norm
            if res_col:
                c = row[res_col - 1]
                if c.value in fills:
                    c.fill = fills[c.value]
        for i, col in enumerate(ws.iter_cols(max_row=min(ws.max_row, 200)), 1):
            w = max((len(str(c.value)) for c in col if c.value is not None), default=8)
            ws.column_dimensions[get_column_letter(i)].width = min(max(10, w + 2), 60)
        ws.freeze_panes = "A2"
        if ws.max_row > 1:
            ws.auto_filter.ref = ws.dimensions
    pct_cols = {"DTR match %", "Consumer match %", "DT->Consumer match %"}
    fill_fd = PatternFill("solid", fgColor="DDEBF7")
    for name in ("Feeder_Summary", "Feeder_DTR_Counts"):
        w = wb[name]
        idx = [c.column for c in w[1] if c.value in pct_cols]
        res_c = next((c.column for c in w[1] if c.value == "DTR result"), None)
        for i, row in enumerate(w.iter_rows(min_row=2), start=0):
            for j in idx:
                row[j - 1].number_format = "0.0%"
            if name == "Feeder_DTR_Counts":
                if levels[i] == 0:
                    for c in row:
                        c.fill, c.font = fill_fd, Font(name="Arial", size=10, bold=True)
                else:
                    w.row_dimensions[row[0].row].outline_level = 1
                    if res_c and row[res_c - 1].value in fills:
                        row[res_c - 1].fill = fills[row[res_c - 1].value]
        w.sheet_properties.outlinePr.summaryBelow = False
    ws = wb["Summary"]
    for row in ws.iter_rows(min_row=2):
        row[5].number_format = "0.0%"
    for row in wb["Breakdown_Status"].iter_rows(min_row=2):
        row[1].number_format = "0.0%"
        if row[2].value == "SUSPECT BREAKDOWN":
            row[2].fill = fills["MISMATCH"]
    # live totals from the per-breakdown sheets, so the numbers can be checked in Excel
    if include_rows:
        n = len(r["summary"]) + 1
        ws.cell(n + 2, 1, "Cross-check (formulas on the breakdown sheets)").font = Font(name="Arial", bold=True)
        for i, (lab, sh) in enumerate([("Feeder -> DTR", "Feeder_DTR_Breakdowns"),
                                       ("Feeder -> Consumer", "Feeder_Consumer_Breakdowns"),
                                       ("DTR -> Consumer", "DTR_Consumer_Breakdowns")]):
            r0 = n + 3 + i
            ws.cell(r0, 1, lab).font = f_norm
            col = None
            for c in wb[sh][1]:
                if c.value == "Result":
                    col = c.column_letter
            if col:
                ws.cell(r0, 3, f"=COUNTIF('{sh}'!{col}:{col},\"MATCH\")").font = f_norm
                ws.cell(r0, 4, f"=COUNTIF('{sh}'!{col}:{col},\"MISMATCH\")").font = f_norm
    ws.column_dimensions["A"].width = 30
    wb["Notes"].column_dimensions["A"].width = 140
    wb.save(tmp)
    tmp.replace(out)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mi", required=True); ap.add_argument("--data", required=True)
    ap.add_argument("--out", default="Breakdown_Mapping_Check.xlsx")
    ap.add_argument("--tol", type=int, default=2, help="timing tolerance in minutes (default 2)")
    a = ap.parse_args()
    cm, dm, fm = tc.load_master(a.mi)
    ev = load_reports(Path(a.data))
    r = check(ev, cm, dm, fm, a.tol)
    write(r, Path(a.out))
    print(r["summary"].to_string(index=False))
    print("written:", a.out)


if __name__ == "__main__":
    main()
