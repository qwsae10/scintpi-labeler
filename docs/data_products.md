# Data products

All files live under `/titan/frodrigues/scint_labeler`. Times are UTC.
Channel suffix `1`, `2`, `3` = receiver channel. For ScintPi 3:
1 = GPS L1 C/A, Galileo E1, BeiDou B1I, GLONASS L1; 2 = L2C / E5b / B2I / L2.
For ScintPi 4: 1 = L1/E1/B1I, 2 = L2C/E5a/B3I/L2, 3 = L5/E5b/B2a/L3. ScintPi 2 has
channel 1 only (L1, integer dB-Hz). `sig_*` columns hold the signal names.

## `products/<receiver>/<YYYYMMDD>/minute.parquet`

One row per satellite and minute with any data.

| Column | Unit | Meaning |
|---|---|---|
| `datetime`, `prn`, `cons` | | minute start, RINEX satellite label, constellation |
| `elev`, `azim` | deg | first sample of the minute (receiver, or SP3 for ScintPi 4 without SatVisibility1) |
| `snr*_mean` | dB-Hz | mean C/N₀ over the minute |
| `s4_*` | | S4: std/mean of linear C/N₀ over the minute (scintkit) |
| `s4_corrected_*` | | S4 with receiver noise removed (Van Dierendonck et al. 1993) |
| `s4_detrended_*` | | S4 after removing a 10 s moving average (ScintPi 3/4) |
| `sigma_phi_*` | rad | std of the clock-removed, 0.1 Hz high-passed carrier phase (ScintPi 3/4) |
| `tau1e_*` | s | lag where the autocorrelation of linear C/N₀ first drops below 1/e (scintlab) |
| `tau_*` | s | same at the 0.5 level (scintkit) |
| `n_s4_*`, `n_sigphi_*`, `n_cycleslip_*` | | sample and cycle-slip counts in the minute |
| `s4_quality_flag_*` | | 1 = fewer than 80 % of the expected samples |
| `sigma_phi_quality_flag_*` | | 1 = within 180 s of an arc edge or gap, too many dropped samples, or GLONASS |

## `second.parquet`

First sample of every satellite and second: `datetime, prn, elev, azim,
snr1, snr2, (snr3), tec_cph12` (TECU, carrier-phase TEC leveled to code TEC,
biases not removed).

## `highrate.parquet`

Full-rate samples (20 Hz ScintPi 2/3, 50 Hz ScintPi 4) of satellites with
candidate minutes, from 30 min before their first to 30 min after their last
candidate minute: `datetime, prn, snr*` (dB-Hz), `ph*` (rad, detrended phase
with the receiver clock removed), `slip*` (cycle slip detected at that sample).

## `meta.json`

Receiver, type, date, sampling rate, raw files used, candidate count,
the candidate and processing settings, scintkit version, run time.

## `events/events.parquet`

| Column | Meaning |
|---|---|
| `event_id` | `<receiver>_<prn>_<start YYYYMMDDTHHMM>_<hash>`, stable while the rule is unchanged |
| `receiver, rx_type, date, day_reason, prn, constellation` | |
| `start_utc, end_utc, n_minutes` | first candidate minute, end of last candidate minute |
| `peak_s4, mean_s4, peak_s4_other` | S4 on the trigger channel; peak S4 on the other channel |
| `min_elev, max_elev, mean_azim` | over the candidate minutes |
| `latitude, longitude, local_hour` | receiver position; local solar time at start |
| `has_phase` | false for ScintPi 2 |
| `queue_rank` | seeded random order for the default queue |

## `labels/<user>.jsonl`

Append-only, one JSON object per action. The latest `label`/`skip`/`clear`
line per event is the user's decision.

```json
{"event_id": "TE-HO3_G04_20241010T0142_1a2b3c4d", "user": "abc123456", "time_utc": "2026-11-12T15:03:11+00:00",
 "action": "label", "label": "Scintillation", "confidence": "High", "for_review": false,
 "notes": "L1 and L2 fade together, E09 nearby too",
 "segments": [{"start": "2024-10-10T01:55:00Z", "end": "2024-10-10T01:58:00Z", "label": "Unsure/Artifact"}],
 "elevation_mask_deg": 10, "app_version": "0.1.0", "seconds_on_event": 41}
```

Reviewer decisions are `"action": "review"` lines with `final_label`.

## `exports/`

`labels_<time>.csv|parquet`: one row per event and labeler, with event fields,
the decision and `final_label`. `minute_labels_<time>.parquet`: one row per
candidate minute, with segment labels applied.
