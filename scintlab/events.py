"""Candidate events: consecutive candidate minutes of one receiver and satellite.

Gaps of up to ``bridge_gap_minutes`` (5 by default) between candidate minutes
of the same satellite are bridged, so one disturbance is one decision rather
than many 1-minute fragments. Consecutive processed days of a receiver are
grouped together, so an event running over UTC midnight is one event.
``n_minutes`` counts candidate minutes; ``span_minutes`` is the full length;
``candidate_minutes`` lists them.

A candidate minute has S4 above the threshold on the configured channel,
elevation above ``min_elevation_deg`` (5 deg by default) and a good S4
quality flag. The app applies a stricter, user-chosen elevation mask on top.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from scintlab import config
from scintlab.process import candidate_mask

logger = logging.getLogger(__name__)


def make_event_id(receiver: str, prn: str, start: pd.Timestamp, end: pd.Timestamp) -> str:
    h = hashlib.sha1(f"{receiver}|{prn}|{start.isoformat()}|{end.isoformat()}".encode()).hexdigest()[:8]
    return f"{receiver}_{prn}_{start:%Y%m%dT%H%M}_{h}"


def local_solar_hour(t: pd.Series, lon: float) -> pd.Series:
    return ((t.dt.hour + t.dt.minute / 60 + lon / 15.0) % 24).round(2)


def _day_candidates(day_dir: Path, cfg: dict) -> tuple[dict, pd.DataFrame]:
    """meta.json and the candidate minutes of one processed receiver-day."""
    meta = json.loads((day_dir / "meta.json").read_text())
    minute = pd.read_parquet(day_dir / "minute.parquet")
    if minute.empty:
        return meta, minute
    return meta, minute.loc[candidate_mask(minute, cfg)]


def _group_events(cand: pd.DataFrame, cfg: dict, rx_type: str, receiver: str, processed: set[str],
                  receivers: pd.DataFrame, days: pd.DataFrame) -> list[dict]:
    """Events from the candidate minutes of one receiver, which may span several consecutive days.

    ``processed`` holds the UTC dates (YYYY-MM-DD) with products for this receiver; an event within
    a bridge gap of midnight whose neighbouring day is not processed may continue there unseen, and
    is marked ``cut_at_day_edge``.
    """
    if cand.empty:
        return []
    ch = int(cfg["candidates"]["s4_channel"])
    bridge = int(cfg["candidates"]["bridge_gap_minutes"])
    reach = pd.Timedelta(minutes=bridge + 1)  # the farthest apart two joined candidate minutes can be
    cand = cand.sort_values(["prn", "datetime"])
    rx = receivers.set_index("receiver").loc[receiver] if receiver in set(receivers["receiver"]) else None
    lon = float(rx["longitude"]) if rx is not None else np.nan
    lat = float(rx["latitude"]) if rx is not None else np.nan
    reasons = days[days["receiver"] == receiver].set_index("date")["reason"].to_dict()
    day = lambda t: t.strftime("%Y-%m-%d")  # noqa: E731
    out = []
    for prn, g in cand.groupby("prn", sort=True):
        gap = g["datetime"].diff().dt.total_seconds().div(60)
        run = (gap.isna() | (gap > bridge + 1)).cumsum()
        for _, e in g.groupby(run):
            start = e["datetime"].iloc[0]
            end = e["datetime"].iloc[-1] + pd.Timedelta(minutes=1)
            first_day, last_day = start.floor("D"), e["datetime"].iloc[-1].floor("D")
            cut_before = start - reach < first_day and day(first_day - pd.Timedelta(days=1)) not in processed
            cut_after = e["datetime"].iloc[-1] + reach >= last_day + pd.Timedelta(days=1) \
                and day(last_day + pd.Timedelta(days=1)) not in processed
            s4b = f"s4_{3 - ch}" if ch in (1, 2) else None
            out.append({
                "event_id": make_event_id(receiver, prn, start, end),
                "receiver": receiver, "rx_type": rx_type, "date": day(start),
                "day_reason": reasons.get(day(start), ""), "prn": prn, "constellation": prn[0],
                "start_utc": start, "end_utc": end, "n_minutes": int(len(e)),
                "span_minutes": int(round((end - start).total_seconds() / 60)),
                # Candidate minutes only; bridged gaps inside the event are not candidates.
                "candidate_minutes": [t.strftime("%Y-%m-%dT%H:%MZ") for t in e["datetime"]],
                "peak_s4": float(e[f"s4_{ch}"].max()),
                "peak_s4_other": float(e[s4b].max()) if s4b and s4b in e and e[s4b].notna().any() else np.nan,
                "mean_s4": float(e[f"s4_{ch}"].mean()),
                "min_elev": float(e["elev"].min()), "max_elev": float(e["elev"].max()),
                "mean_azim": float(e["azim"].mean()),
                "latitude": lat, "longitude": lon,
                "local_hour": float(local_solar_hour(pd.Series([start]), lon).iloc[0]) if np.isfinite(lon) else np.nan,
                "has_phase": bool(rx_type != "sc2"),
                # Joined across UTC midnight (built from two days' products).
                "crosses_midnight": bool(first_day != last_day),
                "cut_at_day_edge": bool(cut_before or cut_after),
            })
    return out


def events_for_day(day_dir: Path, cfg: dict, receivers: pd.DataFrame, days: pd.DataFrame) -> list[dict]:
    """Events of one receiver-day on its own (events running over midnight are cut there)."""
    meta, cand = _day_candidates(day_dir, cfg)
    return _group_events(cand, cfg, meta["rx_type"], meta["receiver"], {meta["date"]}, receivers, days)


def events_for_receiver(day_dirs: list[Path], cfg: dict, receivers: pd.DataFrame, days: pd.DataFrame) -> list[dict]:
    """Events of one receiver over all its processed days. Candidate minutes of consecutive days are
    grouped together, so an event running over UTC midnight is one event, not one per day."""
    metas, cands = [], []
    for d in sorted(day_dirs):
        try:
            meta, cand = _day_candidates(d, cfg)
        except Exception:
            logger.exception("could not read %s", d)
            continue
        metas.append(meta)
        if len(cand):
            cands.append(cand)
    if not metas:
        return []
    rx_type = metas[-1]["rx_type"]
    processed = {m["date"] for m in metas}
    cand = pd.concat(cands, ignore_index=True) if cands else pd.DataFrame()
    # A day file may repeat the last seconds of the previous day; one row per (prn, minute).
    if len(cand):
        cand = cand.drop_duplicates(["prn", "datetime"])
    return _group_events(cand, cfg, rx_type, metas[-1]["receiver"], processed, receivers, days)


S4_BINS = [0.2, 0.3, 0.5, np.inf]
ELEV_BINS = [0, 10, 20, 30, 90.01]


def add_queue_order(ev: pd.DataFrame, seed: int) -> None:
    """Add ``queue_rank`` (uniform random order), ``stratum`` and ``strat_rank`` (stratified order).

    Strata are receiver type x peak-S4 bin x max-elevation bin. The stratified
    order takes one event from each stratum in turn (each stratum shuffled), so
    rare strata (strong, high-elevation events) are reached early while every
    stratum's labeled share stays known: weight = N_stratum / n_labeled_stratum.
    """
    key = ev["event_id"].map(lambda s: int(hashlib.sha1(f"{seed}|{s}".encode()).hexdigest()[:12], 16))
    ev["queue_rank"] = key.rank(method="first").astype(int)
    s4 = pd.cut(ev["peak_s4"], S4_BINS, right=False, labels=["s4_0.2-0.3", "s4_0.3-0.5", "s4_0.5+"])
    el = pd.cut(ev["max_elev"], ELEV_BINS, right=False, labels=["el_5-10", "el_10-20", "el_20-30", "el_30+"])
    ev["stratum"] = ev["rx_type"] + "|" + s4.astype(str) + "|" + el.astype(str)
    within = ev.groupby("stratum")["queue_rank"].rank(method="first")
    ev["strat_rank"] = (within * 1000 + ev["stratum"].map(
        {k: i for i, k in enumerate(sorted(ev["stratum"].unique()))})).rank(method="first").astype(int)


def build_events(cfg: dict | None = None, write: bool = True) -> pd.DataFrame:
    """Find the events in every processed receiver-day; with ``write``, install them as the event index
    (the previous index is kept under a dated name)."""
    cfg = cfg or config.load()
    root = config.products_dir()
    receivers = pd.read_csv(config.data_root() / "inventory" / "receivers.csv")
    days = pd.read_csv(config.data_root() / "days.csv", dtype={"date": str})
    by_rx: dict[str, list[Path]] = {}
    for meta in sorted(root.glob("*/*/meta.json")):
        by_rx.setdefault(meta.parent.parent.name, []).append(meta.parent)
    rows = []
    for rx, dirs in sorted(by_rx.items()):
        rows += events_for_receiver(dirs, cfg, receivers, days)
    ev = pd.DataFrame(rows)
    if len(ev):
        add_queue_order(ev, int(cfg["sampling"]["seed"]))
    if write:
        install_events(ev)
    return ev


def install_events(ev: pd.DataFrame) -> Path:
    """Make ``ev`` the event index the app reads; the previous index is kept under a dated name."""
    path = config.events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        # Keep old indexes: label files refer to event ids.
        path.rename(path.with_name(f"events_{pd.Timestamp.utcnow():%Y%m%dT%H%M%S}.parquet"))
    ev.to_parquet(path, index=False)
    return path


def filter_events(ev: pd.DataFrame, rules: dict) -> pd.DataFrame:
    """Events passing the data rules, named as in ``queue_filters`` of the config (percentages 0-100;
    None or "" = off) plus ``elevation_mask_deg`` (on the event's highest elevation). The queue and
    the export use the same rules. Events whose quality was not measured (NaN) pass."""
    val = lambda k: None if rules.get(k) in (None, "") else rules[k]  # noqa: E731
    if val("elevation_mask_deg") is not None:
        ev = ev[ev["max_elev"] >= float(val("elevation_mask_deg"))]
    if val("min_minutes") is not None:
        ev = ev[ev["n_minutes"] >= float(val("min_minutes"))]
    for key, col in (("min_frac_snr1", "frac_snr1"), ("min_frac_snr2", "frac_snr2"), ("min_frac_tec", "frac_tec")):
        if val(key) is not None and col in ev:
            ev = ev[~(ev[col] < float(val(key)) / 100)]
    if val("max_gap_s") is not None and "max_gap_s" in ev:
        ev = ev[~(ev["max_gap_s"] > float(val("max_gap_s")))]
    if val("hide_cut") and str(val("hide_cut")).lower() not in ("0", "false") and "cut_at_day_edge" in ev:
        ev = ev[~ev["cut_at_day_edge"].fillna(False).astype(bool)]
    lol = val("loss_of_lock")
    if lol in ("hide", "only") and "ends_in_loss_of_lock" in ev:
        hit = ev["ends_in_loss_of_lock"].fillna(False).astype(bool) | ev["starts_after_gap"].fillna(False).astype(bool)
        ev = ev[~hit] if lol == "hide" else ev[hit]
    return ev


def load_events() -> pd.DataFrame:
    """The installed event index, with the data-quality columns of ``scintlab audit-events``
    (events/quality.parquet) when they exist; events without them have NaN there. Events labelers
    added by hand (``added_events``) follow, with ``added`` True."""
    ev = pd.read_parquet(config.events_path())
    for c in ("start_utc", "end_utc"):
        ev[c] = pd.to_datetime(ev[c], utc=True)
    q = config.events_path().with_name("quality.parquet")
    if q.exists():
        ev = ev.merge(pd.read_parquet(q), on="event_id", how="left")
    ev["added"] = False
    add = added_events()
    if len(add):
        add = add[~add["event_id"].isin(ev["event_id"])]
        ev = pd.concat([ev, add], ignore_index=True)
    return ev


# ---------------------------------------------------------------- events added by hand
# A labeler who sees scintillation just outside an event's window (in the margin the plots show
# around it) marks those minutes, and they become an event of their own at the end of the queue.
# They are shared by every labeler, kept in events/added.jsonl (a log: an "add" line per event, a
# "remove" line when one is taken back), and survive a rebuild of the event index. They were not
# drawn by the sampling plan, so the export leaves them out of the population and its weights.

def added_path() -> Path:
    return config.events_path().with_name("added.jsonl")


def added_events() -> pd.DataFrame:
    from scintlab.labels import _read

    rows: dict[str, dict] = {}
    for r in _read(added_path()):
        if r.get("action") == "add" and r.get("event"):
            rows[r["event"]["event_id"]] = r["event"]
        elif r.get("action") == "remove":
            rows.pop(r.get("event_id"), None)
    if not rows:
        return pd.DataFrame()
    ev = pd.DataFrame(list(rows.values()))
    for c in ("start_utc", "end_utc"):
        ev[c] = pd.to_datetime(ev[c], utc=True)
    ev["added"] = True
    return ev


def make_added_event(src: dict, start: pd.Timestamp, end: pd.Timestamp, minutes: pd.DataFrame,
                     cfg: dict, user: str, rank: int) -> dict:
    """The event row for minutes [start, end) of ``src``'s receiver and satellite, measured from
    their minute products as ``_group_events`` measures a detected event. ``rank``: its place in
    the random and stratified orders (the caller gives one past the last, so it comes last)."""
    ch = int(cfg["candidates"]["s4_channel"])
    s4, s4b = f"s4_{ch}", (f"s4_{3 - ch}" if ch in (1, 2) else None)
    m = minutes[(minutes["datetime"] >= start) & (minutes["datetime"] < end)]
    num = lambda col, f: float(getattr(m[col], f)()) if col in m and m[col].notna().any() else np.nan  # noqa: E731
    lon = src.get("longitude")
    lon = float(lon) if lon is not None and np.isfinite(float(lon)) else np.nan
    first_day, last_day = start.floor("D"), (end - pd.Timedelta(minutes=1)).floor("D")
    return {
        "event_id": make_event_id(src["receiver"], src["prn"], start, end),
        "receiver": src["receiver"], "rx_type": src["rx_type"], "date": start.strftime("%Y-%m-%d"),
        "day_reason": src.get("day_reason", "") if src.get("date") == start.strftime("%Y-%m-%d") else "",
        "prn": src["prn"], "constellation": src["prn"][0],
        "start_utc": start.isoformat(), "end_utc": end.isoformat(),
        "n_minutes": int(round((end - start).total_seconds() / 60)),
        "span_minutes": int(round((end - start).total_seconds() / 60)),
        "candidate_minutes": [],  # none chosen by the detector: the export counts every minute
        "peak_s4": num(s4, "max"), "peak_s4_other": num(s4b, "max") if s4b else np.nan,
        "mean_s4": num(s4, "mean"), "min_elev": num("elev", "min"), "max_elev": num("elev", "max"),
        "mean_azim": num("azim", "mean"), "latitude": src.get("latitude"), "longitude": src.get("longitude"),
        "local_hour": float(local_solar_hour(pd.Series([start]), lon).iloc[0]) if np.isfinite(lon) else np.nan,
        "has_phase": bool(src.get("has_phase", True)), "crosses_midnight": bool(first_day != last_day),
        "cut_at_day_edge": False, "queue_rank": rank, "strat_rank": rank, "stratum": "added",
        "added_by": user, "added_from": src["event_id"],
    }
