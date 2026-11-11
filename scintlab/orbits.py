"""Satellite elevation and azimuth from SP3 orbits, for ScintPi 4 files without a SatVisibility1 block.

Orbits come from TECbase's daily files on ganymede2:
``/titan/frodrigues/tecbase/nav/YYYY/DDD/`` (``GRG0MGXFIN`` multi-GNSS first,
then ``IGS0OPSFIN`` GPS only). Positions are interpolated with a cubic spline
over the 5- or 15-minute samples; light time is ignored (< 0.01 deg).
"""

from __future__ import annotations

import gzip
import logging
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.interpolate import CubicSpline

logger = logging.getLogger(__name__)

NAV_ROOT = Path("/titan/frodrigues/tecbase/nav")
PRODUCTS = ("GRG0MGXFIN", "COD0MGXFIN", "IGS0OPSFIN")
WGS84_A, WGS84_E2 = 6378137.0, 6.69437999014e-3
GPS_MINUS_UTC = pd.Timedelta(seconds=18)


def read_sp3(path: Path) -> pd.DataFrame:
    """SP3 file -> DataFrame(time [GPS], prn, x, y, z [m])."""
    rows, t = [], None
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt") as f:
        for line in f:
            if line.startswith("*"):
                v = line[1:].split()
                t = pd.Timestamp(int(v[0]), int(v[1]), int(v[2]), int(v[3]), int(v[4])) + \
                    pd.Timedelta(seconds=float(v[5]))
            elif line.startswith("P") and t is not None:
                prn = line[1:4].replace(" ", "0")
                x, y, z = (float(line[4 + 14 * i: 18 + 14 * i]) for i in range(3))
                if abs(x) > 1 and abs(x) < 1e5:
                    rows.append((t, prn, x * 1e3, y * 1e3, z * 1e3))
    return pd.DataFrame(rows, columns=["time", "prn", "x", "y", "z"])


@lru_cache(maxsize=4)
def orbits_for_day(day: str) -> pd.DataFrame | None:
    """Orbits of ``day`` plus the neighbouring days (for interpolation near midnight)."""
    parts = []
    for k in (-1, 0, 1):
        d = pd.Timestamp(day) + pd.Timedelta(days=k)
        folder = NAV_ROOT / f"{d:%Y}" / f"{d.dayofyear:03d}"
        for prod in PRODUCTS:
            hits = sorted(folder.glob(f"{prod}_{d:%Y}{d.dayofyear:03d}*_ORB.SP3*"))
            if hits:
                parts.append(read_sp3(hits[0]))
                break
    if not parts:
        return None
    return pd.concat(parts).drop_duplicates(["time", "prn"]).sort_values(["prn", "time"])


def ecef_to_enu_matrix(lat: float, lon: float) -> np.ndarray:
    p, l = np.radians(lat), np.radians(lon)
    return np.array([[-np.sin(l), np.cos(l), 0],
                     [-np.sin(p) * np.cos(l), -np.sin(p) * np.sin(l), np.cos(p)],
                     [np.cos(p) * np.cos(l), np.cos(p) * np.sin(l), np.sin(p)]])


def station_ecef(lat: float, lon: float, h: float = 0.0) -> np.ndarray:
    p, l = np.radians(lat), np.radians(lon)
    n = WGS84_A / np.sqrt(1 - WGS84_E2 * np.sin(p) ** 2)
    return np.array([(n + h) * np.cos(p) * np.cos(l), (n + h) * np.cos(p) * np.sin(l),
                     (n * (1 - WGS84_E2) + h) * np.sin(p)])


def elev_azim(times_utc: pd.Series, prns: pd.Series, lat: float, lon: float) -> tuple[np.ndarray, np.ndarray]:
    """Elevation and azimuth (deg) for each (UTC time, RINEX prn) row; NaN where no orbit."""
    t = pd.to_datetime(times_utc)
    elev = np.full(len(t), np.nan, dtype=np.float32)
    azim = np.full(len(t), np.nan, dtype=np.float32)
    if len(t) == 0:
        return elev, azim
    orb = orbits_for_day(t.iloc[len(t) // 2].strftime("%Y-%m-%d"))
    if orb is None:
        logger.warning("no SP3 orbits for %s; elevation stays NaN", t.iloc[0].date())
        return elev, azim
    rx = station_ecef(lat, lon)
    R = ecef_to_enu_matrix(lat, lon)
    gps_s = ((t + GPS_MINUS_UTC) - pd.Timestamp("2000-01-01")).dt.total_seconds().to_numpy()
    prn_arr = np.asarray(prns, dtype=str)
    for prn, o in orb.groupby("prn"):
        sel = prn_arr == prn
        if not sel.any() or len(o) < 8:
            continue
        ot = (o["time"] - pd.Timestamp("2000-01-01")).dt.total_seconds().to_numpy()
        # Unique whole seconds, then broadcast: 20-50 Hz rows share a second.
        ts = np.floor(gps_s[sel])
        uniq, inv = np.unique(ts, return_inverse=True)
        inside = (uniq >= ot[0]) & (uniq <= ot[-1])
        xyz = np.full((len(uniq), 3), np.nan)
        if inside.any():
            for k, c in enumerate("xyz"):
                xyz[inside, k] = CubicSpline(ot, o[c].to_numpy())(uniq[inside])
        enu = (xyz - rx) @ R.T
        e = np.degrees(np.arctan2(enu[:, 2], np.hypot(enu[:, 0], enu[:, 1])))
        a = np.degrees(np.arctan2(enu[:, 0], enu[:, 1])) % 360
        elev[sel] = e[inv]
        azim[sel] = a[inv]
    return elev, azim
