"""
topology_check.py  -  Feeder / DTR / Consumer mapping check from outage timing.

Idea: if a consumer is really fed by DTR X (and feeder Y), it should lose and
regain power at (almost) the same moments as X and Y. For every consumer we
score its outage events against every DTR and every feeder that has outage
events, and flag consumers whose recorded mapping (Consumer MI) is clearly
beaten by another DTR/feeder.

Event match rule: |start difference| <= WINDOW_MIN AND |restore difference| <= WINDOW_MIN.
Matching both start AND restore is what separates feeders that trip together
(a substation/33 kV trip) but are restored at different times.

Score for consumer i vs source s (a DTR or feeder):
    m  = number of source events the consumer matched
    J  = m / (n_i + n_s - m)          (Jaccard overlap of the two event sets)
    R  = m / n_s                      (share of the source's outages the consumer followed)
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import numpy as np
import pandas as pd


# ----------------------------------------------------------------------------- settings
@dataclass
class Settings:
    window_min: int = 2            # time tolerance for start and restore (minutes)
    max_events_per_day: int = 40   # consumers above this are treated as faulty/flapping meters
    min_events: int = 2            # a consumer/source needs at least this many events to be judged
    min_alt_match: int = 2         # suggested source must share at least this many events
    min_alt_recall: float = 0.6    # consumer must follow >=60% of the suggested source's outages
    max_rec_recall: float = 0.4    # ...and <=40% of its recorded source's outages
    min_margin: float = 0.25       # Jaccard(best alternative) - Jaccard(recorded) must exceed this
    tie_gap: float = 0.05          # alternatives within this Jaccard gap are "tied" -> ambiguous
    virtual_min_share: float = 0.5 # peer-group event: >=50% of a DT's reporting consumers off together
    virtual_min_consumers: int = 3 # ...and at least 3 of them (excluding the consumer being judged)
    group_issue_share: float = 0.5 # if >=50% of a source's consumers are flagged, suspect the source
    include_names: bool = False    # consumer names/addresses are personal data; off by default


# ----------------------------------------------------------------------------- loading
def _clean_cols(df):
    df.columns = [str(c).strip() for c in df.columns]
    return df


def load_master(mi_path: str | Path):
    """Recorded mapping from the Genus MI workbook."""
    mi_path = Path(mi_path)
    need = {"Total Consumer MI Data": ["Account Number", "Feeder Code", "DT Code", "New Meter Serial No"],
            "Total DTR MI": ["New Meter Serial No", "DT Code", "Feeder Code"],
            "Total Feeder MI": ["New Meter Serial No", "Feeder Code"]}
    sheets = pd.ExcelFile(mi_path).sheet_names
    missing = [n for n in need if n not in sheets]
    if missing:
        raise ValueError(f"MI workbook is missing sheet(s): {', '.join(missing)}. Sheets found: {', '.join(sheets)}")
    frames = {}
    for name, cols in need.items():
        df = _clean_cols(pd.read_excel(mi_path, sheet_name=name, dtype=str))
        lack = [c for c in cols if c not in df.columns]
        if lack:
            raise ValueError(f"Sheet '{name}' is missing column(s): {', '.join(lack)}")
        frames[name] = df
    cons, dtr, fdr = frames["Total Consumer MI Data"], frames["Total DTR MI"], frames["Total Feeder MI"]
    cons = cons.rename(columns={"Account Number": "consumer_number", "Feeder Code": "rec_feeder",
                                "DT Code": "rec_dt", "New Meter Serial No": "meter_id",
                                "Feeder Name": "rec_feeder_name", "DT Name": "rec_dt_name"})
    cons = cons.drop_duplicates("consumer_number", keep="last")
    dtr = dtr.rename(columns={"New Meter Serial No": "meter_id", "DT Code": "dt_code",
                              "Feeder Code": "feeder_code", "DT Name": "dt_name",
                              "Feeder Name": "feeder_name"}).drop_duplicates("meter_id", keep="last")
    fdr = fdr.rename(columns={"New Meter Serial No": "meter_id", "Feeder Code": "feeder_code",
                              "Feeder Name": "feeder_name"}).drop_duplicates("meter_id", keep="last")
    return cons, dtr, fdr


def _parse_time(s):
    s = s.astype(str).str.strip()
    out = pd.to_datetime(s, format="%d-%m-%Y %H:%M:%S", errors="coerce")
    miss = out.isna()
    if miss.any():   # other layouts: Excel date cells, dd/mm/yyyy, yyyy-mm-dd ...  (day first, as in Indian reports)
        out[miss] = pd.to_datetime(s[miss].replace({"nan": None, "NaT": None, "": None}),
                                   errors="coerce", dayfirst=True, format="mixed")
    return out


def read_any(path: str | Path) -> pd.DataFrame:
    """Read a report CSV/XLSX; skips the 'Discom Name / Report Name / Filters' preamble if present."""
    path = Path(path)
    if path.suffix.lower() == ".csv":
        with open(path, encoding="utf-8", errors="ignore") as fh:
            head = [next(fh, "") for _ in range(10)]
        skip = next((i for i, l in enumerate(head) if l.startswith("Zone,")), 0)
        df = pd.read_csv(path, skiprows=skip, dtype=str)
    else:
        df = pd.read_excel(path, dtype=str)
    return _clean_cols(df)


def classify_report(df: pd.DataFrame) -> str | None:
    cols = {c.lower() for c in df.columns}
    if "consumer_number" in cols:
        return "consumer"
    if "dt_name" in cols or "dtname" in cols:
        return "dtr"
    if "feeder_code" in cols or "feedercode" in cols:
        return "feeder"
    return None


def normalise_events(df: pd.DataFrame, kind: str) -> pd.DataFrame:
    """Return columns: kind, meter_id, consumer_number(optional), start, restore."""
    low = {c.lower().replace(" ", ""): c for c in df.columns}

    def g(*names):
        for n in names:
            if n in low:
                return df[low[n]]
        raise ValueError(f"{kind} outage report is missing column '{names[0]}'. Columns found: {', '.join(df.columns)}")
    out = pd.DataFrame({
        "kind": kind,
        "meter_id": g("meter_id", "meterid").astype(str).str.strip(),
        "start": _parse_time(g("event_occurred", "eventoccurred")),
        "restore": _parse_time(g("event_restored", "eventrestored")),
    })
    out["consumer_number"] = g("consumer_number").astype(str).str.strip() if kind == "consumer" else None
    out["start"] = out["start"].astype("datetime64[ns]")
    out["restore"] = out["restore"].astype("datetime64[ns]")
    n_ok = int(out["start"].notna().sum())
    out = out.dropna(subset=["start"]).drop_duplicates(["kind", "meter_id", "start"])
    out.attrs["n_readable"] = n_ok
    return out


# ----------------------------------------------------------------------------- matching
def _ranked_counts(x: pd.Series) -> pd.Series:
    """value_counts with a deterministic tie-break (by value), so results never depend on row order."""
    return x.value_counts().sort_index().sort_values(ascending=False, kind="mergesort")


def _ns(col: pd.Series) -> np.ndarray:
    """Datetime column -> int64 nanoseconds (NaT -> int64 min), independent of pandas' time unit."""
    return col.values.astype("datetime64[ns]").astype("int64")


