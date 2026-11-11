# ScintPi disturbance labeler

Local review of continuous high-S4 periods in the Mother’s Day one-second
dataset. Source Parquet files are read-only. Candidate events and manual
decisions are stored separately under `state/`.

## Candidate definition

- Raw `s4_1 > 0.2`.
- Elevation strictly above 30° and no higher than 90°.
- `svid = 255` is excluded.
- The confirmed May 5 timestamp errors inside the MOS/UTD May 11 files are
  excluded.
- Consecutive one-minute bins for the same station and PRN form one event.
- No below-threshold or missing minute is bridged.
- Events are joined across daily-file boundaries.

Quality flags are displayed but do not remove candidates. Each plot includes
20 minutes before and after the event. The ten comparison satellites are those
with the most SNR1 samples in that full window. S4 remains visible even when a
quality flag is raised so the trigger can be reviewed; sigma-phi is shown only
for quality value 0 to prevent failed phase estimates from dominating its axis.
Carrier-phase STEC is plotted relative to its minimum in the displayed window.

## Start

Double-click `run_labeler.command`, or run:

```bash
/Users/isaac/opt/miniconda3/envs/default_env/bin/python \
  /Users/isaac/Documents/ML_Analysis_2026/manual_labeler/app.py serve
```

Open `http://127.0.0.1:8765`. The first start scans the 24 source files and
creates the event index; later starts reuse it unless the source files or index
rules change.

Keyboard controls: `1` Scintillation, `2` Multipath, `3` RFI, `4` Other,
`S` skip, left/right arrows navigate. Decisions are committed to SQLite after
every action.

## Maintenance commands

```bash
# Force a fresh candidate scan
/Users/isaac/opt/miniconda3/envs/default_env/bin/python app.py build-index --force

# Print progress
/Users/isaac/opt/miniconda3/envs/default_env/bin/python app.py stats

# Export without opening the browser
/Users/isaac/opt/miniconda3/envs/default_env/bin/python app.py export
```

No classifier or saved model is imported or executed anywhere in this app.
