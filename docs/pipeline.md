# Data pipeline (for whoever maintains the dataset)

Everything runs on ganymede2 from `/titan/frodrigues/scint_labeler`:

```
/titan/frodrigues/scint_labeler/
  code/        git clone of this repository (pull, never edit here)
  env/         Python 3.11 env: scintkit (GitHub main), flask, plotly, pyyaml
  inventory/   files.parquet, receivers.csv, coverage.parquet (raw archive scan)
  ref/         kp_ap_f107.txt (GFZ Kp, ap, F10.7)
  days.csv     the chosen receiver-days and why each was chosen
  products/<receiver>/<YYYYMMDD>/   minute, second, highrate parquet + meta.json
  events/      events.parquet (and dated copies of earlier indexes)
  labels/      <user>.jsonl label logs, settings/<user>.json
  exports/     merged label tables written by `scintlab export`
  logs/        SLURM and setup logs
```

Commands below use `S=/titan/frodrigues/scint_labeler/env/bin/scintlab` and run
from `/titan/frodrigues/scint_labeler`.

## 1. Update the code

Commit and push from a laptop (to GitHub and to the `ganymede` remote,
`/titan/frodrigues/git/scintpi-labeler.git`), then on ganymede:

```bash
git -C code pull --ff-only
```

## 2. Inventory the raw archive

```bash
$S inventory
```

This scans `/mfs/io/groups/uars/scintpi/sc*` for ScintPi 2/3 files
(`scintpi{2,3}_YYYYMMDD_HHMM_<lon>_<lat>_v<fw>.bin[.zip]`) and ScintPi 4 SBF files
(`scNNN/YYYYMMDD/<prefix><doy><h><mm>.<yy>_`). It also assigns each file a
receiver code from scintkit's station registry, by coordinates (ScintPi 2/3) or
by SBF prefix (ScintPi 4). It takes about 20 minutes.

- Receivers are often tested at UTD before deployment, so their first files
  carry Dallas coordinates and match US-TX1. Each receiver keeps only the
  files in its main station folder.
- Receivers missing from the registry get `<folder>-<type>` codes (for
  example `sc015-sc2`, the Taiwan ScintPi 2). They are kept only if they have
  nonzero coordinates, at least 60 files, and no larger same-type receiver
  within 3 km.
- `sc004` (solar receiver) and `solarScintPi` are excluded in the config.

## 3. Choose days

```bash
$S fetch-indices            # GFZ Kp/F10.7 into ref/
$S select-days              # writes days.csv (refuses to overwrite without --overwrite)
```

Each receiver gets 8 days, as consecutive pairs (the sidereal-repeat tab needs
the neighbouring day):

1. 2024-05-10/11 and 2024-10-10/11 when the receiver has data (`forced`).
2. Low-latitude receivers (|dipole magnetic latitude| < 30°) get two pairs in
   their sector's plasma-bubble season with F10.7 ≥ 120 (`season`) and one
   pair with daily max Kp ≥ 6 (`storm`). Mid-latitude receivers get two storm
   pairs.
3. Random pairs fill the rest (`random`).

The draw is seeded (`sampling.seed`). A day is eligible with at least 20 h of
nominal file coverage. The first run gave 342 days on 43 receivers.

## 4. Process

```bash
N=$(($(wc -l < days.csv) - 1))
sbatch -p rodrigues -c 2 --mem=40G --array=0-$((N-1))%12 -J scintlab code/cluster/process_days.sbatch $N
```

One task per day. Finished days (with `meta.json`) are skipped on reruns. To
redo one day:
`sbatch -p rodrigues -c 2 --mem=40G code/cluster/process_one.sbatch TE-HO3 2024-10-10 --overwrite`.

Per receiver-day:

- **ScintPi 3 and 4**: scintkit's chain (raw → lvl0 → receiver epochs →
  signals → cycle-slip repair → 6th-order 0.1 Hz Butterworth high-pass →
  receiver clock = median over satellites above 10° → minute products). Files
  are joined into blocks (4 h ScintPi 3, 2 h ScintPi 4) before filtering, so
  15-minute SBF files do not get a filter edge every 15 minutes.
