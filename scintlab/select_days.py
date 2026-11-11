"""Choose the receiver-days to process.

Each receiver gets about ``days_per_receiver`` days, chosen as consecutive
pairs so every event has the same satellite one sidereal day away. Pairs are
drawn in this order until the target is reached:

1. Forced days (2024-05-10/11 and 2024-10-10/11 by default) where the
   receiver has data.
2. Low-latitude receivers (|dipole magnetic latitude| < 30 deg): two pairs in
   the local plasma-bubble season with F10.7 >= 120, then one storm pair.
   Mid-latitude receivers: two storm pairs (daily max Kp >= 6).
3. Random pairs from all remaining eligible days.

A day is eligible when the archive holds at least ``min_day_coverage_hours``
of nominal file time for it. The draw is seeded, so the same inventory and
configuration give the same days.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Dipole north pole, IGRF-13 epoch 2020.
POLE_LAT, POLE_LON = 80.65, -72.68
# Months of the equatorial plasma-bubble season by longitude sector.
SEASON_MONTHS = {
    "american": {9, 10, 11, 12, 1, 2, 3},
    "african": {2, 3, 4, 9, 10},
    "asian": {2, 3, 4, 9, 10},
}
F107_SEASON_MIN = 120.0


def dipole_mlat(lat: float, lon: float) -> float:
    p, l = np.radians(lat), np.radians(lon)
    pp, lp = np.radians(POLE_LAT), np.radians(POLE_LON)
    return float(np.degrees(np.arcsin(np.sin(p) * np.sin(pp) + np.cos(p) * np.cos(pp) * np.cos(l - lp))))


def sector(lon: float) -> str:
    lon = ((lon + 180) % 360) - 180
    if -130 <= lon < -30:
        return "american"
    if -30 <= lon < 60:
        return "african"
    return "asian"


def read_kp(path: str | Path) -> pd.DataFrame:
    """GFZ ``Kp_ap_Ap_SN_F107_since_1932.txt`` -> daily ``kp_max`` and ``f107`` (observed)."""
    rows = []
    with open(path) as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            v = line.split()
            kp = [float(x) for x in v[7:15]]
            rows.append((f"{v[0]}-{v[1]}-{v[2]}", max(kp), float(v[25]), kp))
    df = pd.DataFrame(rows, columns=["date", "kp_max", "f107", "kp3h"])
    df.loc[df["kp_max"] < 0, "kp_max"] = np.nan
    df.loc[df["f107"] <= 0, "f107"] = np.nan
    return df.set_index("date")


def _pairs(days: list[str]) -> list[tuple[str, str]]:
    s = set(days)
    out = []
    for d in sorted(s):
        nxt = (pd.Timestamp(d) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        if nxt in s:
            out.append((d, nxt))
    return out


def choose_days(coverage: pd.DataFrame, stations: pd.DataFrame, kp: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """``coverage``: receiver, rx_type, date, hours, n_files. ``stations``: receiver, latitude, longitude."""
    s = cfg["sampling"]
    rng = np.random.default_rng(int(s["seed"]))
    target = int(s["days_per_receiver"])
    forced = [str(d) for d in s["forced_days"]]
    first = str(s["first_date"])
    reg = stations.drop_duplicates("receiver").set_index("receiver")
    out = []

    for (receiver, rx_type), cov in coverage.groupby(["receiver", "rx_type"]):
        if receiver not in reg.index:
            logger.warning("receiver %s has no coordinates, skipped", receiver)
            continue
        lat, lon = float(reg.loc[receiver, "latitude"]), float(reg.loc[receiver, "longitude"])
        mlat = dipole_mlat(lat, lon)
        low = abs(mlat) < float(s["low_latitude_mlat_deg"])
        cov = cov[cov["date"] >= first].set_index("date")
        good = sorted(cov.index[cov["hours"] >= float(s["min_day_coverage_hours"])])
        pairs = _pairs(good)
        chosen: dict[str, str] = {}

        def take(pair, reason):
            for d in pair:
                chosen.setdefault(d, reason)

        def free(pair):
            return not any(d in chosen for d in pair)

        # 1. Forced days: pairs if possible, single days otherwise (any coverage).
        for d in forced:
            if d in cov.index and cov.loc[d, "hours"] > 0:
                chosen.setdefault(d, "forced")

        def kpmax(pair):
            return max(kp["kp_max"].get(d, np.nan) for d in pair)

        storm = [p for p in pairs if free(p) and kpmax(p) >= float(s["storm_kp"])]
        season_months = SEASON_MONTHS[sector(lon)]
        season = [p for p in pairs if free(p) and pd.Timestamp(p[0]).month in season_months
                  and kp["f107"].get(p[0], np.nan) >= F107_SEASON_MIN]

        def draw(pool, n, reason):
            pool = [p for p in pool if free(p)]
            for i in rng.permutation(len(pool))[:n]:
                if len(chosen) >= target:
                    return
                if free(pool[i]):
                    take(pool[i], reason)

        if low:
            draw(season, 2, "season")
            draw(storm, 1, "storm")
        else:
            draw(storm, 2, "storm")
        while len(chosen) < target:
            pool = [p for p in pairs if free(p)]
            if not pool:
                break
            draw(pool, 1, "random")

        for d, reason in sorted(chosen.items()):
            out.append({
                "receiver": receiver, "rx_type": rx_type, "date": d, "reason": reason,
                "latitude": lat, "longitude": lon, "mlat": round(mlat, 1),
                "low_latitude": low, "coverage_hours": float(cov.loc[d, "hours"]),
                "n_files": int(cov.loc[d, "n_files"]),
                "kp_max": kp["kp_max"].get(d, np.nan), "f107": kp["f107"].get(d, np.nan),
            })
        logger.info("%s (%s, mlat %.1f): %d days", receiver, rx_type, mlat, len(chosen))
    return pd.DataFrame(out)
