"""SQLite persistence for candidate events and manual reviews."""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

import pandas as pd

from config import DATABASE_PATH, EXPORT_DIR, STATE_DIR, VALID_CONFIDENCE, VALID_LABELS


EVENT_COLUMNS = (
    "event_id",
    "station",
    "prn",
    "start_utc",
    "end_utc",
    "high_minutes",
    "peak_s4",
    "peak_s4_2",
    "mean_s4",
    "min_elevation_deg",
    "max_elevation_deg",
    "quality_values",
    "active",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path = DATABASE_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    return connection


def initialize_database(path: Path = DATABASE_PATH) -> None:
    with connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                station TEXT NOT NULL,
                prn TEXT NOT NULL,
                start_utc TEXT NOT NULL,
                end_utc TEXT NOT NULL,
                high_minutes INTEGER NOT NULL CHECK (high_minutes > 0),
                peak_s4 REAL NOT NULL,
                peak_s4_2 REAL,
                mean_s4 REAL NOT NULL,
                min_elevation_deg REAL NOT NULL,
                max_elevation_deg REAL NOT NULL,
                quality_values TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
            );

            CREATE TABLE IF NOT EXISTS reviews (
                event_id TEXT PRIMARY KEY REFERENCES events(event_id),
                state TEXT NOT NULL CHECK (state IN ('labeled', 'skipped')),
                label TEXT CHECK (label IN ('Scintillation', 'Multipath', 'RFI', 'Other')),
                confidence TEXT CHECK (confidence IN ('High', 'Medium', 'Low')),
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS review_history (
                history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL REFERENCES events(event_id),
                state TEXT NOT NULL,
                label TEXT,
                confidence TEXT,
                notes TEXT NOT NULL,
                saved_at TEXT NOT NULL,
                revision INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_events_active_time
                ON events(active, start_utc, station, prn);
            CREATE INDEX IF NOT EXISTS idx_events_station_time
                ON events(station, start_utc);
            CREATE INDEX IF NOT EXISTS idx_events_prn_time
                ON events(prn, start_utc);
            CREATE INDEX IF NOT EXISTS idx_events_peak_s4
                ON events(peak_s4);
            CREATE INDEX IF NOT EXISTS idx_reviews_state_label
                ON reviews(state, label);
            CREATE INDEX IF NOT EXISTS idx_review_history_event
                ON review_history(event_id, history_id);
            """
        )
        event_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(events)")
        }
        if "peak_s4_2" not in event_columns:
            connection.execute("ALTER TABLE events ADD COLUMN peak_s4_2 REAL")
        connection.execute("PRAGMA optimize")


def set_metadata(connection: sqlite3.Connection, key: str, value: object) -> None:
    serialized = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    connection.execute(
        """
        INSERT INTO metadata(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, serialized),
    )


def get_metadata(key: str, path: Path = DATABASE_PATH) -> str | None:
    initialize_database(path)
    with connect(path) as connection:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
    return None if row is None else str(row["value"])


def replace_active_events(
    events: Iterable[dict[str, object]],
    metadata: dict[str, object],
    path: Path = DATABASE_PATH,
) -> int:
    initialize_database(path)
    rows = list(events)
    with connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE events SET active = 0")
        connection.executemany(
            """
            INSERT INTO events(
                event_id, station, prn, start_utc, end_utc, high_minutes,
                peak_s4, peak_s4_2, mean_s4, min_elevation_deg, max_elevation_deg,
                quality_values, active
            ) VALUES (
                :event_id, :station, :prn, :start_utc, :end_utc, :high_minutes,
                :peak_s4, :peak_s4_2, :mean_s4, :min_elevation_deg, :max_elevation_deg,
                :quality_values, 1
            )
            ON CONFLICT(event_id) DO UPDATE SET
                station = excluded.station,
                prn = excluded.prn,
                start_utc = excluded.start_utc,
                end_utc = excluded.end_utc,
                high_minutes = excluded.high_minutes,
                peak_s4 = excluded.peak_s4,
                peak_s4_2 = excluded.peak_s4_2,
                mean_s4 = excluded.mean_s4,
                min_elevation_deg = excluded.min_elevation_deg,
                max_elevation_deg = excluded.max_elevation_deg,
                quality_values = excluded.quality_values,
                active = 1
            """,
            rows,
        )
        for key, value in metadata.items():
            set_metadata(connection, key, value)
        set_metadata(connection, "event_count", len(rows))
        set_metadata(connection, "indexed_at", utc_now())
        connection.commit()
    return len(rows)


def save_review(
    event_id: str,
    *,
    state: str,
    label: str | None,
    confidence: str | None,
    notes: str,
    path: Path = DATABASE_PATH,
) -> dict[str, object]:
    if state not in {"labeled", "skipped"}:
        raise ValueError("state must be labeled or skipped")
    if state == "labeled" and label not in VALID_LABELS:
        raise ValueError(f"label must be one of {VALID_LABELS}")
    if state == "skipped":
        label = None
        confidence = None
    elif confidence is not None and confidence not in VALID_CONFIDENCE:
        raise ValueError(f"confidence must be one of {VALID_CONFIDENCE}")

    notes = notes.strip()
    timestamp = utc_now()
    initialize_database(path)
    with connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute(
            "SELECT 1 FROM events WHERE event_id = ? AND active = 1", (event_id,)
        ).fetchone() is None:
            raise KeyError(f"unknown event: {event_id}")
        current = connection.execute(
            "SELECT created_at, revision FROM reviews WHERE event_id = ?", (event_id,)
        ).fetchone()
        created_at = timestamp if current is None else current["created_at"]
        revision = 1 if current is None else int(current["revision"]) + 1
        connection.execute(
            """
            INSERT INTO reviews(
                event_id, state, label, confidence, notes,
                created_at, updated_at, revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                state = excluded.state,
                label = excluded.label,
                confidence = excluded.confidence,
                notes = excluded.notes,
                updated_at = excluded.updated_at,
                revision = excluded.revision
            """,
            (
                event_id,
                state,
                label,
                confidence,
                notes,
                created_at,
                timestamp,
                revision,
            ),
        )
        connection.execute(
            """
            INSERT INTO review_history(
                event_id, state, label, confidence, notes, saved_at, revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (event_id, state, label, confidence, notes, timestamp, revision),
        )
        connection.commit()

    return {
        "event_id": event_id,
        "state": state,
        "label": label,
        "confidence": confidence,
        "notes": notes,
        "created_at": created_at,
        "updated_at": timestamp,
        "revision": revision,
    }


def get_event(event_id: str, path: Path = DATABASE_PATH) -> dict[str, object] | None:
    initialize_database(path)
    with connect(path) as connection:
        row = connection.execute(
            """
            SELECT e.*,
                   COALESCE(r.state, 'unlabeled') AS review_state,
                   r.label, r.confidence, r.notes, r.created_at,
                   r.updated_at, r.revision
            FROM events e
            LEFT JOIN reviews r ON r.event_id = e.event_id
            WHERE e.event_id = ? AND e.active = 1
            """,
            (event_id,),
        ).fetchone()
    return None if row is None else dict(row)


def list_events(
    *,
    station: str | None = None,
    prn: str | None = None,
    date: str | None = None,
    status: str = "unlabeled",
    min_s4: float | None = None,
    max_s4: float | None = None,
    path: Path = DATABASE_PATH,
) -> list[dict[str, object]]:
    initialize_database(path)
    conditions = ["e.active = 1"]
    parameters: list[object] = []
    if station:
        conditions.append("e.station = ?")
        parameters.append(station)
    if prn:
        conditions.append("UPPER(e.prn) LIKE ?")
        parameters.append(f"%{prn.upper()}%")
    if date:
        conditions.append("substr(e.start_utc, 1, 10) = ?")
        parameters.append(date)
    if min_s4 is not None:
        conditions.append("e.peak_s4 >= ?")
        parameters.append(min_s4)
    if max_s4 is not None:
        conditions.append("e.peak_s4 <= ?")
        parameters.append(max_s4)

    if status == "unlabeled":
        conditions.append("r.event_id IS NULL")
    elif status == "labeled":
        conditions.append("r.state = 'labeled'")
    elif status == "skipped":
        conditions.append("r.state = 'skipped'")
    elif status in VALID_LABELS:
        conditions.append("r.label = ?")
        parameters.append(status)
    elif status != "all":
        raise ValueError("invalid status filter")

    query = f"""
        SELECT e.*,
               COALESCE(r.state, 'unlabeled') AS review_state,
               r.label, r.confidence, r.notes, r.updated_at, r.revision
        FROM events e
        LEFT JOIN reviews r ON r.event_id = e.event_id
        WHERE {' AND '.join(conditions)}
        ORDER BY e.start_utc, e.station, e.prn, e.event_id
    """
    with connect(path) as connection:
        return [dict(row) for row in connection.execute(query, parameters).fetchall()]


def summary(path: Path = DATABASE_PATH) -> dict[str, object]:
    initialize_database(path)
    with connect(path) as connection:
        totals = dict(
            connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN r.state = 'labeled' THEN 1 ELSE 0 END) AS labeled,
                    SUM(CASE WHEN r.state = 'skipped' THEN 1 ELSE 0 END) AS skipped,
                    SUM(CASE WHEN r.event_id IS NULL THEN 1 ELSE 0 END) AS unlabeled
                FROM events e
                LEFT JOIN reviews r ON r.event_id = e.event_id
                WHERE e.active = 1
                """
            ).fetchone()
        )
        stations = [
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT station FROM events WHERE active = 1 ORDER BY station"
            )
        ]
        dates = [
            row[0]
            for row in connection.execute(
                """
                SELECT DISTINCT substr(start_utc, 1, 10)
                FROM events WHERE active = 1 ORDER BY 1
                """
            )
        ]
        label_counts = {
            row["label"]: row["count"]
            for row in connection.execute(
                """
                SELECT label, COUNT(*) AS count
                FROM reviews r JOIN events e USING(event_id)
                WHERE e.active = 1 AND r.state = 'labeled'
                GROUP BY label ORDER BY label
                """
            )
        }
    return {
        **{key: int(value or 0) for key, value in totals.items()},
        "stations": stations,
        "dates": dates,
        "label_counts": label_counts,
    }