def match_events(cons_ev: pd.DataFrame, src_ev: pd.DataFrame, window: int,
                 chunk_pairs: int = 3_000_000) -> pd.DataFrame:
    """All (consumer event, source event) pairs whose start AND restore are within `window` minutes.
    Sorted-array search in chunks, so memory stays bounded even when thousands of meters
    go off in the same minute (substation trips)."""
    w = np.int64(window) * 60_000_000_000
    NAT = np.iinfo(np.int64).min
    c = cons_ev[["cid", "cev", "start", "restore"]].sort_values("start").reset_index(drop=True)
    cs = _ns(c["start"])
    cr = _ns(c["restore"])
    s = src_ev[["sid", "sev", "start", "restore"]].reset_index(drop=True)
    ss = _ns(s["start"])
    sr = _ns(s["restore"])
    lo = np.searchsorted(cs, ss - w, side="left")
    hi = np.searchsorted(cs, ss + w, side="right")
    cnt = hi - lo
    out_c, out_s = [], []
    i = 0
    n = len(s)
    while i < n:
        # grow chunk until it would produce too many candidate pairs
        j, tot = i, 0
        while j < n and (tot + cnt[j] <= chunk_pairs or j == i):
            tot += cnt[j]
            j += 1
        idx_s = np.repeat(np.arange(i, j), cnt[i:j])
        if len(idx_s):
            offs = np.arange(len(idx_s)) - np.repeat(np.cumsum(cnt[i:j]) - cnt[i:j], cnt[i:j])
            idx_c = lo[idx_s] + offs
            rc, rs = cr[idx_c], sr[idx_s]
            both = (rc != NAT) & (rs != NAT)
            ok = ~both | (np.abs(rc - rs) <= w)
            out_c.append(idx_c[ok])
            out_s.append(idx_s[ok])
        i = j
    if not out_c:
        return pd.DataFrame(columns=["cid", "cev", "sid", "sev"])
    ic, is_ = np.concatenate(out_c), np.concatenate(out_s)
    return pd.DataFrame({"cid": c["cid"].values[ic], "cev": c["cev"].values[ic],
                         "sid": s["sid"].values[is_], "sev": s["sev"].values[is_]})


