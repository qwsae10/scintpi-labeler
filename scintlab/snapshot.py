"""Offline snapshot: a small, self-contained copy of N events that runs on any laptop.

Built on ganymede with ``scintlab snapshot``. The output folder (and zip) holds:

- the data the tabs need for each event (all satellites +/- 30 min around it,
  the same satellite on the neighbouring days for the sidereal tab, the
  event's 20 Hz window, and the day's minute table for the sky maps), in the
  same layout as the project folder;
- ``events/events.parquet`` with just these events;
- a copy of the ``scintlab`` package and launchers (``run_mac_linux.sh``,
  ``run_windows.bat``) that make a virtual environment, install the required
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
import pyarrow.dataset as ds
import pyarrow.parquet as pq

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
fi
if [ ! -f .venv/scintlab-ready ]; then
  .venv/bin/python -m pip install -r requirements.txt
  touch .venv/scintlab-ready
fi
export SCINTLAB_DATA_ROOT="$PWD" PYTHONPATH="$PWD/app"
PORT=${1:-8765}
exec .venv/bin/python -m scintlab.cli serve --port "$PORT" --open-browser
"""

RUN_BAT = r"""@echo off
rem Start the ScintPi labeler on this snapshot. Needs Python 3.10 or newer (python.org).
cd /d "%~dp0"
if not exist .venv (
  echo First run: creating a Python environment, one minute...
  python -m venv .venv || exit /b 1
)
if not exist .venv\scintlab-ready (
  .venv\Scripts\python -m pip install -r requirements.txt || exit /b 1
  type nul > .venv\scintlab-ready
)
set SCINTLAB_DATA_ROOT=%CD%
set PYTHONPATH=%CD%\app
.venv\Scripts\python -m scintlab.cli serve --port 8765 --open-browser
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


def _product_windows(events):
    """Group half-open UTC windows by source file so overlap never duplicates a row."""
    windows = defaultdict(list)

    def add(rx, kind, start, stop, prn=None):
        for day in pd.date_range(start.floor("D"), stop.ceil("D"), freq="D", inclusive="left"):
            windows[rx, day.strftime("%Y-%m-%d"), kind].append((start, stop, prn))

    for e in events.itertuples():
        t0, t1 = e.start_utc - PAD, e.end_utc + PAD
        add(e.receiver, "second", t0, t1)
        add(e.receiver, "highrate", t0, t1, e.prn)
        add(e.receiver, "minute", t0.floor("min"), t1)
        day = pd.Timestamp(e.date, tz="UTC")
        add(e.receiver, "minute", day, day + pd.Timedelta(days=1))
        for direction in (-1, 1):
            shift = pd.Timedelta(seconds=direction * data.SIDEREAL_S)
            lo, hi = t0 + shift - SEARCH, t1 + shift + SEARCH
            add(e.receiver, "second", lo, hi, e.prn)
            add(e.receiver, "minute", lo.floor("min"), hi, e.prn)
    return windows


def _copy_windows(src, dest, windows):
    query = None
    for start, stop, prn in dict.fromkeys(windows):
        stamp = ds.field("datetime")
        within = (stamp >= start.tz_convert(None)) & (stamp < stop.tz_convert(None))
        if prn is not None:
            within = within & (ds.field("prn") == prn)
        query = within if query is None else query | within

    source = ds.dataset(src, format="parquet")
    scanner = source.scanner(filter=query, batch_size=65536, use_threads=False,
                             batch_readahead=0, fragment_readahead=0)
    rows = 0
    with pq.ParquetWriter(dest, source.schema) as writer:
        for batch in scanner.to_batches():
            if batch.num_rows:
                writer.write_batch(batch)
                rows += batch.num_rows
    if not rows:
        dest.unlink()
    return rows


def make_snapshot(out_dir: str | Path, n: int = 50, elev_mask: float = 10.0, receivers=None,
                  order: str = "strat_rank", make_zip: bool = True) -> dict:
    out = Path(out_dir)
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"{out} exists and is not empty")
    out.mkdir(parents=True, exist_ok=True)
    if n < 1:
        raise ValueError("Snapshot size must be positive")
    ev = load_events()
    ev = ev[ev["max_elev"] >= elev_mask]
    if receivers:
        ev = ev[ev["receiver"].isin(receivers)]
    ev = ev.sort_values(order if order in ev else "queue_rank").head(n)
    if ev.empty:
        raise ValueError("No events match the snapshot selection")

    written = defaultdict(int)
    for (rx, day, kind), windows in sorted(_product_windows(ev).items()):
        src_dir = data.day_dir(rx, day)
        src = src_dir / f"{kind}.parquet"
        if not src.is_file():
            continue
        dest = out / "products" / rx / day.replace("-", "")
        dest.mkdir(parents=True, exist_ok=True)
        rows = _copy_windows(src, dest / src.name, windows)
        if rows:
            written[kind] += rows
            meta_path = src_dir / "meta.json"
            meta = json.loads(meta_path.read_text()) if meta_path.exists() else {"receiver": rx, "date": day}
            meta["snapshot"] = True
            (dest / "meta.json").write_text(json.dumps(meta, indent=2, default=str))

    root = config.data_root()
    (out / "events").mkdir(exist_ok=True)
    ev.to_parquet(out / "events" / "events.parquet", index=False)
    for rel in ("inventory/receivers.csv", "days.csv", "ref/kp_ap_f107.txt"):
        if (root / rel).exists():
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / rel, out / rel)
    (out / "config.yaml").write_text("labels:\n  reviewers: []\n")

    pkg = Path(__file__).resolve().parent
    shutil.copytree(pkg, out / "app" / "scintlab", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
    docs = pkg.parent / "docs" / "labeling_guide.md"
    if docs.exists():
        shutil.copy2(docs, out / "LABELING_GUIDE.md")
    (out / "requirements.txt").write_text("numpy\npandas>=2,<3\npyarrow\npyyaml\nflask>=3\nplotly>=5.20,<6\n")
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
                    zf.write(p, Path(out.name) / p.relative_to(out))
        result["zip"] = str(zpath)
        result["zip_mb"] = round(zpath.stat().st_size / 1e6, 1)
    return result