def export_reviews(
    export_dir: Path = EXPORT_DIR, path: Path = DATABASE_PATH
) -> dict[str, object]:
    export_dir.mkdir(parents=True, exist_ok=True)
    initialize_database(path)
    query = """
        SELECT
            e.event_id, e.station, e.prn, e.start_utc, e.end_utc,
            e.high_minutes, e.peak_s4, e.peak_s4_2, e.mean_s4,
            e.min_elevation_deg, e.max_elevation_deg, e.quality_values,
            r.state AS review_state, r.label, r.confidence, r.notes,
            r.created_at, r.updated_at, r.revision
        FROM reviews r
        JOIN events e USING(event_id)
        WHERE e.active = 1
        ORDER BY e.start_utc, e.station, e.prn
    """
    with connect(path) as connection:
        frame = pd.read_sql_query(query, connection)

    csv_path = export_dir / "manual_event_labels.csv"
    parquet_path = export_dir / "manual_event_labels.parquet"
    _atomic_csv(frame, csv_path)
    _atomic_parquet(frame, parquet_path)
    return {
        "rows": len(frame),
        "csv": str(csv_path),
        "parquet": str(parquet_path),
        "exported_at": utc_now(),
    }


def _atomic_csv(frame: pd.DataFrame, destination: Path) -> None:
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.stem}-", suffix=".tmp", dir=destination.parent
    )
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        frame.to_csv(temporary, index=False, quoting=csv.QUOTE_MINIMAL)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_parquet(frame: pd.DataFrame, destination: Path) -> None:
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.stem}-", suffix=".tmp", dir=destination.parent
    )
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        frame.to_parquet(temporary, index=False, compression="zstd")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
