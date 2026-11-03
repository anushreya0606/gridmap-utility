"""
evaluate.py  -  How well does the method work? (no field ground truth needed)

We take consumers the method currently considers correctly mapped, deliberately
corrupt their recorded DT/feeder in a copy of the MI table, and check whether the
method (a) flags them and (b) recovers the true DT/feeder. Consumers we did not touch
tell us the false-alarm rate.

    python evaluate.py --mi Genus_Consumer_MI_Data.xlsx --data data_folder --n 400
"""
import argparse
from pathlib import Path
import numpy as np
import pandas as pd
import topology_check as tc
from run_check import load_reports


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mi", required=True); ap.add_argument("--data", required=True)
    ap.add_argument("--n", type=int, default=400); ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    rng = np.random.default_rng(a.seed)
    cm, dm, fm = tc.load_master(a.mi)
    ev = load_reports(Path(a.data), log=lambda *x: None)
    quiet = lambda *x: None

    base = tc.analyse(ev["consumer"], ev["dtr"], ev["feeder"], cm, dm, fm, log=quiet)["results"]
    ok = base[base["dtr_status"] == "OK"]
    dt_fd = cm.dropna(subset=["rec_dt"]).groupby("rec_dt")["rec_feeder"].first()
    pick = ok.sample(n=min(a.n, len(ok)), random_state=a.seed)

    corrupt = cm.copy().set_index("consumer_number")
    truth = []
    all_dts = dt_fd.index.values
    for i, (_, r) in enumerate(pick.iterrows()):
        same_feeder = i % 2 == 0           # half: wrong DT on the SAME feeder (hard); half: other feeder
        pool = dt_fd[(dt_fd == r["rec_feeder"]) if same_feeder else (dt_fd != r["rec_feeder"])]
        pool = pool[pool.index != r["rec_dt"]]
        if pool.empty:
            continue
        fake = rng.choice(pool.index.values)
        corrupt.at[r["consumer_number"], "rec_dt"] = fake
        corrupt.at[r["consumer_number"], "rec_feeder"] = dt_fd[fake]
        truth.append({"consumer_number": r["consumer_number"], "true_dt": r["rec_dt"],
                      "true_feeder": r["rec_feeder"], "case": "same feeder" if same_feeder else "other feeder"})
    truth = pd.DataFrame(truth)
    corrupt = corrupt.reset_index()

    res = tc.analyse(ev["consumer"], ev["dtr"], ev["feeder"], corrupt, dm, fm, log=quiet)["results"]
    res = res.set_index("consumer_number")
    flag = lambda s: s.astype(str).str.startswith(("WRONG", "LIKELY", "GROUP"))
    is_flag = flag(res["dtr_status"]) | flag(res["feeder_status"])

    t = truth.join(res, on="consumer_number")
    t["flagged"] = is_flag.reindex(t["consumer_number"]).values
    t["dt_exact"] = t["suggested_dt"] == t["true_dt"]
    t["dt_in_candidates"] = t["dt_exact"] | t.apply(
        lambda x: x["true_dt"] in str(x["_dtr_cands_all"]).split("|"), axis=1)
    t["feeder_recovered"] = t["suggested_feeder"] == t["true_feeder"]
    untouched = res.index.isin(ok.index.map(lambda i: ok.at[i, "consumer_number"])) & ~res.index.isin(truth["consumer_number"])
    fp = is_flag[untouched].mean()

    print(f"injected errors: {len(truth)}")
    print(t.groupby("case")[["flagged", "dt_exact", "dt_in_candidates", "feeder_recovered"]].mean().round(3).to_string())
    print(f"overall detection rate: {t['flagged'].mean():.3f}")
    fl = t[t["flagged"]]
    print(f"of flagged: feeder suggestion correct {fl['feeder_recovered'].mean():.3f}, "
          f"true DT in candidate list {fl['dt_in_candidates'].mean():.3f}, mean candidates "
          f"{fl['dtr_n_candidates'].mean():.1f}")
    print(f"false-alarm rate on untouched OK consumers: {fp:.4f}")
    t.to_csv("evaluation_detail.csv", index=False)


if __name__ == "__main__":
    main()
