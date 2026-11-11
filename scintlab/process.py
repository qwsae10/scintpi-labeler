"""Process one receiver-day: raw files -> minute, second and 20 Hz products for the labeler.

Output folder ``<data_root>/products/<receiver>/<YYYYMMDD>/``:

- ``minute.parquet``  every satellite and minute: geometry, mean C/N0, S4,
  sigma-phi, tau (scintkit, 0.5 level) and tau1e (1/e level), sample counts,
  quality flags.
- ``second.parquet``  every satellite and second (first sample): geometry,
  C/N0 per channel, carrier-phase TEC.
- ``highrate.parquet``  full-rate C/N0 and clock-removed detrended phase of
  the satellites that have candidate minutes, from ``highrate_pad_minutes``
  before their first to that many after their last candidate minute.
- ``meta.json``  inputs, sampling rate, versions, timings.

ScintPi 3 and 4 use the scintkit chain (lvl0 -> epochs -> signals -> cycle
slips -> 0.1 Hz high-pass -> receiver clock removal -> minute products).
ScintPi 2 uses scintlab.sc2 and has only C/N0 on L1.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path

import numpy as np
import pandas as pd

from scintlab import config
from scintlab.sc2 import read_sc2

logger = logging.getLogger(__name__)

HR_COLUMNS = {
    "snr1": "snr1", "snr2": "snr2", "snr3": "snr3",
    "detrended_noclk_cph1": "ph1", "detrended_noclk_cph2": "ph2", "detrended_noclk_cph3": "ph3",
    "cycleslips_cph1": "slip1", "cycleslips_cph2": "slip2", "cycleslips_cph3": "slip3",
}
SECOND_COLUMNS = ["datetime", "prn", "elev", "azim", "snr1", "snr2", "snr3", "tec_cph12"]


# --------------------------------------------------------------------------- tau

def tau_at_level(snr_db: np.ndarray, fs: float, level: float, min_seconds: float) -> float:
    """First lag (s, linearly interpolated) where the normalized autocorrelation of 10**(snr/10) < level."""
    x = snr_db[np.isfinite(snr_db)]
    if len(x) < min_seconds * fs or np.unique(x).size < 2:
        return np.nan
    x = 10 ** (x.astype(float) / 10)
    x = x - x.mean()
    n = len(x)
    f = np.fft.rfft(x, 2 * n)
    ac = np.fft.irfft(f * np.conj(f))[:n]
    if ac[0] <= 0:
        return np.nan
    ac = ac / ac[0]
    below = np.flatnonzero(ac < level)
    if not len(below):
        return np.nan
    k = below[0]
    frac = (ac[k - 1] - level) / (ac[k - 1] - ac[k]) if k > 0 else 0.0
    return (k - 1 + frac) / fs


def add_tau1e(frame: pd.DataFrame, minute: pd.DataFrame, fs: float, cfg: dict) -> pd.DataFrame:
    level = float(cfg["processing"]["tau_level"])
    min_s = float(cfg["processing"]["tau_min_seconds"])
    keys = [frame["prn"], frame["datetime"].dt.floor("1min")]
    for i in (1, 2, 3):
        col = f"snr{i}"
        if col not in frame.columns:
            continue
        tau = frame.groupby(keys, sort=False)[col].agg(
            lambda s: tau_at_level(s.to_numpy(dtype=float), fs, level, min_s))
        tau.index.names = ["prn", "datetime"]
        minute = minute.merge(tau.rename(f"tau1e_{i}").reset_index(), on=["prn", "datetime"], how="left")
    return minute


# --------------------------------------------------------------------------- loading

def _blocks(files: pd.DataFrame, hours: float) -> list[list[str]]:
    """Group files (sorted by start) into blocks spanning at most ``hours``."""
    out, cur, t0 = [], [], None
    for row in files.sort_values("start").itertuples():
        if cur and (row.start - t0) >= pd.Timedelta(hours=hours):
            out.append(cur)
            cur = []
        if not cur:
            t0 = row.start
        cur.append(row.path)
    if cur:
        out.append(cur)
    return out


def _fill_geometry(df: pd.DataFrame, lat: float, lon: float) -> pd.DataFrame:
    """Elevation and azimuth from SP3 orbits when the file has none (ScintPi 4 without SatVisibility1)."""
    from scintkit.formatting import satellite
    from scintlab.orbits import elev_azim

    key = df["cons"].astype(np.int64) * 1000 + df["svid"].astype(np.int64)
    prn_of = {k: satellite(int(k // 1000), int(k % 1000))[1] for k in np.unique(key)}
    elev, azim = elev_azim(df["datetime"], key.map(prn_of), lat, lon)
    df["elev"], df["azim"] = elev, azim
    logger.info("geometry from SP3 orbits: %.0f%% of rows have elevation", 100 * np.isfinite(elev).mean())
    return df


def _scintkit_block(paths: list[str], work: Path, cfg: dict, lat: float = np.nan, lon: float = np.nan):
    from scintkit.products import compute_products
    from scintkit.reading import read_lvl0, to_lvl0

    os.environ["SCINTKIT_BIN2ASC"] = cfg["paths"]["bin2asc"]
    frames = []
    for p in paths:
        try:
            lvl0 = to_lvl0(p, work)
            df, _ = read_lvl0(lvl0)
            frames.append(df)
        except Exception as error:  # one bad file must not lose the day
            logger.warning("skipping %s: %s", os.path.basename(p), error)
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    del frames
    if ("elev" not in df or df["elev"].isna().all()) and np.isfinite(lat):
        df = _fill_geometry(df, lat, lon)
    frame, minute, fs = compute_products(df)
    return frame, minute, fs


def _sc2_block(paths: list[str]):
    from scintkit.amplitude import compute_s4, compute_s4_corrected, compute_tau
    from scintkit.constants import S4_MIN_SAMPLE_FRACTION

    frames = []
    for p in paths:
        try:
            frames.append(read_sc2(p))
        except Exception as error:
            logger.warning("skipping %s: %s", os.path.basename(p), error)
    if not frames:
        return None
    frame = (pd.concat(frames, ignore_index=True)
             .drop_duplicates(["prn", "datetime"]).sort_values(["datetime", "prn"]).reset_index(drop=True))
    per_second = frame.groupby([frame["prn"], frame["datetime"].dt.floor("1s")]).size()
    fs = float(per_second.mode().iloc[0])
    keys = [frame["prn"], frame["datetime"].dt.floor("1min")]
    g = frame.groupby(keys, sort=False)["snr1"]
    minute = pd.DataFrame({
        "s4_1": g.agg(compute_s4),
        "s4_corrected_1": g.agg(compute_s4_corrected),
        "tau_1": g.agg(lambda x: compute_tau(x, fs)),
        "n_s4_1": g.count(),
    })
    minute.index.names = ["prn", "datetime"]
    minute = minute.reset_index()
    minute["s4_quality_flag_1"] = (minute["n_s4_1"] < fs * 60 * S4_MIN_SAMPLE_FRACTION).astype(np.int8)
    return frame, minute, fs


# --------------------------------------------------------------------------- per-block reduction

def _minute_table(frame: pd.DataFrame, minute: pd.DataFrame) -> pd.DataFrame:
    from scintkit.formatting import first_samples

    first = first_samples(frame, "1min")
    keep = [c for c in ("datetime", "prn", "cons", "elev", "azim", "sig_1", "sig_2", "sig_3") if c in first]
    first = first[keep]
    means = {}
    keys = [frame["prn"], frame["datetime"].dt.floor("1min")]
    for i in (1, 2, 3):
        if f"snr{i}" in frame.columns:
            means[f"snr{i}_mean"] = frame.groupby(keys, sort=False)[f"snr{i}"].mean()
    m = pd.DataFrame(means)
    m.index.names = ["prn", "datetime"]
    out = first.merge(m.reset_index(), on=["prn", "datetime"], how="left")
    drop = [c for c in minute.columns if c.startswith("_")]
    return out.merge(minute.drop(columns=drop), on=["prn", "datetime"], how="left")


def _second_table(frame: pd.DataFrame) -> pd.DataFrame:
    from scintkit.formatting import first_samples

    sec = first_samples(frame, "1s")
    return sec[[c for c in SECOND_COLUMNS if c in sec.columns]]


def _highrate_table(frame: pd.DataFrame) -> pd.DataFrame:
    cols = {k: v for k, v in HR_COLUMNS.items() if k in frame.columns}
    hr = frame[["datetime", "prn", *cols]].rename(columns=cols)
    for c in hr.columns:
        if c.startswith(("snr", "ph")):
            hr[c] = hr[c].astype(np.float32)
    return hr


def _shrink(df: pd.DataFrame) -> pd.DataFrame:
    for c in df.columns:
        if df[c].dtype == np.float64 and c not in ("tec_cph12",):
            df[c] = df[c].astype(np.float32)
    if "prn" in df:
        df["prn"] = df["prn"].astype("category")
    return df


# --------------------------------------------------------------------------- candidates

def candidate_mask(minute: pd.DataFrame, cfg: dict) -> pd.Series:
    c = cfg["candidates"]
    ch = int(c["s4_channel"])
    s4 = minute.get(f"s4_{ch}")
    if s4 is None:
        return pd.Series(False, index=minute.index)
    q = minute.get(f"s4_quality_flag_{ch}", pd.Series(0, index=minute.index)).fillna(1)
    return (s4 > float(c["s4_threshold"])) & (minute["elev"] > float(c["min_elevation_deg"])) & q.eq(0)


def highrate_windows(minute: pd.DataFrame, cfg: dict) -> dict[str, list[tuple[pd.Timestamp, pd.Timestamp]]]:
    pad = pd.Timedelta(minutes=float(cfg["processing"]["highrate_pad_minutes"]))
    cand = minute.loc[candidate_mask(minute, cfg), ["prn", "datetime"]]
    out = {}
    for prn, g in cand.groupby("prn", observed=True):
        spans = []
        for t in sorted(g["datetime"]):
            lo, hi = t - pad, t + pd.Timedelta(minutes=1) + pad
            if spans and lo <= spans[-1][1]:
                spans[-1] = (spans[-1][0], max(spans[-1][1], hi))
            else:
                spans.append((lo, hi))
        out[str(prn)] = spans
    return out


# --------------------------------------------------------------------------- driver

def process_day(receiver: str, rx_type: str, date: str, files: pd.DataFrame, cfg: dict | None = None,
                overwrite: bool = False) -> Path:
    """Process the raw ``files`` (path, start) of one receiver and UTC ``date``; return the output folder."""
    cfg = cfg or config.load()
    out = config.products_dir() / receiver / date.replace("-", "")
    if (out / "meta.json").exists() and not overwrite:
        logger.info("done already: %s", out)
        return out
    out.mkdir(parents=True, exist_ok=True)
    work = Path(cfg["paths"]["work_dir"]) / f"{receiver}_{date}_{os.getpid()}"
    work.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    day0 = pd.Timestamp(date)
    day1 = day0 + pd.Timedelta(days=1)
    hours = float(cfg["processing"]["block_hours"][rx_type])
    lat = float(files["latitude"].median()) if "latitude" in files else np.nan
    lon = float(files["longitude"].median()) if "longitude" in files else np.nan
    minutes, seconds, hr_parts, fs_seen, used = [], [], [], set(), []
    try:
        for i, block in enumerate(_blocks(files, hours)):
            t0 = time.time()
            res = _sc2_block(block) if rx_type == "sc2" else _scintkit_block(block, work, cfg, lat, lon)
            if res is None:
                continue
            frame, minute, fs = res
            fs_seen.add(float(fs))
            frame = frame[(frame["datetime"] >= day0) & (frame["datetime"] < day1)]
            minute = minute[(minute["datetime"] >= day0) & (minute["datetime"] < day1)]
            if frame.empty:
                continue
            minute = add_tau1e(frame, minute, fs, cfg)
            minutes.append(_shrink(_minute_table(frame, minute)))
            seconds.append(_shrink(_second_table(frame)))
            hr_path = work / f"hr_{i:03d}.parquet"
            _highrate_table(frame).to_parquet(hr_path, index=False)
            hr_parts.append(hr_path)
            used += block
            del frame, minute
            for p in work.glob("*_lvl0.pq"):
                p.unlink()
            logger.info("%s %s block %d: %d files, %.0f s", receiver, date, i, len(block), time.time() - t0)

        if not minutes:
            raise RuntimeError(f"no data for {receiver} {date}")
        minute = (pd.concat(minutes, ignore_index=True)
                  .drop_duplicates(["prn", "datetime"]).sort_values(["prn", "datetime"]))
        second = (pd.concat(seconds, ignore_index=True)
                  .drop_duplicates(["prn", "datetime"]).sort_values(["prn", "datetime"]))
        minute["prn"] = minute["prn"].astype(str)
        second["prn"] = second["prn"].astype(str)
        windows = highrate_windows(minute, cfg)
        hr = []
        for p in hr_parts:
            part = pd.read_parquet(p)
            part = part[part["prn"].isin(list(windows))]
            if part.empty:
                continue
            keep = np.zeros(len(part), dtype=bool)
            t = part["datetime"].to_numpy()
            prn = part["prn"].astype(str).to_numpy()
            for sat, spans in windows.items():
                sel = prn == sat
                for lo, hi in spans:
                    keep |= sel & (t >= np.datetime64(lo)) & (t < np.datetime64(hi))
            hr.append(part[keep])
        highrate = (pd.concat(hr, ignore_index=True) if hr else _highrate_table(pd.DataFrame(
            columns=["datetime", "prn"])))
        highrate = highrate.drop_duplicates(["prn", "datetime"]).sort_values(["prn", "datetime"])
        highrate["prn"] = highrate["prn"].astype(str)

        minute.to_parquet(out / "minute.parquet", index=False)
        second.to_parquet(out / "second.parquet", index=False)
        # Sorted by satellite and time in row groups of 200k rows, so the app can read one
        # satellite and window without loading the day.
        highrate.to_parquet(out / "highrate.parquet", index=False, row_group_size=200_000)
        meta = {
            "receiver": receiver, "rx_type": rx_type, "date": date,
            "sampling_rate_hz": sorted(fs_seen), "files": [os.path.basename(p) for p in used],
            "n_candidate_minutes": int(candidate_mask(minute, cfg).sum()),
            "n_highrate_rows": int(len(highrate)),
            "candidates": cfg["candidates"], "processing": cfg["processing"],
            "scintkit_version": _version("scintkit"), "seconds": round(time.time() - t_start, 1),
            "written_utc": pd.Timestamp.utcnow().isoformat(),
        }
        (out / "meta.json").write_text(json.dumps(meta, indent=2, default=str))
        logger.info("wrote %s (%d candidate minutes, %.0f s)", out, meta["n_candidate_minutes"], meta["seconds"])
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _version(pkg: str) -> str:
    try:
        from importlib.metadata import version
        return version(pkg)
    except Exception:
        return "unknown"
