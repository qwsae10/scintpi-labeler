"""Candidate events: consecutive candidate minutes of one receiver and satellite.

Gaps of up to ``bridge_gap_minutes`` (5 by default) between candidate minutes
of the same satellite are bridged, so one disturbance is one decision rather
than many 1-minute fragments. ``n_minutes`` counts candidate minutes;
``span_minutes`` is the full length; ``candidate_minutes`` lists them.

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


def events_for_day(day_dir: Path, cfg: dict, receivers: pd.DataFrame, days: pd.DataFrame) -> list[dict]:
    meta = json.loads((day_dir / "meta.json").read_text())
    minute = pd.read_parquet(day_dir / "minute.parquet")
    if minute.empty:
        return []
    ch = int(cfg["candidates"]["s4_channel"])
    bridge = int(cfg["candidates"]["bridge_gap_minutes"])
    cand = minute.loc[candidate_mask(minute, cfg)].sort_values(["prn", "datetime"])
    rx = receivers.set_index("receiver").loc[meta["receiver"]] if meta["receiver"] in set(receivers["receiver"]) \
        else None
    lon = float(rx["longitude"]) if rx is not None else np.nan
    lat = float(rx["latitude"]) if rx is not None else np.nan
    d = days[(days["receiver"] == meta["receiver"]) & (days["date"] == meta["date"])]
    reason = d["reason"].iloc[0] if len(d) else ""
    out = []
    for prn, g in cand.groupby("prn", sort=True):
        gap = g["datetime"].diff().dt.total_seconds().div(60)
        run = (gap.isna() | (gap > bridge + 1)).cumsum()
        for _, e in g.groupby(run):
            start = e["datetime"].iloc[0]
            end = e["datetime"].iloc[-1] + pd.Timedelta(minutes=1)
            s4b = f"s4_{3 - ch}" if ch in (1, 2) else None
            out.append({
                "event_id": make_event_id(meta["receiver"], prn, start, end),
                "receiver": meta["receiver"], "rx_type": meta["rx_type"], "date": meta["date"],
                "day_reason": reason, "prn": prn, "constellation": prn[0],
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
                "has_phase": bool(meta["rx_type"] != "sc2"),
            })
    return out


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


def build_events(cfg: dict | None = None) -> pd.DataFrame:
    cfg = cfg or config.load()
    root = config.products_dir()
    receivers = pd.read_csv(config.data_root() / "inventory" / "receivers.csv")
    days = pd.read_csv(config.data_root() / "days.csv", dtype={"date": str})
    rows = []
    for meta in sorted(root.glob("*/*/meta.json")):
        try:
            rows += events_for_day(meta.parent, cfg, receivers, days)
        except Exception:
            logger.exception("could not read %s", meta.parent)
    ev = pd.DataFrame(rows)
    if len(ev):
        add_queue_order(ev, int(cfg["sampling"]["seed"]))
    path = config.events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        # Keep old indexes: label files refer to event ids.
        path.rename(path.with_name(f"events_{pd.Timestamp.utcnow():%Y%m%dT%H%M%S}.parquet"))
    ev.to_parquet(path, index=False)
    return ev


def load_events() -> pd.DataFrame:
    ev = pd.read_parquet(config.events_path())
    for c in ("start_utc", "end_utc"):
        ev[c] = pd.to_datetime(ev[c], utc=True)
    return ev
