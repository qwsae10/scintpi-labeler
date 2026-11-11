"""Offline snapshot: a small, self-contained copy of N events that runs on any laptop.

Built on ganymede with ``scintlab snapshot``. The output folder (and zip) holds:

- the data the tabs need for each event (all satellites +/- 30 min around it,
  the same satellite on the neighbouring days for the sidereal tab, the
  event's 20 Hz window, and the day's minute table for the sky maps), in the
  same layout as the project folder;
- ``events/events.parquet`` with just these events;
- a copy of the ``scintlab`` package and launchers (``run_mac_linux.sh``,
  ``run_windows.bat``) that make a virtual environment, install five
  packages and open the app.

Labels made offline are saved in the snapshot's ``labels/<user>.jsonl``. Event
ids are the same as on ganymede, so that file can be copied into
``/titan/frodrigues/scint_labeler/labels/`` later.
"""

from __future__ import annotations

import json
import logging
import shutil
import zipfile
from collections import defaultdict
from pathlib import Path

import pandas as pd

from scintlab import config, data
from scintlab.events import load_events

logger = logging.getLogger(__name__)

PAD = pd.Timedelta(minutes=30)
SEARCH = pd.Timedelta(seconds=900 + 120)  # sidereal search span of data.geometric_repeat, plus margin

RUN_SH = """#!/bin/bash
# Start the ScintPi labeler on this snapshot. Needs Python 3.10 or newer.
set -e
cd "$(dirname "$0")"
PY=$(command -v python3 || command -v python)
if [ ! -d .venv ]; then
  echo "First run: creating a Python environment (one minute)..."
  "$PY" -m venv .venv
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q numpy "pandas>=2.0" pyarrow pyyaml "flask>=3.0" "plotly>=5.20,<6"
fi
export SCINTLAB_DATA_ROOT="$PWD" PYTHONPATH="$PWD/app"
PORT=${1:-8765}
( sleep 3; (open "http://localhost:$PORT" || xdg-open "http://localhost:$PORT") >/dev/null 2>&1 ) &
exec .venv/bin/python -m scintlab.cli serve --port "$PORT"
"""

RUN_BAT = r"""@echo off
rem Start the ScintPi labeler on this snapshot. Needs Python 3.10 or newer (python.org).
cd /d "%~dp0"
if not exist .venv (
  echo First run: creating a Python environment, one minute...
  python -m venv .venv
  .venv\Scripts\python -m pip install -q --upgrade pip
  .venv\Scripts\python -m pip install -q numpy "pandas>=2.0" pyarrow pyyaml "flask>=3.0" "plotly>=5.20,<6"
)
set SCINTLAB_DATA_ROOT=%CD%
set PYTHONPATH=%CD%\app
start "" http://localhost:8765
.venv\Scripts\python -m scintlab.cli serve --port 8765
"""

README = """ScintPi labeler - offline snapshot of {n} events ({made})

1. Install Python 3.10 or newer if you do not have it (python.org).
2. macOS/Linux: open a terminal in this folder and run   bash run_mac_linux.sh
   Windows: double-click run_windows.bat
   The first start installs a few packages into .venv (needs internet once).
3. The browser opens http://localhost:8765. Press Ctrl-C in the terminal to stop.

Read LABELING_GUIDE.md first. Every label is saved at once in
labels/<your user name>.jsonl. When you are done, send that file back.
"""


