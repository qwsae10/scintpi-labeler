from __future__ import annotations

from pathlib import Path

from database import get_event, list_events, replace_active_events, save_review, summary


def event() -> dict[str, object]:
    return {
        "event_id": "COL-G12-test",
        "station": "COL",
        "prn": "G12",
        "start_utc": "2024-05-10T06:00:00+00:00",
        "end_utc": "2024-05-10T06:03:00+00:00",
        "high_minutes": 3,
        "peak_s4": 0.61,
        "peak_s4_2": 0.48,
        "mean_s4": 0.42,
        "min_elevation_deg": 33.0,
        "max_elevation_deg": 35.0,
        "quality_values": "0,1",
    }


def test_review_round_trip_and_revision_history(tmp_path: Path) -> None:
    database = tmp_path / "labels.sqlite3"
    replace_active_events([event()], {"test": True}, path=database)
    assert summary(path=database)["unlabeled"] == 1

    first = save_review(
        "COL-G12-test",
        state="labeled",
        label="Scintillation",
        confidence="High",
        notes="clear amplitude fading",
        path=database,
    )
    assert first["revision"] == 1
    assert summary(path=database)["labeled"] == 1
    assert list_events(status="Scintillation", path=database)[0]["prn"] == "G12"

    second = save_review(
        "COL-G12-test",
        state="labeled",
        label="RFI",
        confidence="Medium",
        notes="rechecked comparison satellites",
        path=database,
    )
    assert second["revision"] == 2
    assert get_event("COL-G12-test", path=database)["label"] == "RFI"


def test_skip_is_distinct_from_unlabeled(tmp_path: Path) -> None:
    database = tmp_path / "labels.sqlite3"
    replace_active_events([event()], {}, path=database)
    save_review(
        "COL-G12-test",
        state="skipped",
        label=None,
        confidence=None,
        notes="",
        path=database,
    )
    assert list_events(status="unlabeled", path=database) == []
    assert len(list_events(status="skipped", path=database)) == 1
