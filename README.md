# scintpi-labeler

A browser tool for labeling high-S4 events in ScintPi data, to build a
verification dataset for scintillation classifiers.

Every satellite-minute with **S4 > 0.2 above 5° elevation** is a candidate.
Candidate minutes of one receiver and satellite, at most 5 minutes apart,
form an *event*.
A labeler marks each event as **Scintillation**, **Multipath**, **RFI** or
**Unsure/Artifact**, with **High** or **Low** confidence, and can save hard
cases for a reviewer. Part of an event can carry its own label.

The data come from about a week of days per receiver from 43 ScintPi 2, 3 and
4 receivers, low and mid latitude. The days include the 2024-05-10/11 and
2024-10-10/11 storms. They are processed from the raw 20–50 Hz files on
ganymede2.

| Tab | Shows |
|---|---|
| Overview | 1 s C/N₀ on every frequency; elevation and azimuth; 1-min S4; relative slant TEC; σφ (receiver clock removed) last. Optional in Settings: τ (1/e decorrelation time) and noise-corrected S4 |
| 20 Hz | full-rate C/N₀ and clock-removed detrended carrier phase per frequency, with cycle slips |
| Other satellites | C/N₀ of the best-covered other satellites; S4 of every satellite as a heat map; how many satellites exceed S4 0.2 each minute |
| Sidereal repeat | the same satellite on the day before and after, at the same azimuth and elevation (multipath repeats, scintillation does not) |
| Sky maps | polar maps of S4, σφ, τ and C/N₀ for every satellite-minute of the day, with the event track |
| Network *(off by default)* | highest S4 at the nearest other receivers |
| Kp / F10.7 *(off by default)* | geomagnetic and solar activity around the event |

Each user picks tabs, window length, elevation mask and other options on the
Settings page.

## For labelers

1. [docs/connecting.md](docs/connecting.md): get access and open the app (one
   command).
2. [docs/labeling_guide.md](docs/labeling_guide.md): how to decide, with
   examples of each class and of messy data.

You can stop at any time. Every key press is saved on ganymede immediately,
and the queue resumes where you left off.

## Offline snapshot (no ganymede account needed)

Download the [50-event demo ZIP](https://github.com/qwsae10/scintpi-labeler/releases/download/offline-demo-2026-11-11/scintpi-demo-50.zip)
from the public GitHub release. Extract
it, open its folder and run `bash run_mac_linux.sh` on macOS/Linux or
double-click `run_windows.bat` on Windows. You need Python 3.10+ and an
internet connection for the first launch. After setup it works offline.

Already cloned this repository? The data are not in the repository; they are
in that ZIP. Extract it anywhere, then from the clone:

```bash
python -m pip install -e .
SCINTLAB_DATA_ROOT=/path/to/scintpi-demo-50 scintlab serve      # macOS/Linux
```

On Windows PowerShell: `$env:SCINTLAB_DATA_ROOT="C:\path\to\scintpi-demo-50"; scintlab serve`.
Open http://localhost:8765. `SCINTLAB_DATA_ROOT` always names the data folder;
on ganymede it defaults to `/titan/frodrigues/scint_labeler`.

To build a fresh demo on Ganymede, `scintlab snapshot --n 50` writes 50 events, a copy of the app
and launchers to `/titan/frodrigues/scint_labeler/snapshots/<name>.zip`
(size depends on the selected events). Copy the zip to any laptop with Python 3.10+, unzip it and
run `bash run_mac_linux.sh` (or double-click `run_windows.bat` on Windows).
Labels are saved in the snapshot's `labels/<user>.jsonl` with the same event
ids as on ganymede, so that file can be copied into the shared `labels/`
folder. Details: [docs/pipeline.md](docs/pipeline.md#offline-snapshots).

## For maintainers

- [docs/pipeline.md](docs/pipeline.md): raw archive → chosen days →
  processed products → events → labels → export.
- [docs/data_products.md](docs/data_products.md): file and column reference.

```
scintlab/             Python package
  inventory.py        scan the raw archive, assign receivers
  select_days.py      choose receiver-days (forced storms, Kp storms, bubble season, random)
  sc2.py              ScintPi 2 raw reader (scintkit has none)
  orbits.py           SP3 elevation/azimuth for ScintPi 4 files without it
  process.py          raw -> minute / second / 20 Hz products per receiver-day
  events.py           candidate events
  labels.py           append-only per-user label logs, settings, export
  figures.py          one Plotly figure per tab
  app.py, templates/, static/   Flask app
  default_config.yaml every threshold and default
cluster/              SLURM scripts and serve.sh
docs/
tests/
legacy_mothersday/    the earlier single-dataset labeler (Mother's Day 2024, 1 s files)
```

Development install (laptop):

```bash
git clone https://github.com/qwsae10/scintpi-labeler.git
cd scintpi-labeler
python -m pip install -e ".[dev]"
pytest
```

To run the app on a laptop against a copied subset, set
`SCINTLAB_DATA_ROOT=/path/to/copy` (same layout as
`/titan/frodrigues/scint_labeler`) and run `scintlab serve`.