def _naive(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["datetime"] = df["datetime"].dt.tz_convert("UTC").dt.tz_localize(None)
    return df


def _split_by_day(df: pd.DataFrame):
    if df.empty:
        return
    for day, g in df.groupby(df["datetime"].dt.floor("D")):
        yield day.strftime("%Y-%m-%d"), g


def make_snapshot(out_dir: str | Path, n: int = 50, elev_mask: float = 10.0, receivers=None,
                  order: str = "strat_rank", make_zip: bool = True) -> dict:
    out = Path(out_dir)
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"{out} exists and is not empty")
    out.mkdir(parents=True, exist_ok=True)
    ev = load_events()
    ev = ev[ev["max_elev"] >= elev_mask]
    if receivers:
        ev = ev[ev["receiver"].isin(receivers)]
    ev = ev.sort_values(order if order in ev else "queue_rank").head(n)
    parts: dict[tuple, list] = defaultdict(list)
    last_day = None

    def add(rx, kind, df):
        if df is None or df.empty:
            return
        for day, g in _split_by_day(_naive(df)):
            parts[(rx, day, kind)].append(g)

    day_minutes_done = set()
    # Day by day, so only a few day files are in memory at once (login nodes allow 8 GB).
    for e in ev.sort_values(["receiver", "date", "start_utc"]).itertuples():
        rx, prn = e.receiver, e.prn
        if (rx, e.date) != last_day:
            data.clear_cache()
            last_day = (rx, e.date)
        t0, t1 = e.start_utc - PAD, e.end_utc + PAD
        add(rx, "second", data.window(rx, "second", t0, t1))
        add(rx, "minute", data.window(rx, "minute", t0.floor("min"), t1))
        add(rx, "highrate", data.window(rx, "highrate", t0, t1, prn))
        for days in (-1, 1):
            shift = pd.Timedelta(seconds=days * data.SIDEREAL_S)
            lo, hi = t0 + shift - SEARCH, t1 + shift + SEARCH
            add(rx, "second", data.window(rx, "second", lo, hi, prn))
            add(rx, "minute", data.window(rx, "minute", lo.floor("min"), hi, prn))
        if (rx, e.date) not in day_minutes_done:
            m = data.day_minutes(rx, e.date)
            if m is not None:
                add(rx, "minute", m)
            day_minutes_done.add((rx, e.date))

    written = defaultdict(int)
    for (rx, day, kind), frames in sorted(parts.items()):
        df = pd.concat(frames, ignore_index=True).drop_duplicates(["prn", "datetime"])
        df = df.sort_values(["prn", "datetime"]).reset_index(drop=True)
        d = out / "products" / rx / day.replace("-", "")
        d.mkdir(parents=True, exist_ok=True)
        df.to_parquet(d / f"{kind}.parquet", index=False, row_group_size=200_000)
        src = data.day_dir(rx, day) / "meta.json"
        meta = json.loads(src.read_text()) if src.exists() else {"receiver": rx, "date": day}
        meta["snapshot"] = True
        (d / "meta.json").write_text(json.dumps(meta, indent=2, default=str))
        written[kind] += len(df)

    root = config.data_root()
    (out / "events").mkdir(exist_ok=True)
    ev.to_parquet(out / "events" / "events.parquet", index=False)
    for rel in ("inventory/receivers.csv", "days.csv", "ref/kp_ap_f107.txt"):
        if (root / rel).exists():
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / rel, out / rel)
    (out / "config.yaml").write_text("labels:\n  reviewers: []\n")

    pkg = Path(__file__).resolve().parent
    shutil.copytree(pkg, out / "app" / "scintlab", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    docs = pkg.parent / "docs" / "labeling_guide.md"
    if docs.exists():
        shutil.copy2(docs, out / "LABELING_GUIDE.md")
    (out / "run_mac_linux.sh").write_text(RUN_SH)
    (out / "run_mac_linux.sh").chmod(0o755)
    (out / "run_windows.bat").write_text(RUN_BAT.replace("\n", "\r\n"))
    (out / "README.txt").write_text(README.format(n=len(ev), made=pd.Timestamp.utcnow().strftime("%Y-%m-%d")))

    result = {"events": int(len(ev)), "receivers": sorted(ev["receiver"].unique().tolist()),
              "rows": dict(written), "folder": str(out)}
    if make_zip:
        zpath = out.with_suffix(".zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(out.rglob("*")):
                if p.is_file():
                    info = zipfile.ZipInfo.from_file(p, Path(out.name) / p.relative_to(out))
                    if p.name.endswith(".sh"):
                        info.external_attr = 0o755 << 16
                    with open(p, "rb") as f:
                        zf.writestr(info, f.read(), zipfile.ZIP_DEFLATED)
        result["zip"] = str(zpath)
        result["zip_mb"] = round(zpath.stat().st_size / 1e6, 1)
    return result
