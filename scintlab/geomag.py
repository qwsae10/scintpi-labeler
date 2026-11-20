"""Magnetic dip latitude of the receivers, for the event header.

Dip latitude d = atan(tan(I) / 2), with I the inclination of the IGRF field at SHELL_KM above the
site (the usual thin-shell height at low latitudes, as the storm-day dashboard uses). Low-latitude
scintillation is ordered by it, not by geographic latitude: the anomaly crests sit near ±15-20°
dip, and the dip equator crosses South America near 10° S geographic.

Needs ``ppigrf`` (pip install ppigrf); without it the column is NaN and the header shows "–".
The field changes ~0.1° a year, so one value per receiver and month is plenty.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

SHELL_KM = 350.0


def dip_latitudes(ev: pd.DataFrame, h: float = SHELL_KM) -> pd.Series:
    """Dip latitude (degrees) of each event's receiver, from its latitude/longitude and date."""
    out = pd.Series(np.nan, index=ev.index, dtype=float)
    if not len(ev) or not {"latitude", "longitude", "start_utc"} <= set(ev.columns):
        return out
    try:
        import ppigrf
    except ImportError:
        return out
    month = pd.to_datetime(ev["start_utc"], utc=True).dt.strftime("%Y-%m")
    key = pd.DataFrame({"lat": ev["latitude"].astype(float), "lon": ev["longitude"].astype(float), "month": month})
    ok = key["lat"].notna() & key["lon"].notna() & ~((key["lat"] == 0) & (key["lon"] == 0))
    sites = key[ok].drop_duplicates()
    vals = {}
    for m, g in sites.groupby("month"):  # one IGRF call per month, all sites at once
        when = (pd.Timestamp(m + "-01") + pd.Timedelta(days=14)).to_pydatetime()
        be, bn, bu = ppigrf.igrf(g["lon"].to_numpy(), g["lat"].to_numpy(), h, when)
        be, bn, bu = (np.asarray(x, dtype=float).ravel() for x in (be, bn, bu))
        incl = np.arctan2(-bu, np.hypot(be, bn))
        for (lat, lon), d in zip(g[["lat", "lon"]].itertuples(index=False), np.degrees(np.arctan(np.tan(incl) / 2))):
            vals[(lat, lon, m)] = float(d)
    out[ok] = [vals.get((r.lat, r.lon, r.month), np.nan) for r in key[ok].itertuples(index=False)]
    return out
