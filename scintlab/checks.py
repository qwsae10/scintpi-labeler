"""Consistency checks between receivers.

``check_time_offset`` tests the ScintPi 2 time-scale assumption (GPS time of day
minus 18 s) against a co-located ScintPi 3, whose time comes from scintkit:
both report whole-degree elevations from u-blox, so the seconds at which a
satellite's integer elevation changes should coincide. The median difference
of those transition times is the clock offset between the two products.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def elevation_transitions(second: pd.DataFrame) -> pd.DataFrame:
    s = second.dropna(subset=["elev"]).sort_values(["prn", "datetime"])
    change = s.groupby("prn")["elev"].diff().fillna(0).ne(0)
    t = s.loc[change, ["prn", "datetime", "elev"]]
    return t


def check_time_offset(second_a: pd.DataFrame, second_b: pd.DataFrame, max_lag_s: float = 60) -> dict:
    """Median (b - a) time difference of matching elevation transitions, in seconds."""
    ta, tb = elevation_transitions(second_a), elevation_transitions(second_b)
    diffs = []
    for (prn, el), g in ta.groupby(["prn", "elev"]):
        h = tb[(tb["prn"] == prn) & (tb["elev"] == el)]
        if h.empty:
            continue
        for t in g["datetime"]:
            d = (h["datetime"] - t).dt.total_seconds()
            d = d[d.abs() <= max_lag_s]
            if len(d):
                diffs.append(float(d.iloc[d.abs().argmin()]))
    diffs = np.array(diffs)
    if not len(diffs):
        return {"n": 0}
    return {"n": int(len(diffs)), "median_s": float(np.median(diffs)),
            "iqr_s": [float(np.percentile(diffs, 25)), float(np.percentile(diffs, 75))]}
