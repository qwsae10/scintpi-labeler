"""Audit the event index before (re)building it: ``scintlab audit-events``.

Three things, written to one folder (nothing the app reads is changed unless ``install`` is set):

1. The index rebuilt with events grouped across UTC midnight (``events.build_events``), compared
   with the installed one: ``id_map.csv`` says for every installed event whether it keeps its id or
   becomes part of a merged event, and ``labels_affected.csv`` lists the labels on events whose id
   changes (their label lines stay in labels/*.jsonl; nothing is deleted).
2. Per event, from the 1 s data (``quality.parquet``, keyed by event_id of the rebuilt index):

   frac_snr1, frac_snr2, frac_tec   share of the event's seconds with L1 C/N0, channel-2 C/N0, and
                                    TEC (|TEC| < 1000 TECU) of this satellite
   max_gap_s                        longest stretch of the event without L1 C/N0
   ends_in_loss_of_lock             the satellite's data stops within 2 min of the event's end and
                                    stays away for 2 min, while >= 3 other satellites are tracked
                                    and it is not setting (elevation above the floor + 3 deg)
   starts_after_gap                 the same at the start: the data begins after >= 2 min without it
3. ``report.txt``: how many events each candidate rule would hide, and what labels the hidden ones
   already have, so the thresholds can be chosen from numbers.
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from scintlab import config
from scintlab.events import build_events, install_events

logger = logging.getLogger(__name__)

DAY_S = 86400
LOOK_S = 120          # a loss of lock / gap must last at least this long
NEAR_S = 120          # ... and happen within this long of the event's edge
MIN_OTHERS = 3        # other satellites tracked meanwhile (else the receiver stopped, not the satellite)
SETTING_MARGIN_DEG = 3.0
TEC_LIMIT_TECU = 1000.0
COLUMNS = ["datetime", "prn", "elev", "snr1", "snr2", "tec_cph12"]


# --------------------------------------------------------------------------- 1 s data, one receiver

class _Seconds:
    """1 s data of one receiver, a few days at a time, as numpy arrays per satellite."""

    def __init__(self, receiver: str, keep: int = 3):
        self.rx, self.keep, self.days = receiver, keep, OrderedDict()

    def _day(self, day: int):
        if day in self.days:
            self.days.move_to_end(day)
            return self.days[day]
        date = pd.Timestamp(day * DAY_S, unit="s")
        path = config.products_dir() / self.rx / f"{date:%Y%m%d}" / "second.parquet"
        out = None
        if path.exists():
            cols = [c for c in COLUMNS if c in pq.ParquetFile(path).schema_arrow.names]
            df = pd.read_parquet(path, columns=cols)
            t = (pd.to_datetime(df["datetime"]).astype("int64") // 10**9).to_numpy()
            ok = {k: df[c].notna().to_numpy() if c in df else np.zeros(len(df), bool)
                  for k, c in (("snr1", "snr1"), ("snr2", "snr2"), ("tec", "tec_cph12"))}
            if "tec_cph12" in df:
                ok["tec"] &= df["tec_cph12"].abs().lt(TEC_LIMIT_TECU).to_numpy()
            elev = df["elev"].to_numpy(dtype=float) if "elev" in df else np.full(len(df), np.nan)
            prn = df["prn"].astype(str).to_numpy()
            order = np.lexsort((t, prn))
            t, prn, elev = t[order], prn[order], elev[order]
            ok = {k: v[order] for k, v in ok.items()}
            names, first = np.unique(prn, return_index=True)
            bounds = list(first) + [len(prn)]
            by_prn = {n: slice(bounds[i], bounds[i + 1]) for i, n in enumerate(names)}
            # satellites with L1 C/N0 in each second of the day
            sod = t[ok["snr1"]] - day * DAY_S
            count = np.bincount(sod[(sod >= 0) & (sod < DAY_S)], minlength=DAY_S)
            out = {"t": t, "elev": elev, "ok": ok, "by_prn": by_prn, "count": count}
        self.days[day] = out
        while len(self.days) > self.keep:
            self.days.popitem(last=False)
        return out

    def samples(self, prn: str, lo: int, hi: int):
        """(t, elev, ok1, ok2, oktec) of ``prn`` in [lo, hi) epoch seconds, unique times."""
        parts = []
        for day in range(lo // DAY_S, (hi - 1) // DAY_S + 1):
            d = self._day(day)
            if d is None or prn not in d["by_prn"]:
                continue
            sl = d["by_prn"][prn]
            t = d["t"][sl]
            i, j = np.searchsorted(t, lo), np.searchsorted(t, hi)
            parts.append((t[i:j], d["elev"][sl][i:j], *(d["ok"][k][sl][i:j] for k in ("snr1", "snr2", "tec"))))
        if not parts:
            e = np.array([], dtype=np.int64)
            return e, np.array([]), np.array([], bool), np.array([], bool), np.array([], bool)
        t, elev, o1, o2, ot = (np.concatenate(x) for x in zip(*parts))
        t, idx = np.unique(t, return_index=True)  # a day file may repeat the previous day's last seconds
        return t, elev[idx], o1[idx], o2[idx], ot[idx]

    def tracked(self, lo: int, hi: int) -> np.ndarray:
        """Satellites with L1 C/N0 in each second of [lo, hi) (0 where no day is processed)."""
        out = np.zeros(max(0, hi - lo), dtype=np.int64)
        for day in range(lo // DAY_S, (hi - 1) // DAY_S + 1):
            d = self._day(day)
            if d is None:
                continue
            a, b = max(lo, day * DAY_S), min(hi, (day + 1) * DAY_S)
            out[a - lo:b - lo] = d["count"][a - day * DAY_S:b - day * DAY_S]
        return out


def _event_quality(sec: _Seconds, prn: str, s: int, e: int, floor_deg: float) -> dict:
    span = max(1, e - s)
    t, elev, o1, o2, ot = sec.samples(prn, s - LOOK_S - NEAR_S, e + LOOK_S + NEAR_S)
    inside = (t >= s) & (t < e)
    t1 = t[inside & o1]
    edges = np.concatenate([[s - 1], t1, [e]])
    row = {
        "frac_snr1": round(len(t1) / span, 4),
        "frac_snr2": round(int((inside & o2).sum()) / span, 4),
        "frac_tec": round(int((inside & ot).sum()) / span, 4),
        "max_gap_s": int((np.diff(edges) - 1).max()),
        "ends_in_loss_of_lock": False,
        "starts_after_gap": False,
    }
    tl = t[o1]
    el = elev[o1]
    near_end = tl[(tl >= e - NEAR_S) & (tl < e + NEAR_S)]
    if len(near_end):
        last = near_end.max()
        k = np.searchsorted(tl, last)
        quiet = not ((tl > last) & (tl <= last + LOOK_S)).any()
        others = np.median(sec.tracked(last + 1, last + 1 + LOOK_S)) if quiet else 0
        row["ends_in_loss_of_lock"] = bool(quiet and others >= MIN_OTHERS
                                           and np.nan_to_num(el[k], nan=-90) >= floor_deg + SETTING_MARGIN_DEG)
    near_start = tl[(tl >= s - NEAR_S) & (tl < s + NEAR_S)]
    if len(near_start):
        first = near_start.min()
        k = np.searchsorted(tl, first)
        quiet = not ((tl < first) & (tl >= first - LOOK_S)).any()
        others = np.median(sec.tracked(first - LOOK_S, first)) if quiet else 0
        row["starts_after_gap"] = bool(quiet and others >= MIN_OTHERS
                                       and np.nan_to_num(el[k], nan=-90) >= floor_deg + SETTING_MARGIN_DEG)
    return row


def _receiver_quality(args) -> list[dict]:
    rx, ev, floor_deg = args
    sec = _Seconds(rx)
    rows = []
    for r in ev.sort_values("start_utc").itertuples():
        s, e = int(r.start_utc.timestamp()), int(r.end_utc.timestamp())
        try:
            rows.append({"event_id": r.event_id, **_event_quality(sec, r.prn, s, e, floor_deg)})
        except Exception:
            logger.exception("quality of %s failed", r.event_id)
    logger.info("%s: %d events", rx, len(rows))
    return rows


def quality(ev: pd.DataFrame, cfg: dict, workers: int = 1) -> pd.DataFrame:
    floor_deg = float(cfg["candidates"]["min_elevation_deg"])
    jobs = [(rx, g[["event_id", "prn", "start_utc", "end_utc"]], floor_deg) for rx, g in ev.groupby("receiver")]
    if workers > 1:
        with Pool(workers) as pool:
            parts = pool.map(_receiver_quality, jobs, chunksize=1)
    else:
        parts = [_receiver_quality(j) for j in jobs]
    return pd.DataFrame([r for p in parts for r in p])


# --------------------------------------------------------------------------- old ids -> new ids

def id_map(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """For every installed event: ``same`` (id unchanged), ``merged`` (now part of a longer event,
    usually one joined over midnight), or ``gone`` (no rebuilt event covers it)."""
    rows = []
    utc = lambda df: df.assign(start_utc=pd.to_datetime(df["start_utc"], utc=True),  # noqa: E731
                               end_utc=pd.to_datetime(df["end_utc"], utc=True))
    old, new = utc(old), utc(new)
    newg = {k: g.sort_values("start_utc") for k, g in new.groupby(["receiver", "prn"])}
    new_ids = set(new["event_id"])
    for r in old.itertuples():
        if r.event_id in new_ids:
            rows.append((r.event_id, r.event_id, "same"))
            continue
        g = newg.get((r.receiver, r.prn))
        hit = None if g is None else g[(g["start_utc"] <= r.start_utc) & (g["end_utc"] >= r.end_utc)]
        if hit is None or hit.empty:
            rows.append((r.event_id, None, "gone"))
        else:
            rows.append((r.event_id, hit["event_id"].iloc[0], "merged"))
    return pd.DataFrame(rows, columns=["old_id", "new_id", "status"])


# --------------------------------------------------------------------------- report

def _labels() -> pd.DataFrame:
    from scintlab.labels import LabelStore

    d = LabelStore().all_decisions()
    if d.empty:
        return pd.DataFrame(columns=["event_id", "user", "action", "label"])
    return d


def _counts(s: pd.Series) -> str:
    return ", ".join(f"{k} {v}" for k, v in s.value_counts().items()) or "none"


def report(old: pd.DataFrame | None, new: pd.DataFrame, ids: pd.DataFrame | None, q: pd.DataFrame,
           labels: pd.DataFrame) -> str:
    L = []
    w = L.append
    w(f"Event audit, {pd.Timestamp.utcnow():%Y-%m-%d %H:%M} UTC")
    w("")
    w("== Grouping across UTC midnight ==")
    if old is not None and ids is not None:
        st = ids["status"].value_counts()
        merged_into = ids[ids["status"] == "merged"]["new_id"].nunique()
        w(f"installed events: {len(old)}   rebuilt: {len(new)}")
        w(f"  keep their id: {st.get('same', 0)}   merged into {merged_into} longer events: {st.get('merged', 0)}"
          f"   not covered by any rebuilt event: {st.get('gone', 0)}")
        changed = ids[ids["status"] != "same"]["old_id"]
        lab = labels[labels["event_id"].isin(changed)]
        w(f"  decisions on events whose id changes: {len(lab)} ({lab['event_id'].nunique()} events; "
          f"{_counts(lab['label'].fillna(lab['action']))})")
    w(f"rebuilt events running over midnight: {int(new['crosses_midnight'].sum())}")
    w(f"rebuilt events cut at the edge of the processed days (neighbour day not processed): "
      f"{int(new['cut_at_day_edge'].sum())}")
    w("")
    ev = new.merge(q, on="event_id", how="left")
    n = len(ev)
    pct = lambda k: f"{k} ({100 * k / max(n, 1):.1f}%)"  # noqa: E731
    w(f"== Per event, {n} rebuilt events ==")
    for k in (1, 2, 3):
        w(f"  candidate minutes <= {k}: {pct(int((ev['n_minutes'] <= k).sum()))}")
    for f in (0.5, 0.8, 0.9):
        w(f"  L1 1 s coverage < {f:.0%}: {pct(int((ev['frac_snr1'] < f).sum()))}")
    for g in (30, 60, 300):
        w(f"  a gap in L1 longer than {g} s: {pct(int((ev['max_gap_s'] > g).sum()))}")
    for c, name in (("frac_snr2", "channel-2 C/N0"), ("frac_tec", "TEC")):
        w(f"  {name} on >= 80% of the event: {pct(int((ev[c] >= 0.8).sum()))}   on none of it: "
          f"{pct(int((ev[c] == 0).sum()))}")
    w(f"  ends in loss of lock: {pct(int(ev['ends_in_loss_of_lock'].sum()))}   "
      f"starts after a gap: {pct(int(ev['starts_after_gap'].sum()))}")
    w("  by receiver type: " + ", ".join(f"{t} {len(g)}" for t, g in ev.groupby("rx_type")))
    w("")
    w("== Candidate rules: events left, and the decisions already made on the ones hidden ==")
    w("(labels of the installed index, matched through the id map)")
    lab = labels.copy()
    if ids is not None:
        lab["event_id"] = lab["event_id"].map(ids.set_index("old_id")["new_id"]).fillna(lab["event_id"])
    lab = lab[lab["event_id"].isin(ev["event_id"])]
    lab["what"] = lab["label"].fillna(lab["action"])
    w(f"decisions so far: {len(lab)} ({_counts(lab['what'])})")
    for mins in (1, 2, 3):
        for cov in (0.0, 0.8, 0.9):
            for gap in (None, 60):
                keep = (ev["n_minutes"] >= mins) & (ev["frac_snr1"].fillna(0) >= cov)
                if gap is not None:
                    keep &= ev["max_gap_s"].fillna(1e9) <= gap
                rule = f"minutes >= {mins}, L1 coverage >= {cov:.0%}" + (f", no gap > {gap} s" if gap else "")
                hid = lab[lab["event_id"].isin(ev.loc[~keep, "event_id"])]
                w(f"  {rule:48s} left {int(keep.sum()):6d} ({100 * keep.mean():5.1f}%)   hidden decided: "
                  f"{_counts(hid['what'])}")
    return "\n".join(L) + "\n"


def _install_id_map(ids: pd.DataFrame) -> None:
    """events/id_map.csv: every id labels may have been saved under -> the id it has now. Maps of
    earlier rebuilds are carried forward (A -> B, then B -> C becomes A -> C)."""
    path = config.events_path().with_name("id_map.csv")
    now = ids[ids["status"] == "merged"][["old_id", "new_id"]]
    step = dict(zip(now["old_id"], now["new_id"]))
    rows = []
    if path.exists():
        prev = pd.read_csv(path)
        for r in prev[prev["status"].eq("merged")].itertuples():
            rows.append((r.old_id, step.get(r.new_id, r.new_id), "merged"))
    rows += [(o, n, "merged") for o, n in step.items()]
    pd.DataFrame(rows, columns=["old_id", "new_id", "status"]).drop_duplicates("old_id", keep="last").to_csv(
        path, index=False)


# --------------------------------------------------------------------------- driver

def run(out: Path, workers: int = 1, receivers: list[str] | None = None, install: bool = False) -> dict:
    cfg = config.load()
    out.mkdir(parents=True, exist_ok=True)
    new = build_events(cfg, write=False)
    if receivers:
        new = new[new["receiver"].isin(receivers)]
    old = None
    if config.events_path().exists():
        old = pd.read_parquet(config.events_path())
        for c in ("start_utc", "end_utc"):
            old[c] = pd.to_datetime(old[c], utc=True)
        if receivers:
            old = old[old["receiver"].isin(receivers)]
    ids = id_map(old, new) if old is not None else None
    q = quality(new, cfg, workers)
    labels = _labels()
    text = report(old, new, ids, q, labels)
    new.to_parquet(out / "events_rebuilt.parquet", index=False)
    q.to_parquet(out / "quality.parquet", index=False)
    if ids is not None:
        ids.to_csv(out / "id_map.csv", index=False)
        changed = ids[ids["status"] != "same"]
        labels[labels["event_id"].isin(changed["old_id"])].merge(
            changed, left_on="event_id", right_on="old_id").to_csv(out / "labels_affected.csv", index=False)
    (out / "report.txt").write_text(text)
    res = {"out": str(out), "events": len(new), "installed": False}
    if install:
        if receivers:
            raise SystemExit("--install rebuilds the whole index; run it without --receiver")
        res["installed"] = str(install_events(new))
        q.to_parquet(config.events_path().with_name("quality.parquet"), index=False)
        if ids is not None:
            _install_id_map(ids)
    (out / "run.json").write_text(json.dumps(res, indent=2))
    return {**res, "report": text}
