"""Read products for the app, with small caches."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from scintlab import config

SIDEREAL_S = 86164.0905


def day_dir(receiver: str, date: str) -> Path:
    return config.products_dir() / receiver / date.replace("-", "")


@lru_cache(maxsize=16)
def _read(receiver: str, date: str, kind: str) -> pd.DataFrame | None:
    path = day_dir(receiver, date) / f"{kind}.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize("UTC")
    df["prn"] = df["prn"].astype(str)
    return df


def window(receiver: str, kind: str, t0: pd.Timestamp, t1: pd.Timestamp, prn: str | None = None) -> pd.DataFrame:
    """Rows of ``kind`` (minute, second, highrate) in [t0, t1), joined across UTC days."""
    parts = []
    for d in pd.date_range(t0.floor("D"), t1.floor("D"), freq="D"):
        df = _read(receiver, d.strftime("%Y-%m-%d"), kind)
        if df is None:
            continue
        sel = (df["datetime"] >= t0) & (df["datetime"] < t1)
        if prn is not None:
            sel &= df["prn"].eq(prn)
        parts.append(df.loc[sel])
    if not parts:
        return pd.DataFrame(columns=["datetime", "prn"])
    return pd.concat(parts, ignore_index=True).sort_values("datetime")


def day_minutes(receiver: str, date: str) -> pd.DataFrame | None:
    return _read(receiver, date, "minute")


def available_days(receiver: str) -> list[str]:
    root = config.products_dir() / receiver
    if not root.exists():
        return []
    return sorted(f"{p.parent.name[:4]}-{p.parent.name[4:6]}-{p.parent.name[6:]}" for p in root.glob("*/meta.json"))


def receivers_on(date: str) -> list[str]:
    tag = date.replace("-", "")
    return sorted(p.parent.parent.name for p in config.products_dir().glob(f"*/{tag}/meta.json"))


def geometric_repeat(receiver: str, prn: str, t0: pd.Timestamp, t1: pd.Timestamp, days: int):
    """Time shift (s) that puts ``prn`` on another day (``days`` = -1 or +1) at the same azimuth and
    elevation as during [t0, t1). Returns ``(shift_s, median_mismatch_deg)`` or None.

    GPS ground tracks repeat after about one sidereal day minus ~4 min drift
    per year, so the search starts at ``days`` sidereal days and scans +/- 15 min.
    Galileo, GLONASS and BeiDou MEO satellites do not repeat after one day; the
    mismatch then stays large and the caller reports no repeat.
    """
    ref = window(receiver, "second", t0, t1, prn)
    if "elev" not in ref:
        return None
    ref = ref.dropna(subset=["elev", "azim"])
    if len(ref) < 60:
        return None
    ref = ref.iloc[:: max(1, len(ref) // 120)]
    guess = days * SIDEREAL_S
    lo = t0 + pd.Timedelta(seconds=guess - 900)
    hi = t1 + pd.Timedelta(seconds=guess + 900)
    other = window(receiver, "second", lo, hi, prn)
    if "elev" not in other:
        return None
    other = other.dropna(subset=["elev", "azim"])
    if len(other) < 60:
        return None
    ot = (other["datetime"] - t0).dt.total_seconds().to_numpy()
    rt = (ref["datetime"] - t0).dt.total_seconds().to_numpy()
    best = None
    for shift in np.arange(guess - 900, guess + 901, 2.0):
        el = np.interp(rt + shift, ot, other["elev"].to_numpy(), left=np.nan, right=np.nan)
        az = np.interp(rt + shift, ot, np.unwrap(np.radians(other["azim"].to_numpy())), left=np.nan, right=np.nan)
        d_el = el - ref["elev"].to_numpy()
        d_az = (np.degrees(az) - ref["azim"].to_numpy() + 180) % 360 - 180
        mis = np.nanmedian(np.hypot(d_el, d_az * np.cos(np.radians(ref["elev"].to_numpy()))))
        if np.isfinite(mis) and (best is None or mis < best[1]):
            best = (float(shift), float(mis))
    return best


@lru_cache(maxsize=1)
def kp_table() -> pd.DataFrame | None:
    from scintlab.select_days import read_kp

    path = config.data_root() / "ref" / "kp_ap_f107.txt"
    return read_kp(path) if path.exists() else None
