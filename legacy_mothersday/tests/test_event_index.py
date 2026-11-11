from __future__ import annotations

import pandas as pd

from event_index import events_from_minutes, make_event_id


def test_event_id_is_stable_and_specific() -> None:
    start = pd.Timestamp("2024-05-10T06:00:00Z")
    end = pd.Timestamp("2024-05-10T06:03:00Z")
    first = make_event_id("COL", "G12", start, end)
    assert first == make_event_id("COL", "G12", start, end)
    assert first != make_event_id("COL", "G13", start, end)
    assert first.startswith("COL-G12-20240510T0600-")


def test_adjacent_minutes_group_but_one_minute_gap_breaks_event() -> None:
    minutes = pd.DataFrame(
        {
            "station": ["COL"] * 4 + ["MOS"],
            "prn": ["G12"] * 5,
            "minbin": pd.to_datetime(
                [
                    "2024-05-10T23:59:00Z",
                    "2024-05-11T00:00:00Z",
                    "2024-05-11T00:02:00Z",
                    "2024-05-11T00:03:00Z",
                    "2024-05-11T00:00:00Z",
                ],
                utc=True,
            ),
            "s4_1": [0.3, 0.4, 0.5, 0.6, 0.7],
            "s4_2": [0.2, 0.3, 0.4, 0.55, float("nan")],
            "elevation_deg": [30, 31, 32, 33, 45],
            "quality_1": ["0", "0", "1", "1", "0"],
        }
    )
    events = events_from_minutes(minutes)
    assert [(event["station"], event["high_minutes"]) for event in events] == [
        ("COL", 2),
        ("COL", 2),
        ("MOS", 1),
    ]
    assert events[0]["start_utc"] == "2024-05-10T23:59:00+00:00"
    assert events[0]["end_utc"] == "2024-05-11T00:01:00+00:00"
    assert events[1]["quality_values"] == "1"
    assert events[1]["peak_s4_2"] == 0.55
    assert events[2]["peak_s4_2"] is None
