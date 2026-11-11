"""Reader for ScintPi 2 raw files (firmware v224, v224d, v226f).

scintkit has no ScintPi 2 reader. The layout below was read from the bytes of
``scintpi2_20230902_1800_96.75728W_32.99184N_v226f.bin`` (2026-11-11):
20-byte little-endian records, one per satellite and epoch, at 20 Hz.

| bytes | type | field |
|---|---|---|
| 0-3   | uint32 | time of day, ms (wraps at midnight) |
| 4-7   | uint32 | millisecond part of the time of day |
| 8-9   | uint16 | satellite number (u-blox numbering inside the constellation) |
| 10    | uint8  | u-blox gnssId: 0 GPS, 1 SBAS, 2 Galileo, 3 BeiDou, 5 QZSS, 6 GLONASS |
| 11    | uint8  | C/N0, dB-Hz (integer; 0 = not tracked) |
| 12-15 | int32  | elevation, deg |
| 16-19 | int32  | azimuth, deg |

ScintPi 2 logs only C/N0 of one signal (L1), so it has no carrier phase,
no second frequency, no sigma-phi and no TEC.

Time scale: the time of day is UTC. Checked 2026-11-11 against the co-located
ScintPi 3 TE-HO3 (500 m away) on 2024-10-10: cross-correlating 20 Hz C/N0
during scintillation (C35, E03, G02, G09, R14; peak r 0.2-0.5) put the ScintPi 2
stamps 13-28 s early, median about 18 s, when 18 leap seconds were subtracted.
No satellite matched near zero lag, so no leap-second correction is applied.
"""

from __future__ import annotations

import os
import zipfile

import numpy as np
import pandas as pd

RECORD = np.dtype([
    ("tod_ms", "<u4"), ("ms", "<u4"), ("svid", "<u2"), ("gnss", "u1"), ("cno", "u1"),
    ("elev", "<i4"), ("azim", "<i4"),
])
LEAP_SECONDS = 0  # time of day is already UTC (see module docstring)
GNSS = {0: ("GPS", "G"), 1: ("SBAS", "S"), 2: ("GAL", "E"), 3: ("BDS", "C"), 5: ("QZSS", "J"), 6: ("GLO", "R")}
SIGNAL = {"GPS": "GPS_L1CA", "SBAS": "SBAS_L1", "GAL": "GAL_E1", "BDS": "BDS_B1I", "QZSS": "QZSS_L1CA",
          "GLO": "GLO_L1"}


def _buffer(path: str) -> bytes:
    if path.endswith(".zip"):
        with zipfile.ZipFile(path) as zf:
            return zf.read(zf.namelist()[0])
    with open(path, "rb") as f:
        return f.read()


def read_sc2(path: str | os.PathLike, file_date: str | None = None) -> pd.DataFrame:
    """Raw ScintPi 2 file -> frame with ``datetime`` (UTC), ``prn``, ``cons``, ``elev``, ``azim``, ``snr1``, ``sig_1``.

    ``file_date`` (YYYY-MM-DD) is the UTC date of the first record; by default it
    is taken from the file name. A drop in the time of day marks midnight.
    """
    path = os.fspath(path)
    buf = _buffer(path)
    n = len(buf) // RECORD.itemsize
    rec = np.frombuffer(buf[: n * RECORD.itemsize], dtype=RECORD)
    if file_date is None:
        stamp = os.path.basename(path).split("_")[1]
        file_date = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
    tod = rec["tod_ms"].astype(np.int64)
    ok = (tod >= 0) & (tod < 86_400_000) & np.isin(rec["gnss"], list(GNSS))
    rec, tod = rec[ok], tod[ok]
    # Day rollover: the time of day drops by more than 12 h.
    day = np.concatenate([[0], np.cumsum(np.diff(tod) < -43_200_000)])
    base = pd.Timestamp(file_date)
    # The file starts at its name's time; a first record just before midnight
    # (GPS time ahead of UTC) belongs to the previous day.
    if tod[0] > 86_400_000 - 3_600_000 and os.path.basename(path).split("_")[2].startswith("00"):
        base -= pd.Timedelta(days=1)
    t = base + pd.to_timedelta(tod + day * 86_400_000 - LEAP_SECONDS * 1000, unit="ms")

    gnss = rec["gnss"]
    svid = rec["svid"].astype(np.int64)
    letters = np.vectorize(lambda g: GNSS[g][1])(gnss) if len(gnss) else np.array([], dtype=str)
    number = np.where(gnss == 1, svid - 100, svid)
    prn = pd.Series(letters).str.cat(pd.Series(number).astype(str).str.zfill(2))
    cons = pd.Series(gnss).map(lambda g: GNSS[g][0])
    snr = rec["cno"].astype(np.float32)
    snr[snr == 0] = np.nan
    elev = rec["elev"].astype(np.float32)
    azim = rec["azim"].astype(np.float32)
    elev[(elev < -90) | (elev > 90)] = np.nan
    azim[(azim < 0) | (azim > 360)] = np.nan
    df = pd.DataFrame({
        "datetime": t, "prn": prn.to_numpy(), "cons": cons.to_numpy(), "svid": svid,
        "elev": elev, "azim": azim, "snr1": snr,
    })
    # GLONASS with unknown slot (u-blox svid 255) and implausible numbers are dropped.
    df = df[~((df["cons"] == "GLO") & (df["svid"] > 32)) & (number > 0)]
    df["sig_1"] = df["cons"].map(SIGNAL)
    return df.reset_index(drop=True)
