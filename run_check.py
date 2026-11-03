"""
run_check.py  -  one-shot run: read the MI workbook + outage reports, write the Excel report.

Usage (Windows or Linux):
    python run_check.py --mi Genus_Consumer_MI_Data.xlsx --data data_folder --out Mapping_Report.xlsx
`data_folder` holds any number of consumer / DTR / feeder outage reports (.csv or .xlsx);
the type of each file is detected from its columns.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

import topology_check as tc


def load_reports(folder: Path, log=print, raw_rows: dict | None = None):
    ev = {"consumer": [], "dtr": [], "feeder": []}
    for p in sorted(folder.iterdir()):
        if p.suffix.lower() not in (".csv", ".xlsx", ".xls") or p.name.startswith("~$"):
            continue
        try:
            df = tc.read_any(p)
        except Exception as e:  # unreadable file: skip, don't crash the pipeline
            log(f"  skip {p.name}: {e}")
            continue
        kind = tc.classify_report(df)
        if kind is None:
            continue
        nrm = tc.normalise_events(df, kind)
        ev[kind].append(nrm)
        if raw_rows is not None:
            raw_rows[kind] = raw_rows.get(kind, 0) + len(df)
            raw_rows[kind + "_ok"] = raw_rows.get(kind + "_ok", 0) + nrm.attrs.get("n_readable", len(nrm))
        log(f"  {kind:8s} {p.name}  ({len(df):,} rows)")
    return {k: (pd.concat(v, ignore_index=True).drop_duplicates(["kind", "meter_id", "start"])
                if v else pd.DataFrame(columns=["kind", "meter_id", "consumer_number", "start", "restore"]))
            for k, v in ev.items()}


COLS_WRONG = {
    "consumer_number": "Consumer No", "meter_id": "Meter ID", "events": "Outage events",
    "rec_feeder": "Recorded Feeder", "rec_dt": "Recorded DT",
    "suggested_feeder": "Suggested Feeder", "suggested_dt": "Suggested DT",
    "confidence": "Confidence (recorded mapping is wrong)", "reason": "Reason",
    "dtr_status": "DTR check", "dtr_rec_followed": "Recorded DT outages followed",
    "dtr_best_followed": "Suggested DT outages followed", "dtr_rec_score": "DT score (recorded)",
    "dtr_best_score": "DT score (suggested)",
    "feeder_status": "Feeder check", "feeder_rec_followed": "Recorded feeder outages followed",
    "feeder_best_followed": "Suggested feeder outages followed", "feeder_rec_score": "Feeder score (recorded)",
    "feeder_best_score": "Feeder score (suggested)",
}


def write_report(r: dict, out: Path, cons_map: pd.DataFrame, history: pd.DataFrame | None = None,
                 run_label: str = ""):
    st: tc.Settings = r["settings"]
    wrong = r["wrong"][list(COLS_WRONG)].rename(columns=COLS_WRONG)  # internal _ columns are dropped here
    if st.include_names:
        names = cons_map.set_index("consumer_number")[["Consumer Name", "Consumer Address"]]
        wrong = wrong.join(names, on="Consumer No")
    s = r["summary"]
    summary = pd.DataFrame([
        ("Run", run_label),
        ("Consumers with outage events", s["consumers_with_events"]),
        ("Faulty / flapping meters excluded", s["faulty_meters_excluded"]),
        ("Consumers missing from MI", s["consumers_not_in_MI"]),
        ("Consumers evaluable at DTR level", s["dtr_evaluable"]),
        ("Consumers evaluable at feeder level", s["feeder_evaluable"]),
        ("Flagged consumers (all)", None), ("  High confidence", None),
        ("  Medium confidence", None), ("  Low confidence", None),
        ("Sources suspected (group issue)", None), ("DTRs on wrong feeder", None),
        ("", ""),
        ("DTR -> FEEDER CHECK (all DTRs in the circle)", ""),
        ("DTRs total", s["dtr_total"]),
        ("  Correct feeder", s["dtr_correct"]),
        ("  Wrong feeder", s["dtr_wrong"]),
        ("  Conflicting evidence", s["dtr_conflict"]),
        ("  Explained by a feeder-meter issue", s["dtr_meter_issue"]),
        ("  Unclear", s["dtr_unclear"]),
        ("  Not checkable (no usable outage data)", s["dtr_not_checkable"]),
        ("  % correct of DTRs that could be judged", None),
        ("", ""),
        ("Settings", ""),
        ("Time window (min)", st.window_min), ("Max events/day before meter is 'faulty'", st.max_events_per_day),
        ("Min shared events for a suggestion", st.min_alt_match),
        ("Min share of suggested source's outages followed", st.min_alt_recall),
        ("Max share of recorded source's outages followed", st.max_rec_recall),
        ("Min score margin", st.min_margin),
    ], columns=["Item", "Value"])

    sheets = {"Summary": summary, "Wrong_Consumer_Mapping": wrong,
              "DTR_Feeder_Check": r["dtr_check"],
              "Mismatch_Patterns": r["patterns"], "DTR_on_Wrong_Feeder": r["dtr_wrong"], "Suspect_Source_Meters": r["suspects"],
              "Feeder_Event_Check": r["feeder_diag"], "Faulty_Meters_Excluded": r["faulty"],
              "Not_in_MI": r["not_in_mi"]}
    if history is not None and len(history):
        sheets["Flag_History"] = history
    tmp = out.with_suffix(".tmp.xlsx")
    with pd.ExcelWriter(tmp, engine="openpyxl") as xw:
        for name, df in sheets.items():
            (df if len(df) else pd.DataFrame({"(none)": []})).to_excel(xw, sheet_name=name, index=False)

    # formatting + live counts on Summary
    wb = load_workbook(tmp)
    hdr_fill = PatternFill("solid", fgColor="1F4E78")
    for ws in wb.worksheets:
        for c in ws[1]:
            c.font = Font(name="Arial", bold=True, color="FFFFFF")
            c.fill = hdr_fill
            c.alignment = Alignment(wrap_text=True, vertical="center")
        for row in ws.iter_rows(min_row=2):
            for c in row:
                c.font = Font(name="Arial", size=10)
        for i, col in enumerate(ws.columns, 1):
            width = max(len(str(c.value)) if c.value is not None else 0 for c in list(col)[:300])
            ws.column_dimensions[get_column_letter(i)].width = min(max(10, width + 2), 45)
        ws.freeze_panes = "A2"
        if ws.max_row > 1:
            ws.auto_filter.ref = ws.dimensions
    ws = wb["Wrong_Consumer_Mapping"]
    conf_col = next((c.column_letter for c in ws[1] if c.value == "Confidence (recorded mapping is wrong)"), None)
    fills = {"High": "F8CBAD", "Medium": "FFE699", "Low": "DDEBF7"}
    if conf_col:
        for c in ws[conf_col][1:]:
            if c.value in fills:
                c.fill = PatternFill("solid", fgColor=fills[c.value])
    sm = wb["Summary"]
    W = "Wrong_Consumer_Mapping"
    formulas = {8: f"=MAX(0,COUNTA('{W}'!A:A)-1)",
                9: f"=COUNTIF('{W}'!{conf_col}:{conf_col},\"High\")",
                10: f"=COUNTIF('{W}'!{conf_col}:{conf_col},\"Medium\")",
                11: f"=COUNTIF('{W}'!{conf_col}:{conf_col},\"Low\")",
                12: "=MAX(0,COUNTA(Suspect_Source_Meters!A:A)-1)",
                13: "=MAX(0,COUNTA(DTR_on_Wrong_Feeder!A:A)-1)"}
    for row, f in formulas.items():
        sm[f"B{row}"] = f
        sm[f"B{row}"].font = Font(name="Arial", size=10)
    for row in sm.iter_rows(min_row=2, max_col=1):
        if row[0].value in ("Settings", "DTR -> FEEDER CHECK (all DTRs in the circle)"):
            row[0].font = Font(name="Arial", bold=True)
        if row[0].value == "  % correct of DTRs that could be judged":
            rr = row[0].row
            # correct / (correct + wrong + conflict), counted from the rows above
            sm[f"B{rr}"] = f"=IFERROR(B{rr-6}/(B{rr-6}+B{rr-5}+B{rr-4}),0)"
            sm[f"B{rr}"].number_format = "0.0%"
            sm[f"B{rr}"].font = Font(name="Arial", size=10)
    wb.save(tmp)
    tmp.replace(out)  # atomic swap: Excel never sees a half-written file
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mi", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", default="Mapping_Report.xlsx")
    ap.add_argument("--window", type=int, default=2)
    a = ap.parse_args()
    cons_map, dtr_map, fdr_map = tc.load_master(a.mi)
    ev = load_reports(Path(a.data))
    r = tc.analyse(ev["consumer"], ev["dtr"], ev["feeder"], cons_map, dtr_map, fdr_map,
                   tc.Settings(window_min=a.window))
    write_report(r, Path(a.out), cons_map, run_label=pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"))
    print(r["summary"])
    print("written:", a.out)


if __name__ == "__main__":
    main()