- **ScintPi 4** needs `bin2asc` from `module load rxtools/25.0.0` (Apptainer).
  The batch scripts load it. Stations whose SBF files lack the SatVisibility1
  block get elevation and azimuth from TECbase SP3 orbits
  (`/titan/frodrigues/tecbase/nav`) before the clock estimate.
- **ScintPi 2**: `scintlab.sc2` reads the 20-byte records (scintkit has no
  ScintPi 2 reader). L1 C/N₀ only, so S4, τ and C/N₀ but no phase, σφ, L2 or TEC.
- **τ (tau1e)**: first lag where the normalized autocorrelation of the linear
  intensity 10^(C/N₀/10) falls below 1/e, linearly interpolated between lags,
  per satellite-minute with at least 50 s of samples. scintkit's own `tau`
  (0.5 level) is also stored.
- **Candidates**: S4 on channel 1 > 0.2, elevation > 5°, S4 quality flag 0.
  The full-rate C/N₀ and clock-removed phase of candidate satellites are
  kept from 30 min before to 30 min after their candidate minutes.

## 5. Build the event index

```bash
$S build-events
```

Consecutive candidate minutes of one receiver and satellite form an event.
Each event gets a stable random `queue_rank` and a `stratum` (receiver type ×
peak-S4 bin 0.2/0.3/0.5 × max-elevation bin 10/20/30°). The default queue
order (`strat_rank`) takes one event from each stratum in turn. Weak,
low-elevation events (about 70% of all events) therefore do not use up the
labeling time, and the export carries `stratum_weight` = events in stratum /
events labeled in stratum, for unbiased class fractions and scores. Rebuilding keeps the old index as
`events_<time>.parquet`. Event ids depend only on receiver, satellite, start
and end, so labels stay attached as long as the candidate rule is unchanged.
After rebuilding, press nothing: running apps reload on restart (or POST
`/api/reload`).

## 6. Progress and export

```bash
$S stats            # progress for the current user
$S export           # exports/labels_<time>.csv|parquet and minute_labels_<time>.parquet
```

The export has one row per event and labeler. `final_label` is the reviewer's
label when there is one, otherwise the labeler's. The minute table expands
every event into minutes and applies segment labels, for scoring a
per-minute classifier.

## Configuration

`scintlab/default_config.yaml` holds every threshold. Override keys in
`/titan/frodrigues/scint_labeler/config.yaml` (shared) or a file named by
`$SCINTLAB_CONFIG`. `$SCINTLAB_DATA_ROOT` points the tools at another project
folder (for example a local copy for development).

## Known limitations

- The ScintPi 2 time stamp is taken as UTC time of day. This was checked
  against the co-located ScintPi 3 at Tegucigalpa: subtracting 18 leap seconds
  left the ScintPi 2 data about 18 s early (see `scintlab/sc2.py`).
- SP3 geometry exists only up to the last TECbase orbit day (2026-10-25 at
  setup). Later ScintPi 4 days without SatVisibility1 have no elevation and
  therefore no candidates.
- The high-pass filter is causal and scintkit flags σφ within 180 s of an arc
  edge or gap. Flagged σφ is drawn as grey open circles.
- GLONASS σφ is always flagged by scintkit.

## Adding a labeler

1. They need a ganymede2 account and membership in the `uars` group
   (`/titan/frodrigues` is `drwxrws--- root:uars`). Ask CIRC or a `uars`
   group admin to add them.
2. `labels/` and `labels/settings/` are setgid and group-writable (`2775`),
   so their label file is created on first save.
3. The GitHub repository is private. Add them as a collaborator (read
   access) so they can read the docs.
4. Send them `docs/connecting.md` and `docs/labeling_guide.md`.
5. To make someone a reviewer, add their NetID to `labels.reviewers` in
   `/titan/frodrigues/scint_labeler/config.yaml`.
