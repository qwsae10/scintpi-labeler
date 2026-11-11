"""Inventory of the raw ScintPi archive: one row per raw file with receiver and start time.

ScintPi 2 and 3 files are ``scintpi{2,3}_YYYYMMDD_HHMM_<lon>_<lat>_v<fw>.bin[.zip]``
in ``<raw_root>/scNNN/``. ScintPi 4 files are 15-minute SBF files
``<prefix><doy><hour a-x><mm>.<yy>_`` in ``<raw_root>/scNNN/YYYYMMDD/``.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

SC23_NAME = re.compile(
    r"^scintpi([23])_(\d{8})_(\d{4})_([0-9.]+[EW])_([0-9.]+[NS])_(v\w+?)\.bin(\.zip)?$", re.I
)
SC4_NAME = re.compile(r"^([a-z]{2}[0-9]{2})([0-9]{3})([a-x])([0-9]{2})\.([0-9]{2})_$", re.I)
DATE_DIR = re.compile(r"^20\d{6}$")


def scan_archive(raw_root: str | Path, exclude_folders=()) -> pd.DataFrame:
    """Walk the archive and return ``path, size`` for every raw file it recognizes."""
    rows = []
    raw_root = Path(raw_root)
    for folder in sorted(raw_root.iterdir()):
        if not folder.is_dir() or folder.name in exclude_folders:
            continue
        try:
            entries = list(os.scandir(folder))
        except PermissionError:
            logger.warning("no permission: %s", folder)
            continue
        for entry in entries:
            if entry.is_file() and SC23_NAME.match(entry.name):
                rows.append((entry.path, entry.stat().st_size))
            elif entry.is_dir() and DATE_DIR.match(entry.name):
                try:
                    for sub in os.scandir(entry.path):
                        if sub.is_file() and SC4_NAME.match(sub.name):
                            rows.append((sub.path, sub.stat().st_size))
                except PermissionError:
                    logger.warning("no permission: %s", entry.path)
    return pd.DataFrame(rows, columns=["path", "size"])


def _sc4_prefix_map() -> dict[str, str]:
    from scintkit.stations import load_station_codes

    st = load_station_codes()
    st = st[st["SC4 Prefix"].notna()]
    return {str(p).strip().lower(): str(c) for p, c in zip(st["SC4 Prefix"], st["Code"])}


def _identify(generation: str, lon: str, lat: str) -> str | None:
    from scintkit.stations import identify_station

    name = f"scintpi{generation}_20000101_0000_{lon}_{lat}_v000.bin"
    try:
        hit = identify_station(filename=name)
    except ValueError as error:
        logger.warning("station lookup failed for %s: %s", name, error)
        return None
    return None if hit is None else str(hit.get("Code") or hit.get("code"))


def parse_files(files: pd.DataFrame) -> pd.DataFrame:
    """Add ``rx_type`` (sc2/sc3/sc4), ``receiver`` (registry code), ``start`` and ``firmware``.

    Files whose coordinates match no registry station within 3 km are dropped
    and listed in the log.
    """
    df = files.copy()
    df["name"] = df["path"].map(os.path.basename)
    df["folder"] = df["path"].map(lambda p: Path(p).parts[-2] if not DATE_DIR.match(Path(p).parts[-2])
                                  else Path(p).parts[-3])
    m23 = df["name"].str.extract(SC23_NAME)
    m4 = df["name"].str.extract(SC4_NAME)
    is23 = m23[0].notna()
    is4 = m4[0].notna()

    df["rx_type"] = np.where(is23, "sc" + m23[0].fillna(""), np.where(is4, "sc4", None))
    df["firmware"] = m23[5]
    start23 = pd.to_datetime(m23[1] + m23[2], format="%Y%m%d%H%M", errors="coerce")
    yy = pd.to_numeric(m4[4], errors="coerce")
    doy = pd.to_numeric(m4[1], errors="coerce")
    hour = m4[2].str.lower().map(lambda h: ord(h) - ord("a") if isinstance(h, str) else np.nan)
    minute = pd.to_numeric(m4[3], errors="coerce")
    start4 = (pd.to_datetime((2000 + yy).astype("Int64").astype(str), format="%Y", errors="coerce")
              + pd.to_timedelta(doy - 1, unit="D") + pd.to_timedelta(hour, unit="h")
              + pd.to_timedelta(minute, unit="m"))
    df["start"] = start23.where(is23, start4)

    from scintkit.filenames import coordinates
    from scintkit.stations import load_station_codes

    receiver = pd.Series(None, index=df.index, dtype=object)
    combos = m23.loc[is23, [0, 3, 4]].drop_duplicates()
    lookup = {tuple(r): _identify(*r) for r in combos.itertuples(index=False, name=None)}
    receiver[is23] = [lookup[tuple(r)] for r in m23.loc[is23, [0, 3, 4]].itertuples(index=False, name=None)]
    prefixes = _sc4_prefix_map()
    receiver[is4] = m4.loc[is4, 0].str.lower().map(prefixes)

    # Coordinates: registry for registered receivers, file name otherwise.
    coords = df["name"].where(is23).map(lambda n: coordinates(n) if isinstance(n, str) else None)
    df["latitude"] = coords.map(lambda c: c[0] if c else np.nan)
    df["longitude"] = coords.map(lambda c: c[1] if c else np.nan)
    # Receivers missing from the registry get "<folder>-<type>" codes.
    unregistered = receiver.isna() & is23
    receiver[unregistered] = df.loc[unregistered, "folder"] + "-" + df.loc[unregistered, "rx_type"]
    df["receiver"] = receiver
    reg = load_station_codes().drop_duplicates("Code").set_index("Code")
    known = df["receiver"].isin(reg.index)
    df.loc[known, "latitude"] = df.loc[known, "receiver"].map(reg["Latitude"])
    df.loc[known, "longitude"] = df.loc[known, "receiver"].map(reg["Longitude"])
    df["registered"] = known

    unknown = df["receiver"].isna()
    if unknown.any():
        logger.warning("%d files have no receiver; examples: %s", unknown.sum(),
                       df.loc[unknown, "name"].head(5).tolist())
    df = df[~unknown & df["start"].notna()].copy()

    # Receivers are often tested at UTD before deployment, so files with Dallas
    # coordinates appear in many station folders. Keep each receiver's main folder.
    main = df.groupby("receiver")["folder"].agg(lambda s: s.value_counts().index[0])
    dropped = df["folder"] != df["receiver"].map(main)
    if dropped.any():
        logger.info("dropping %d files outside their receiver's main folder", dropped.sum())
    df = df[~dropped].copy()
    df["date"] = df["start"].dt.strftime("%Y-%m-%d")
    return df.sort_values(["receiver", "start"]).reset_index(drop=True)


def receivers(files: pd.DataFrame) -> pd.DataFrame:
    """One row per receiver: type, main folder, coordinates, registered flag."""
    return (files.groupby("receiver")
            .agg(rx_type=("rx_type", "first"), folder=("folder", "first"),
                 latitude=("latitude", "median"), longitude=("longitude", "median"),
                 registered=("registered", "first"), n_files=("path", "size"),
                 first_date=("date", "min"), last_date=("date", "max"))
            .reset_index())


def nominal_hours(df: pd.DataFrame) -> pd.Series:
    """Nominal file length (h) per receiver: median spacing of file starts, at most 6 h."""
    def spacing(g):
        d = g["start"].sort_values().diff().dt.total_seconds().div(3600)
        d = d[(d > 0.1) & (d <= 6.5)]
        return float(min(6.0, d.median())) if len(d) else 4.0
    return df.groupby("receiver").apply(spacing)


def day_coverage(df: pd.DataFrame) -> pd.DataFrame:
    """Hours of nominal file coverage per receiver and UTC day (start-time day)."""
    hours = nominal_hours(df)
    d = df.drop_duplicates(["receiver", "start"]).copy()
    d["hours"] = d["receiver"].map(hours)
    cov = d.groupby(["receiver", "rx_type", "date"]).agg(hours=("hours", "sum"), n_files=("path", "size"))
    cov["hours"] = cov["hours"].clip(upper=24.0)
    return cov.reset_index()


def drop_unusable_receivers(files: pd.DataFrame, min_files: int = 60, dedup_km: float = 3.0) -> pd.DataFrame:
    """Remove unregistered receivers that are test fragments.

    An unregistered receiver is kept only with nonzero coordinates, at least
    ``min_files`` files, and no larger receiver of the same type within
    ``dedup_km`` (several ScintPi 2 units were run side by side at UTD).
    """
    rx = receivers(files)
    keep = set(rx.loc[rx["registered"], "receiver"])
    cand = rx[~rx["registered"] & (rx["n_files"] >= min_files)
              & ((rx["latitude"].abs() + rx["longitude"].abs()) > 0)]
    for row in cand.sort_values("n_files", ascending=False).itertuples():
        others = rx[(rx["rx_type"] == row.rx_type) & rx["receiver"].isin(keep)]
        d = 111.2 * np.hypot(others["latitude"] - row.latitude,
                             (others["longitude"] - row.longitude) * np.cos(np.radians(row.latitude)))
        if (d < dedup_km).any():
            logger.info("dropping %s: duplicate of a receiver within %.0f km", row.receiver, dedup_km)
            continue
        keep.add(row.receiver)
    dropped = sorted(set(rx["receiver"]) - keep)
    logger.info("dropped receivers: %s", dropped)
    return files[files["receiver"].isin(keep)].copy()
