"""
Baseline verification test: runs topology_check and breakdown_check on test data and verifies numbers.
"""
from pathlib import Path
import sys
import topology_check as tc
import breakdown_check as bc
from run_check import load_reports

def run_test():
    print("Loading MI...")
    cm, dm, fm = tc.load_master("Genus Consumer MI Data.xlsx")
    print("Loading reports...")
    ev = load_reports(Path("data"))
    
    print("Running breakdown check...")
    bres = bc.check(ev, cm, dm, fm, tol_min=2, max_events_per_day=40, suspect_share=0.10)
    bs = bres["summary"]
    print("Breakdown summary:")
    print(bs)

    print("Running topology analysis...")
    st = tc.Settings(window_min=2, max_events_per_day=40, min_alt_recall=0.6, max_rec_recall=0.4)
    r = tc.analyse(ev["consumer"], ev["dtr"], ev["feeder"], cm, dm, fm, st)
    s = r["summary"]
    print("Topology summary:")
    for k, v in s.items():
        print(f"  {k}: {v}")

    # Check assertions
    expected_topo = {
        "consumers_with_events": 47323,
        "faulty_meters_excluded": 17,
        "flagged_consumers": 7042,
        "flagged_high": 1496,
        "flagged_medium": 519,
        "flagged_low": 5027,
        "dtr_total": 4387,
        "dtr_correct": 372,
        "dtr_wrong": 125,
        "dtr_conflict": 5,
        "dtr_meter_issue": 3,
        "dtr_unclear": 124,
        "dtr_not_checkable": 3758,
    }

    mismatches = []
    for k, expected_val in expected_topo.items():
        actual_val = s.get(k)
        if actual_val != expected_val:
            mismatches.append(f"Topology {k}: expected {expected_val}, got {actual_val}")

    if bres["n_breakdowns"] != 51:
        mismatches.append(f"Feeder breakdowns: expected 51, got {bres['n_breakdowns']}")

    # Check breakdown summary counts
    # Row 0: Feeder -> DTR (all): 663 match / 514 mismatch
    # Row 1: Feeder -> Consumer (all): 16439 match / 5047 mismatch
    # Row 2: DTR -> Consumer (all): 11767 match / 1239 mismatch
    # Row 3: Feeder -> DTR (confirmed): 653 match / 317 mismatch
    # Row 4: Feeder -> Consumer (confirmed): 16373 match / 2998 mismatch
    # Row 5: DTR -> Consumer (confirmed): 11749 match / 1187 mismatch
    expected_bd = [
        ("Feeder -> DTR", 663, 514),
        ("Feeder -> Consumer", 16439, 5047),
        ("DTR -> Consumer", 11767, 1239),
        ("Feeder -> DTR  (confirmed breakdowns only)", 653, 317),
        ("Feeder -> Consumer  (confirmed breakdowns only)", 16373, 2998),
        ("DTR -> Consumer  (confirmed breakdowns only)", 11749, 1187),
    ]

    for check_name, exp_m, exp_mm in expected_bd:
        row = bs[bs["Check"] == check_name]
        if row.empty:
            mismatches.append(f"Missing row in breakdown summary: {check_name}")
            continue
        act_m = int(row["MATCH"].iat[0])
        act_mm = int(row["MISMATCH"].iat[0])
        if act_m != exp_m or act_mm != exp_mm:
            mismatches.append(f"{check_name}: expected MATCH {exp_m} / MISMATCH {exp_mm}, got {act_m} / {act_mm}")

    if mismatches:
        print("\nMISMATCHES FOUND:")
        for m in mismatches:
            print("  *", m)
        sys.exit(1)
    else:
        print("\nALL BASELINE NUMBERS MATCH PERFECTLY!")

if __name__ == "__main__":
    run_test()
