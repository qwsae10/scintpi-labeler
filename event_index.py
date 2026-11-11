"""Discover continuous station/satellite periods where raw S4 channel 1 is high."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

from config import (
    DATA_ROOT,
    EXCLUDED_STATIONS,
    EXCLUDED_TIMESTAMP_DATES,
    MAX_ELEVATION_DEG,
    MIN_ELEVATION_DEG,
    MIN_SAMPLES,
    S4_THRESHOLD,
)
from database import get_metadata, replace_active_events


INDEX_VERSION = 4
CANDIDATE_COLUMNS = [
    "minbin",
    "prn",
    "svid",
    "elev",
    "s4_1",
    "s4_2",
    "n_1",
    "quality_1",
]


def source_files(data_root: Path = DATA_ROOT) -> list[Path]:
    return sorted(
        path
        for path in data_root.glob("*/*_1sec.pq")
        if path.parent.name not in EXCLUDED_STATIONS
    )


def source_fingerprint(files: list[Path]) -> str:
    digest = hashlib.sha256()
    digest.update(f"index-version={INDEX_VERSION}\n".encode())
    digest.update(f"s4-threshold={S4_THRESHOLD}\n".encode())
    digest.update(f"minimum-samples={MIN_SAMPLES}\n".encode())
    digest.update(f"elevation=({MIN_ELEVATION_DEG},{MAX_ELEVATION_DEG}]\n".encode())
    for path in files:
        stat = path.stat()
        digest.update(f"{path}:{stat.st_size}:{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()


def needs_rebuild(data_root: Path = DATA_ROOT) -> bool:
    files = source_files(data_root)
    if not files:
        return True
    return get_metadata("source_fingerprint") != source_fingerprint(files)


def build_event_index(data_root: Path = DATA_ROOT) -> dict[str, object]:
    files = source_files(data_root)
    if not files:
        raise FileNotFoundError(f"No *_1sec.pq files found under {data_root}")

    minute_frames: list[pd.DataFrame] = []
    for path in files:
        table = pq.read_table(path, columns=CANDIDATE_COLUMNS)
        mask = pc.and_kleene(
            pc.and_kleene(
                pc.and_kleene(
                    pc.is_valid(table["minbin"]),
                    pc.is_valid(table["prn"]),
                ),
                pc.not_equal(table["svid"], 255),
            ),
            pc.and_kleene(
                pc.and_kleene(
                    pc.greater(table["elev"], MIN_ELEVATION_DEG),
                    pc.less_equal(table["elev"], MAX_ELEVATION_DEG),
                ),
                pc.and_kleene(
                    pc.greater(table["s4_1"], S4_THRESHOLD),
                    pc.greater_equal(table["n_1"], MIN_SAMPLES),
                ),
            ),
        )
        frame = table.filter(pc.fill_null(mask, False)).to_pandas()
        if frame.empty:
            continue
        frame["minbin"] = pd.to_datetime(frame["minbin"], errors="coerce", utc=True)
        frame["s4_1"] = pd.to_numeric(frame["s4_1"], errors="coerce")
        frame["s4_2"] = pd.to_numeric(frame["s4_2"], errors="coerce")
        frame["elev"] = pd.to_numeric(frame["elev"], errors="coerce")
        frame["svid"] = pd.to_numeric(frame["svid"], errors="coerce")
        frame["quality_1"] = pd.to_numeric(frame["quality_1"], errors="coerce")
        valid = (
            frame["minbin"].notna()
            & ~frame["minbin"].dt.strftime("%Y-%m-%d").isin(EXCLUDED_TIMESTAMP_DATES)
        )
        selected = frame.loc[
            valid, ["minbin", "prn", "s4_1", "s4_2", "elev", "quality_1"]
        ].copy()
        if selected.empty:
            continue
        selected["station"] = path.parent.name
        per_minute = (
            selected.groupby(["station", "prn", "minbin"], as_index=False)
            .agg(
                s4_1=("s4_1", "max"),
                s4_2=("s4_2", "max"),
                elevation_deg=("elev", "median"),
                quality_1=("quality_1", lambda values: _quality_string(values)),
            )
        )
        minute_frames.append(per_minute)

    if not minute_frames:
        raise RuntimeError("No candidate minutes matched the configured filters")

    minutes = pd.concat(minute_frames, ignore_index=True)
    minutes = (
        minutes.groupby(["station", "prn", "minbin"], as_index=False)
        .agg(
            s4_1=("s4_1", "max"),
            s4_2=("s4_2", "max"),
            elevation_deg=("elevation_deg", "median"),
            quality_1=("quality_1", lambda values: _merge_quality_strings(values)),
        )
        .sort_values(["station", "prn", "minbin"])
        .reset_index(drop=True)
    )

    events = events_from_minutes(minutes)

    metadata = {
        "source_root": str(data_root),
        "source_file_count": len(files),
        "source_fingerprint": source_fingerprint(files),
        "index_version": INDEX_VERSION,
        "s4_column": "s4_1",
        "s4_threshold": S4_THRESHOLD,
        "minimum_samples": MIN_SAMPLES,
        "minimum_elevation_deg_exclusive": MIN_ELEVATION_DEG,
        "maximum_elevation_deg_inclusive": MAX_ELEVATION_DEG,
        "excluded_timestamp_dates": sorted(EXCLUDED_TIMESTAMP_DATES),
        "excluded_stations": sorted(EXCLUDED_STATIONS),
    }
    count = replace_active_events(events, metadata)
    return {
        "events": count,
        "candidate_minutes": len(minutes),
        "source_files": len(files),
        "source_root": str(data_root),
    }


def events_from_minutes(minutes: pd.DataFrame) -> list[dict[str, object]]:
    """Group adjacent high-S4 minutes independently for each station and PRN."""
    events: list[dict[str, object]] = []
    for (station, prn), group in minutes.groupby(["station", "prn"], sort=True):
        group = group.sort_values("minbin").reset_index(drop=True)
        breaks = group["minbin"].diff().ne(pd.Timedelta(minutes=1)).cumsum()
        for _, event_minutes in group.groupby(breaks, sort=False):
            start = event_minutes["minbin"].iloc[0]
            end = event_minutes["minbin"].iloc[-1] + pd.Timedelta(minutes=1)
            event_id = make_event_id(str(station), str(prn), start, end)
            quality_values = sorted(
                {
                    part
                    for value in event_minutes["quality_1"]
                    for part in str(value).split(",")
                    if part
                }
            )
            peak_s4_2 = event_minutes["s4_2"].max(skipna=True)
            events.append(
                {
                    "event_id": event_id,
                    "station": str(station),
                    "prn": str(prn),
                    "start_utc": start.isoformat(),
                    "end_utc": end.isoformat(),
                    "high_minutes": int(len(event_minutes)),
                    "peak_s4": float(event_minutes["s4_1"].max()),
                    "peak_s4_2": (
                        None if pd.isna(peak_s4_2) else float(peak_s4_2)
                    ),
                    "mean_s4": float(event_minutes["s4_1"].mean()),
                    "min_elevation_deg": float(event_minutes["elevation_deg"].min()),
                    "max_elevation_deg": float(event_minutes["elevation_deg"].max()),
                    "quality_values": ",".join(quality_values),
                }
            )
    return events


def make_event_id(
    station: str, prn: str, start: pd.Timestamp, end: pd.Timestamp
) -> str:
    value = f"{station}|{prn}|{start.isoformat()}|{end.isoformat()}"
    short_hash = hashlib.sha1(value.encode()).hexdigest()[:10]
    return f"{station}-{prn}-{start.strftime('%Y%m%dT%H%M')}-{short_hash}"


def _quality_string(values: pd.Series) -> str:
    numeric = pd.to_numeric(values, errors="coerce").dropna().astype(int)
    return ",".join(str(value) for value in sorted(numeric.unique()))


def _merge_quality_strings(values: pd.Series) -> str:
    combined = {
        part
        for value in values
        for part in str(value).split(",")
        if part and part != "nan"
    }
    return ",".join(sorted(combined))