def match_counts(cons_ev: pd.DataFrame, src_ev: pd.DataFrame, window: int,
                 chunk_pairs: int = 2_000_000) -> pd.DataFrame:
    """Like match_events, but returns only m = number of distinct source events each
    (consumer, source) pair shares. Aggregates chunk by chunk to keep memory small."""
    w = np.int64(window) * 60_000_000_000
    NAT = np.iinfo(np.int64).min
    c = cons_ev[["cid", "start", "restore"]].sort_values("start").reset_index(drop=True)
    c_lab, c_code = np.unique(c["cid"].values.astype(str), return_inverse=True)
    cs = _ns(c["start"]); cr = _ns(c["restore"])
    s = src_ev[["sid", "start", "restore"]].reset_index(drop=True)
    s_lab, s_code = np.unique(s["sid"].values.astype(str), return_inverse=True)
    ss = _ns(s["start"]); sr = _ns(s["restore"])
    lo = np.searchsorted(cs, ss - w, side="left")
    cnt = np.searchsorted(cs, ss + w, side="right") - lo
    n_sid = np.int64(len(s_lab))
    keys, vals = [], []
    i, n = 0, len(s)
    while i < n:
        j, tot = i, 0
        while j < n and (tot + cnt[j] <= chunk_pairs or j == i):
            tot += cnt[j]; j += 1
        k = cnt[i:j]
        idx_s = np.repeat(np.arange(i, j), k)
        if len(idx_s):
            idx_c = lo[idx_s] + (np.arange(len(idx_s)) - np.repeat(np.cumsum(k) - k, k))
            rc, rs = cr[idx_c], sr[idx_s]
            ok = ~((rc != NAT) & (rs != NAT)) | (np.abs(rc - rs) <= w)
            idx_c, idx_s = idx_c[ok], idx_s[ok]
            # distinct (consumer, source event) -> then count per (consumer, source)
            ce = np.unique(c_code[idx_c].astype(np.int64) * n + idx_s)
            pair = (ce // n) * n_sid + s_code[ce % n]
            u, m = np.unique(pair, return_counts=True)
            keys.append(u); vals.append(m)
        i = j
    if not keys:
        return pd.DataFrame(columns=["cid", "sid", "m"])
    df = pd.DataFrame({"k": np.concatenate(keys), "m": np.concatenate(vals)}).groupby("k", sort=False)["m"].sum()
    k = df.index.values
    return pd.DataFrame({"cid": c_lab[k // n_sid], "sid": s_lab[k % n_sid], "m": df.values})


def score_pairs(m: pd.DataFrame, n_c: pd.Series, n_s: pd.Series) -> pd.DataFrame:
    """Jaccard and recall for every (consumer, source) pair that shares >=1 event."""
    if m.empty:
        return pd.DataFrame(columns=["cid", "sid", "m", "J", "R"])
    m = m.copy()
    nc = m["cid"].map(n_c).astype(float)
    ns = m["sid"].map(n_s).astype(float)
    m["m"] = np.minimum(m["m"], np.minimum(nc, ns))
    m["J"] = m["m"] / (nc + ns - m["m"])
    m["R"] = m["m"] / ns
    return m


# ----------------------------------------------------------------------------- virtual (peer) DTR events
def build_virtual_dtr_events(cev: pd.DataFrame, cons_map: pd.DataFrame, st: Settings) -> pd.DataFrame:
    """For DTRs, group events where many of the DT's own consumers went off together.
    This is the 'DTR has 4 consumers, 3 went off' logic, and also covers DTRs with no meter.
    Returns one row per (dt, cluster) with member consumers."""
    df = cev.merge(cons_map[["consumer_number", "rec_dt"]], on="consumer_number", how="inner")
    df = df.dropna(subset=["rec_dt"]).sort_values(["rec_dt", "start", "restore", "consumer_number"], kind="mergesort")
    reporting = df.groupby("rec_dt")["consumer_number"].nunique()
    rows = []
    w = pd.Timedelta(minutes=st.window_min)
    for dt, grp in df.groupby("rec_dt", sort=False):
        n_rep = reporting[dt]
        if n_rep < st.virtual_min_consumers + 1:
            continue
        need = max(st.virtual_min_consumers, int(np.ceil(st.virtual_min_share * n_rep)))
        starts, rests, ids = grp["start"].values, grp["restore"].values, grp["consumer_number"].values
        used = np.zeros(len(grp), bool)
        for i in range(len(grp)):
            if used[i]:
                continue
            j_end = np.searchsorted(starts, starts[i] + w.to_timedelta64(), side="right")
            cand = np.arange(i, j_end)
            cand = cand[~used[cand]]
            if not pd.isna(rests[i]):
                ok = pd.isna(rests[cand]) | (np.abs(rests[cand] - rests[i]) <= w.to_timedelta64())
                cand = cand[ok]
            members = set(ids[cand])
            used[cand] = True
            if len(members) >= need:
                rows.append({"dt": dt, "start": pd.Timestamp(int(np.median(starts[cand].astype("datetime64[ns]").astype("int64")))),
                             "restore": pd.Timestamp(rests[i]) if not pd.isna(rests[i]) else pd.NaT,
                             "members": members, "need": need})
    v = pd.DataFrame(rows)
    return v


# ----------------------------------------------------------------------------- main analysis
def analyse(cons_events: pd.DataFrame, dtr_events: pd.DataFrame, fdr_events: pd.DataFrame,
            cons_map: pd.DataFrame, dtr_map: pd.DataFrame, fdr_map: pd.DataFrame,
            st: Settings = Settings(), log=print) -> dict:
    W = st.window_min

    # ---------- consumer events + data quality
    cev = cons_events.copy()
    cev["consumer_number"] = cev["consumer_number"].astype(str)
    per_day = cev.groupby(["consumer_number", cev["start"].dt.date]).size()
    per_c = per_day.groupby(level=0).max()          # busiest day per consumer
    faulty = per_c[per_c > st.max_events_per_day].index
    not_in_mi = sorted(set(cev["consumer_number"]) - set(cons_map["consumer_number"]))
    cev = cev[~cev["consumer_number"].isin(faulty)].reset_index(drop=True)
    cev["cid"] = cev["consumer_number"]
    cev["cev"] = np.arange(len(cev))
    n_c = cev.groupby("cid").size()
    log(f"consumer events: {len(cev):,}  consumers: {n_c.size:,}  faulty meters excluded: {len(faulty)}")

    rec = cons_map.set_index("consumer_number")
    dtr_info = dtr_map.dropna(subset=["dt_code"]).drop_duplicates("dt_code").set_index("dt_code")
    # feeder of a DT: DTR MI first, else majority of its consumers in Consumer MI
    dt_feeder = dtr_map.dropna(subset=["dt_code"]).drop_duplicates("dt_code").set_index("dt_code")["feeder_code"]
    dt_feeder_cons = cons_map.dropna(subset=["rec_dt"]).groupby("rec_dt")["rec_feeder"].agg(
        lambda s: s.mode().iat[0] if not s.mode().empty else None)
    dt_feeder = dt_feeder.combine_first(dt_feeder_cons)

    # ---------- sources: DTR meters, virtual DTR groups, feeder meters
    dev = dtr_events.merge(dtr_map[["meter_id", "dt_code"]], on="meter_id", how="left").dropna(subset=["dt_code"])
    dev = dev.assign(sid="DT:" + dev["dt_code"]).reset_index(drop=True)
    fev = fdr_events.merge(fdr_map[["meter_id", "feeder_code"]], on="meter_id", how="left").dropna(subset=["feeder_code"])
    fev = fev.assign(sid="FD:" + fev["feeder_code"]).reset_index(drop=True)
    virt = build_virtual_dtr_events(cev, cons_map, st)
    log(f"DTR meter events: {len(dev):,}  peer-group DTR events: {len(virt):,}  feeder events: {len(fev):,}")

    # metered DTRs use meter events; DTRs without meter events use peer-group events
    metered = set(dev["dt_code"])
    if not virt.empty:
        virt = virt[~virt["dt"].isin(metered)].reset_index(drop=True)
        virt["sid"] = "VD:" + virt["dt"]
    src = pd.concat([
        dev[["sid", "start", "restore"]],
        virt[["sid", "start", "restore"]] if not virt.empty else None,
        fev[["sid", "start", "restore"]],
    ], ignore_index=True)
    src["sev"] = np.arange(len(src))
    n_s = src.groupby("sid").size()

    sc = score_pairs(match_counts(cev, src, W), n_c, n_s)
    pairs = match_events(cev, src[src["sid"].str.startswith("FD:")], W)  # only feeder pairs kept (diagnostics)

    # leave-one-out correction for a consumer's own peer group (it helped create those events)
    if not virt.empty:
        virt_idx = src[src["sid"].str.startswith("VD:")].reset_index(drop=True)
        virt = virt.assign(sev=virt_idx["sev"].values)
        own = []
        for _, v in virt.iterrows():
            for cnum in v["members"]:
                own.append((cnum, v["sid"], len(v["members"]) - 1 >= v["need"]))
        own = pd.DataFrame(own, columns=["cid", "sid", "valid"])
        loo = own.groupby(["cid", "sid"])["valid"].agg(m="sum")
        # n of events still valid for the consumer = events where it's absent (all valid) + valid own ones
        own_cnt = own.groupby(["cid", "sid"]).size().rename("own_total")
        loo = loo.join(own_cnt).reset_index()
        loo["n_s_loo"] = loo["sid"].map(n_s) - (loo["own_total"] - loo["m"])
        sc = sc.merge(loo[["cid", "sid", "m", "n_s_loo"]], on=["cid", "sid"], how="left", suffixes=("", "_loo"))
        fix = sc["m_loo"].notna()
        sc.loc[fix, "m"] = sc.loc[fix, "m_loo"]
        nc = sc["cid"].map(n_c).astype(float)
        ns = np.where(fix, sc["n_s_loo"], sc["sid"].map(n_s)).astype(float)
        sc["J"] = np.where(ns > 0, sc["m"] / (nc + ns - sc["m"]), 0.0)
        sc["R"] = np.where(ns > 0, sc["m"] / ns, 0.0)
        loo_n = {(a, b): int(c) for a, b, c in loo[["cid", "sid", "n_s_loo"]].itertuples(index=False)}
        sc = sc.drop(columns=["m_loo", "n_s_loo"])
    else:
        loo_n = {}
    rec_keys = pd.concat([
        pd.DataFrame({"cid": cons_map["consumer_number"],
                      "sid": np.where(cons_map["rec_dt"].isin(metered), "DT:", "VD:") + cons_map["rec_dt"].astype(str)}),
        pd.DataFrame({"cid": cons_map["consumer_number"], "sid": "FD:" + cons_map["rec_feeder"].astype(str)})])
    rec_keys["is_rec"] = True
    sc = sc.merge(rec_keys.drop_duplicates(["cid", "sid"]), on=["cid", "sid"], how="left")
    sc = sc[(sc["m"] >= st.min_alt_match) | sc["is_rec"].fillna(False).astype(bool)].drop(columns="is_rec").reset_index(drop=True)
    sc["level"] = np.where(sc["sid"].str.startswith("FD:"), "feeder", "dtr")
    sc["code"] = sc["sid"].str[3:]

    # ---------- per-consumer decision (vectorised)
    consumers = n_c.index.intersection(rec.index)
    res = rec.loc[consumers, ["meter_id", "rec_feeder", "rec_dt"]].reset_index().rename(
        columns={"index": "consumer_number"})
    res["events"] = res["consumer_number"].map(n_c).astype(int)
    res["cid"] = res["consumer_number"]
    fd_of = dt_feeder
    for level in ("dtr", "feeder"):
        rec_code = res["rec_dt" if level == "dtr" else "rec_feeder"].astype(str)
        if level == "dtr":
            rec_sid = np.where(res["rec_dt"].isin(metered), "DT:", "VD:") + rec_code
        else:
            rec_sid = "FD:" + rec_code
        key = pd.Series(list(zip(res["cid"], rec_sid)))
        rec_n = key.map(loo_n).fillna(pd.Series(rec_sid).map(n_s)).fillna(0).astype(int).values
        scl = sc[sc["level"] == level]
        rr = pd.DataFrame({"cid": res["cid"], "sid": rec_sid}).merge(
            scl[["cid", "sid", "m", "J", "R"]], on=["cid", "sid"], how="left").fillna({"m": 0, "J": 0.0, "R": 0.0})
        alts = scl[scl["m"] >= st.min_alt_match].merge(
            pd.DataFrame({"cid": res["cid"], "rec_code": rec_code}), on="cid")
        alts = alts[alts["code"] != alts["rec_code"]].sort_values(
            ["cid", "J", "R", "m", "code"], ascending=[True, False, False, False, True], kind="mergesort")
        alts["bestJ"] = alts.groupby("cid")["J"].transform("max")
        tied = alts[alts["J"] >= alts["bestJ"] - st.tie_gap]
        best = alts.drop_duplicates("cid").set_index("cid")
        n_tie = tied.groupby("cid").size()
        cands = tied.groupby("cid")["code"].agg(lambda x: ", ".join(list(x)[:5]) + (f" (+{len(x)-5} more)" if len(x) > 5 else ""))
        cands_all = tied.groupby("cid")["code"].agg(lambda x: "|".join(x))
        if level == "dtr":   # which feeder do the tied DTR candidates belong to?
            tf = tied.assign(fd=tied["code"].map(fd_of)).dropna(subset=["fd"])
            vote = tf.groupby("cid")["fd"].agg(lambda x: _ranked_counts(x).index[0])
            vshare = tf.groupby("cid")["fd"].agg(lambda x: _ranked_counts(x).iat[0] / len(x))
        b = best.reindex(res["cid"])
        evaluable = (res["events"].values >= st.min_events) & (rec_n >= st.min_events)
        strong = (b["R"].values >= st.min_alt_recall) & ((b["J"].values - rr["J"].values) >= st.min_margin)
        strong = np.nan_to_num(strong, nan=False).astype(bool)
        wrong_e = strong & evaluable & (rr["R"].values <= st.max_rec_recall)
        wrong_ne = strong & ~evaluable
        if level == "dtr":   # without recorded evidence, only trust a move to another feeder
            sug_fd = b["code"].map(fd_of)
            other_fd = (sug_fd.notna() & (sug_fd.values != res["rec_feeder"].values)).values
            wrong_ne &= other_fd
        status = np.where(wrong_e, "WRONG", np.where(wrong_ne, "LIKELY WRONG (recorded source had no usable outages)",
                          np.where(evaluable, "OK", "NOT EVALUABLE")))
        nt = n_tie.reindex(res["cid"]).fillna(0).values
        flagged = wrong_e | wrong_ne
        margin = b["J"].values - rr["J"].values
        conf = np.where(wrong_ne, "Low", np.where((b["m"].values >= 3) & (margin >= 0.5), "High", "Medium"))
        res[f"{level}_status"] = status
        res[f"{level}_suggested"] = np.where(flagged & (nt == 1), b["code"].values, None)
        res[f"{level}_candidates"] = np.where(flagged, cands.reindex(res["cid"]).values, None)
        res[f"{level}_n_candidates"] = np.where(flagged, nt, 0).astype(int)
        res[f"_{level}_cands_all"] = np.where(flagged, cands_all.reindex(res["cid"]).values, None)
        if level == "dtr":
            res["_vote_feeder"] = vote.reindex(res["cid"]).values
            res["_vote_share"] = vshare.reindex(res["cid"]).values
        res[f"{level}_confidence"] = np.where(flagged, conf, None)
        res[f"{level}_rec_score"] = rr["J"].round(3).values
        res[f"{level}_rec_followed"] = rr["m"].astype(int).astype(str).values + "/" + rec_n.astype(str)
        res[f"{level}_best_score"] = b["J"].round(3).values
        bf = b["m"].fillna(0).astype(int).astype(str) + "/" + b["sid"].map(n_s).fillna(0).astype(int).astype(str)
        res[f"{level}_best_followed"] = np.where(b["sid"].notna().values, bf.values, None)
    res = res.drop(columns="cid")

    # ---------- group-level check: if most of a source's consumers disagree, suspect the source itself
    suspects = []
    for level, col in (("dtr", "rec_dt"), ("feeder", "rec_feeder")):
        ev = res[res[f"{level}_status"].isin(["OK", "WRONG"])]
        share = ev.groupby(col)[f"{level}_status"].agg(lambda s: (s == "WRONG").mean())
        n = ev.groupby(col).size()
        bad = share[(share >= st.group_issue_share) & (n >= 3)]
        for code, sh in bad.items():
            suspects.append({"level": level.upper(), "code": code, "consumers_evaluated": int(n[code]),
                             "share_disagreeing": round(float(sh), 3),
                             "interpretation": "Most consumers do not follow this source's outages: the "
                                               "source meter is probably mapped to the wrong "
                                               f"{'DTR' if level == 'dtr' else 'feeder'}, or its events are unreliable. "
                                               "Check the meter before moving consumers."})
            m = (res[col] == code) & (res[f"{level}_status"] == "WRONG")
            res.loc[m, f"{level}_status"] = "GROUP ISSUE (check source meter)"
    suspects = pd.DataFrame(suspects)

    # final suggested mapping per consumer
    def final(r):
        dt, fd = r["rec_dt"], r["rec_feeder"]
        reason = []
        if str(r["dtr_status"]).startswith(("WRONG", "LIKELY")):
            if pd.notna(r["dtr_suggested"]):
                dt = r["dtr_suggested"]
                fd = dt_feeder.get(dt, fd)
                reason.append("follows another DTR's outages")
            else:
                dt = f"AMBIGUOUS ({r['dtr_n_candidates']}): " + str(r["dtr_candidates"])
                if pd.notna(r["_vote_feeder"]) and r["_vote_share"] >= 0.6:
                    fd = r["_vote_feeder"]
                    reason.append(f"follows {r['dtr_n_candidates']} DTRs equally, {r['_vote_share']:.0%} of them on "
                                  f"feeder {fd}; feeder identified, exact DT needs a field check")
                else:
                    reason.append("follows several DTRs equally; DT and feeder need a field check")
        if str(r["feeder_status"]).startswith(("WRONG", "LIKELY")) and pd.notna(r["feeder_suggested"]):
            if fd == r["rec_feeder"] or fd != r["feeder_suggested"]:
                fd = r["feeder_suggested"]
                reason.append("follows another feeder's outages")
        return pd.Series({"suggested_dt": dt, "suggested_feeder": fd, "reason": "; ".join(reason) or None})
    res = pd.concat([res, res.apply(final, axis=1)], axis=1)

    wrong = res[res["dtr_status"].str.startswith(("WRONG", "LIKELY")) |
                res["feeder_status"].str.startswith(("WRONG", "LIKELY"))].copy()
    conf_rank = {"High": 0, "Medium": 1, "Low": 2}
    wrong["_r"] = wrong[["dtr_confidence", "feeder_confidence"]].apply(
        lambda x: min(conf_rank.get(v, 3) for v in x), axis=1)
    wrong["confidence"] = wrong["_r"].map({0: "High", 1: "Medium", 2: "Low"})
    wrong = wrong.sort_values(["_r", "rec_feeder", "rec_dt"]).drop(columns="_r")

    # ---------- patterns: many consumers moving the same way usually means a record-level error
    pat = wrong[wrong["suggested_feeder"].notna() & (wrong["suggested_feeder"] != wrong["rec_feeder"])]
    patterns = (pat.groupby(["rec_feeder", "suggested_feeder"]).agg(
        consumers=("consumer_number", "size"), recorded_dts=("rec_dt", "nunique"),
        high_conf=("confidence", lambda x: int((x == "High").sum())))
        .reset_index().sort_values("consumers", ascending=False))

    # ---------- DTR -> feeder check (DTR meter events vs feeder meter events)
    dsrc = dev[["dt_code", "start", "restore"]].rename(columns={"dt_code": "cid"}).reset_index(drop=True)
    dsrc["cev"] = np.arange(len(dsrc))
    fsrc = fev[["sid", "start", "restore"]].reset_index(drop=True)
    fsrc["sev"] = np.arange(len(fsrc))
    dsc = score_pairs(match_counts(dsrc, fsrc, W), dsrc.groupby("cid").size(), fsrc.groupby("sid").size())
    dtr_rows = []
    n_d = dsrc.groupby("cid").size()
    n_f = fsrc.groupby("sid").size()
    for dt, n_dt in n_d.items():
        rec_fd = dt_feeder.get(dt)
        g = dsc[dsc["cid"] == dt]
        rr = g[g["sid"] == f"FD:{rec_fd}"]
        rec_J = float(rr["J"].iat[0]) if len(rr) else 0.0
        rec_R = float(rr["R"].iat[0]) if len(rr) else 0.0
        rec_n = int(n_f.get(f"FD:{rec_fd}", 0))
        alts = g[(g["sid"] != f"FD:{rec_fd}") & (g["m"] >= st.min_alt_match)].sort_values(
            ["J", "R", "m", "sid"], ascending=[False, False, False, True], kind="mergesort")
        if alts.empty:
            continue
        b = alts.iloc[0]
        if b["R"] >= st.min_alt_recall and b["J"] - rec_J >= st.min_margin and rec_R <= st.max_rec_recall:
            dtr_rows.append({"dt_code": dt, "dt_name": dtr_info["dt_name"].get(dt) if dt in dtr_info.index else None,
                             "recorded_feeder": rec_fd, "suggested_feeder": b["sid"][3:],
                             "recorded_feeder_outages_followed": f"{int(rr['m'].iat[0]) if len(rr) else 0}/{rec_n}",
                             "suggested_feeder_outages_followed": f"{int(b['m'])}/{int(n_f[b['sid']])}",
                             "score_recorded": round(rec_J, 3), "score_suggested": round(float(b["J"]), 3),
                             "confidence": "High" if rec_n >= st.min_events and b["m"] >= 2 else "Low",
                             "note": None if rec_n >= st.min_events else
                             "recorded feeder had <2 outages; evidence is one-sided"})
    dtr_wrong = pd.DataFrame(dtr_rows)

    # ---------- feeder event diagnostic: did the feeder's own consumers go off?
    cons_on_fd = cons_map.groupby("rec_feeder")["consumer_number"].apply(set)
    reporting = set(n_c.index)
    # (simple, vectorised version)
    fe = src[src["sid"].str.startswith("FD:")].copy()
    hits = pairs.merge(fe[["sev", "sid"]], on=["sev", "sid"])
    hits = hits.merge(cons_map[["consumer_number", "rec_feeder"]], left_on="cid", right_on="consumer_number", how="left")
    diag = []
    for _, e in fe.iterrows():
        code = e["sid"][3:]
        h = hits[hits["sev"] == e["sev"]]
        exp_n = len(cons_on_fd.get(code, set()) & reporting)
        own = int((h["rec_feeder"] == code).sum())
        other = _ranked_counts(h[h["rec_feeder"] != code]["rec_feeder"])
        diag.append({"feeder_code": code, "outage_start": e["start"], "outage_restore": e["restore"],
                     "own_consumers_reporting": exp_n, "own_consumers_followed": own,
                     "share_followed": round(own / exp_n, 3) if exp_n else None,
                     "other_consumers_followed": int(other.sum()),
                     "top_other_feeder": other.index[0] if len(other) else None,
                     "top_other_count": int(other.iat[0]) if len(other) else None})
    feeder_diag = pd.DataFrame(diag).sort_values("share_followed")

    # ---------- feeder meters whose outages are followed by ANOTHER feeder's consumers
    fdg = feeder_diag.groupby("feeder_code").agg(own=("own_consumers_followed", "sum"),
                                                  own_rep=("own_consumers_reporting", "max"),
                                                  share=("share_followed", "mean"),
                                                  other=("top_other_feeder", lambda x: x.mode().iat[0] if x.notna().any() else None),
                                                  other_n=("top_other_count", "sum"))
    moved = fdg[(fdg["share"].fillna(0) < 0.2) & (fdg["other_n"].fillna(0) > fdg["own"] * 3) &
                (fdg["other_n"].fillna(0) >= 10) & fdg["other"].notna()]
    extra = []
    for code, row in moved.iterrows():
        extra.append({"level": "FEEDER", "code": code, "consumers_evaluated": int(row["own_rep"]),
                      "share_disagreeing": round(1 - float(row["share"] or 0), 3),
                      "interpretation": f"This feeder meter's outages are followed by consumers recorded on feeder "
                                        f"{row['other']}, not by its own consumers. The meter is probably installed on "
                                        f"{row['other']} (or the two feeder codes are swapped). Check before moving consumers."})
        m = (res["rec_feeder"] == row["other"]) & (res["suggested_feeder"] == code)
        res.loc[m, "feeder_status"] = "GROUP ISSUE (check source meter)"
        res.loc[m, "reason"] = f"pattern explained by feeder meter {code} being on {row['other']}"
        res.loc[m, "suggested_feeder"] = res.loc[m, "rec_feeder"]
    if extra:
        suspects = pd.concat([suspects, pd.DataFrame(extra)], ignore_index=True).drop_duplicates(["level", "code"], keep="last")
        keep = res.set_index("consumer_number")
        wrong = wrong[wrong["consumer_number"].map(
            lambda c: str(keep.at[c, "dtr_status"]).startswith(("WRONG", "LIKELY")) or
                      str(keep.at[c, "feeder_status"]).startswith(("WRONG", "LIKELY")))].copy()
        for col in ("feeder_status", "reason", "suggested_feeder"):
            wrong[col] = wrong["consumer_number"].map(keep[col])
        pat = wrong[wrong["suggested_feeder"].notna() & (wrong["suggested_feeder"] != wrong["rec_feeder"])]
        patterns = (pat.groupby(["rec_feeder", "suggested_feeder"]).agg(
            consumers=("consumer_number", "size"), recorded_dts=("rec_dt", "nunique"),
            high_conf=("confidence", lambda x: int((x == "High").sum())))
            .reset_index().sort_values("consumers", ascending=False))

    # ---------- DTR -> feeder verdict for EVERY DTR (meter evidence + consumer evidence)
    moved_map = {code: row["other"] for code, row in moved.iterrows()}
    dtr_check = dtr_feeder_check(cons_map, dtr_map, res, dsc, n_d, n_f, dt_feeder, set(n_c.index), dtr_wrong, st,
                                 moved_map)

    summary = {
        "consumers_with_events": int(n_c.size), "faulty_meters_excluded": int(len(faulty)),
        "consumers_not_in_MI": len(not_in_mi),
        "dtr_evaluable": int(res["dtr_status"].isin(["OK", "WRONG"]).sum() +
                             res["dtr_status"].str.startswith("GROUP").sum()),
        "feeder_evaluable": int(res["feeder_status"].isin(["OK", "WRONG"]).sum() +
                                res["feeder_status"].str.startswith("GROUP").sum()),
        "flagged_consumers": int(len(wrong)),
        "flagged_high": int((wrong["confidence"] == "High").sum()),
        "flagged_medium": int((wrong["confidence"] == "Medium").sum()),
        "flagged_low": int((wrong["confidence"] == "Low").sum()),
        "group_issues": int(len(suspects)),
        "dtr_feeder_flags": int(len(dtr_wrong)),
        "dtr_total": int(len(dtr_check)),
        "dtr_correct": int((dtr_check["verdict"] == "CORRECT").sum()),
        "dtr_wrong": int(dtr_check["verdict"].str.startswith("WRONG").sum()),
        "dtr_conflict": int((dtr_check["verdict"] == "CONFLICT").sum()),
        "dtr_unclear": int((dtr_check["verdict"] == "UNCLEAR").sum()),
        "dtr_meter_issue": int((dtr_check["verdict"] == "FEEDER METER ISSUE").sum()),
        "dtr_not_checkable": int((dtr_check["verdict"] == "NOT CHECKABLE").sum()),
    }
    return {"results": res, "wrong": wrong, "patterns": patterns, "dtr_check": dtr_check, "suspects": suspects, "dtr_wrong": dtr_wrong,
            "feeder_diag": feeder_diag, "faulty": pd.DataFrame({"consumer_number": faulty,
                                                                "max_events_in_a_day": per_c[faulty].values}),
            "not_in_mi": pd.DataFrame({"consumer_number": not_in_mi}), "summary": summary,
            "settings": st}


def dtr_feeder_check(cons_map, dtr_map, res, dsc, n_d, n_f, dt_feeder, reporting, dtr_wrong, st, moved_map=None):
    """Is each DTR on the feeder the records say? Two independent kinds of evidence:
       meter:     the DTR meter's outages vs the feeder meter's outages
       consumers: the DTR's consumers' outages vs feeder outages (feeder-level checks + feeder suggestions)"""
    circ = "Circle Name"
    if circ in cons_map.columns:
        rep_c = cons_map.loc[cons_map["consumer_number"].isin(reporting), circ].value_counts(normalize=True)
        circles = set(rep_c[rep_c >= 0.05].index)
        dts = set(cons_map.loc[cons_map[circ].isin(circles), "rec_dt"].dropna())
        if circ in dtr_map.columns:
            dts |= set(dtr_map.loc[dtr_map[circ].isin(circles), "dt_code"].dropna())
    else:   # no circle column: judge the DTRs of the consumers that appear in the outage data
        dts = set(cons_map.loc[cons_map["consumer_number"].isin(reporting), "rec_dt"].dropna())
    dts |= set(n_d.index)
    dmi = dtr_map.dropna(subset=["dt_code"]).drop_duplicates("dt_code").set_index("dt_code")
    cmaj = cons_map.dropna(subset=["rec_dt"]).groupby("rec_dt")["rec_feeder"].agg(
        lambda x: x.mode().iat[0] if not x.mode().empty else None)
    ncons = cons_map.dropna(subset=["rec_dt"]).groupby("rec_dt").size()
    flagged_dt = dtr_wrong.set_index("dt_code") if len(dtr_wrong) else None

    # consumer evidence: a consumer "votes" for a feeder only when its feeder was actually tested
    r = res.copy()
    conf = r[["dtr_confidence", "feeder_confidence"]].apply(
        lambda x: "High" if "High" in x.values else "Medium" if "Medium" in x.values else
        "Low" if "Low" in x.values else None, axis=1)
    moved = (r["suggested_feeder"].notna() & (r["suggested_feeder"] != r["rec_feeder"]) & conf.isin(["High", "Medium"]))
    confirmed = (r["feeder_status"] == "OK") & ~moved
    r["vote"] = np.where(moved, r["suggested_feeder"], np.where(confirmed, r["rec_feeder"], None))
    votes = r.dropna(subset=["vote"])

    rows = []
    for dt in sorted(dts):
        rec_fd = dt_feeder.get(dt)
        mi_fd = dmi["feeder_code"].get(dt) if dt in dmi.index else None
        c_fd = cmaj.get(dt)
        # --- meter evidence
        sid = f"FD:{rec_fd}"
        rec_n = int(n_f.get(sid, 0))
        if dt not in n_d.index:
            m_stat, m_txt = "NOT CHECKABLE", ("no DTR meter" if mi_fd is None and dt not in dmi.index
                                              else "DTR meter reported no outage")
        elif flagged_dt is not None and dt in flagged_dt.index:
            f = flagged_dt.loc[dt]
            m_stat = "WRONG" if rec_n >= st.min_events else "LIKELY WRONG"
            m_txt = (f"followed {f['suggested_feeder_outages_followed']} outages of {f['suggested_feeder']}, "
                     f"{f['recorded_feeder_outages_followed']} of recorded")
        elif rec_n < st.min_events:
            m_stat, m_txt = "NOT CHECKABLE", "recorded feeder had <2 feeder-meter outages"
        else:
            g = dsc[(dsc["cid"] == dt) & (dsc["sid"] == sid)]
            m = int(g["m"].iat[0]) if len(g) else 0
            R = m / rec_n
            m_stat = "CORRECT" if R >= st.min_alt_recall else "UNCLEAR"
            m_txt = f"followed {m}/{rec_n} outages of recorded feeder"
        m_sug = flagged_dt.loc[dt, "suggested_feeder"] if (m_stat.endswith("WRONG")) else None
        # --- consumer evidence
        v = votes[votes["rec_dt"] == dt]
        n_v = len(v)
        if n_v < 3:
            c_stat, c_sug, c_txt = "NOT CHECKABLE", None, f"{n_v} consumers with a feeder-level test"
        else:
            share_rec = (v["vote"] == rec_fd).mean()
            vc = v.loc[v["vote"] != rec_fd, "vote"].value_counts()
            top, top_share = (vc.index[0], vc.iat[0] / n_v) if len(vc) else (None, 0.0)
            if share_rec >= 0.6:
                c_stat, c_sug = "CORRECT", None
            elif top_share >= 0.6:
                c_stat, c_sug = "WRONG", top
            else:
                c_stat, c_sug = "UNCLEAR", None
            c_txt = f"{int(share_rec * n_v)}/{n_v} consumers confirm recorded feeder" + (
                f", {int(round(top_share * n_v))} point to {top}" if top else "")
        # --- verdict
        stats = {m_stat.replace("LIKELY ", ""), c_stat}
        if "WRONG" in stats and "CORRECT" in stats:
            verdict, sug = "CONFLICT", None
        elif "WRONG" in stats:
            sug = m_sug or c_sug
            both = m_stat.endswith("WRONG") and c_stat == "WRONG"
            verdict = "WRONG (both checks agree)" if both and (m_sug == c_sug) else \
                      "WRONG (meter check)" if m_stat.endswith("WRONG") else "WRONG (consumer check)"
        elif "CORRECT" in stats:
            verdict, sug = "CORRECT", None
        elif "UNCLEAR" in stats:
            verdict, sug = "UNCLEAR", None
        else:
            verdict, sug = "NOT CHECKABLE", None
        confidence = None
        if verdict.startswith("WRONG"):
            if (moved_map or {}).get(sug) == rec_fd:   # explained by a feeder meter sitting on rec_fd
                verdict, sug = "FEEDER METER ISSUE", None
            else:
                confidence = ("High" if verdict.endswith("agree)") or m_stat == "WRONG" else
                              "Medium" if c_stat == "WRONG" else "Low")
        rows.append({"dt_code": dt, "dt_name": dmi["dt_name"].get(dt) if dt in dmi.index else None,
                     "recorded_feeder": rec_fd, "verdict": verdict, "confidence": confidence,
                     "suggested_feeder": sug,
                     "meter_check": m_stat, "meter_detail": m_txt,
                     "consumer_check": c_stat, "consumer_detail": c_txt,
                     "consumers_in_MI": int(ncons.get(dt, 0)),
                     "feeder_in_DTR_MI": mi_fd, "feeder_in_Consumer_MI": c_fd,
                     "records_disagree": bool(mi_fd and c_fd and mi_fd != c_fd)})
    out = pd.DataFrame(rows)
    order = {"WRONG (both checks agree)": 0, "WRONG (meter check)": 1, "WRONG (consumer check)": 2,
             "CONFLICT": 3, "FEEDER METER ISSUE": 4, "UNCLEAR": 5, "CORRECT": 6, "NOT CHECKABLE": 7}
    return out.sort_values(["verdict", "recorded_feeder"], key=lambda c: c.map(order) if c.name == "verdict" else c)


def data_checks(cm, dm, fm, ev, raw_rows: dict) -> pd.DataFrame:
    """Plain-language checks on whatever data was uploaded, so problems are visible instead of silent."""
    rows = []
    add = lambda area, item, value, status="OK", note="": rows.append(
        {"Area": area, "Check": item, "Value": value, "Status": status, "What it means": note})
    pct = lambda a, b: (a / b if b else 0.0)
    for col, lab in (("rec_feeder", "feeder code"), ("rec_dt", "DT code")):
        p = pct(cm[col].notna().sum(), len(cm))
        add("MI master", f"Consumers with a {lab}", f"{p:.1%}", "OK" if p > 0.9 else "WARNING",
            "" if p > 0.9 else f"Consumers without a {lab} cannot be placed on the network.")
    add("MI master", "Consumers / DTRs / feeders", f"{len(cm):,} / {dm['dt_code'].nunique():,} / {fm['feeder_code'].nunique():,}")
    for kind in ("consumer", "dtr", "feeder"):
        e, raw = ev[kind], raw_rows.get(kind, 0)
        if raw == 0:
            add(f"{kind} outages", "File provided", "no", "ERROR" if kind in ("consumer", "feeder") else "WARNING",
                "Required for the breakdown check." if kind != "dtr" else "Without DTR outages, Feeder->DTR cannot be checked.")
            continue
        ok_n = raw_rows.get(kind + "_ok", len(e))
        p = pct(ok_n, raw)
        add(f"{kind} outages", "Rows with a readable off-time", f"{ok_n:,} of {raw:,} ({p:.1%})",
            "OK" if p > 0.95 else "WARNING", "" if p > 0.95 else "Some dates could not be read; those rows are ignored. "
            "Check the date format (expected day-month-year).")
        if len(e):
            dup = ok_n - len(e)
            if dup > 0:
                add(f"{kind} outages", "Duplicate rows removed", f"{dup:,}", "OK",
                    "Same meter and same off-time listed more than once; counted once.")
            pr = e["restore"].notna().mean()
            add(f"{kind} outages", "Rows with an on-time (restore)", f"{pr:.1%}", "OK" if pr > 0.9 else "WARNING",
                "" if pr > 0.9 else "Outages without restore time are matched on off-time only.")
            days = e["start"].dt.date
            add(f"{kind} outages", "Period covered", f"{days.min()} to {days.max()} ({days.nunique()} day(s))")
        # coverage against the MI file
        if kind == "consumer" and len(e):
            p = e["consumer_number"].drop_duplicates().isin(cm["consumer_number"]).mean()
            note = "Outage file and MI file may be from different circles." if p < 0.8 else ""
            add("Coverage", "Consumers in outage file found in MI", f"{p:.1%}", "OK" if p >= 0.8 else "WARNING", note)
        if kind in ("dtr", "feeder") and len(e):
            ref = dm if kind == "dtr" else fm
            p = e["meter_id"].drop_duplicates().isin(ref["meter_id"]).mean()
            add("Coverage", f"{kind.upper()} meters in outage file found in MI", f"{p:.1%}",
                "OK" if p >= 0.8 else "WARNING", "" if p >= 0.8 else "Unmatched meters are ignored; check the MI file.")
    if len(ev["consumer"]) and len(ev["feeder"]):
        a, b = ev["consumer"]["start"].dt.date, ev["feeder"]["start"].dt.date
        overlap = set(a) & set(b)
        add("Coverage", "Days present in BOTH consumer and feeder files", len(overlap),
            "OK" if overlap else "ERROR", "" if overlap else "The files are from different dates; nothing can be matched.")
    return pd.DataFrame(rows)
