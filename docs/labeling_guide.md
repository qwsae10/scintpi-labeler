# Labeling guide

## What you are doing

ScintPi receivers measure GNSS signal strength (C/N₀) and carrier phase 20–50
times per second. **S4** is the normalized standard deviation of signal
intensity over one minute. S4 above 0.2 means the signal fluctuated strongly.
There are three common causes:

- **Ionospheric scintillation**: small-scale irregularities in the ionosphere
  (often plasma bubbles after sunset at low latitudes, or storm-time
  structures at mid latitudes) scatter the signal. This is what we want to
  find.
- **Multipath**: the signal also reaches the antenna after reflecting off the
  ground, roofs or walls, and the two copies interfere.
- **Radio-frequency interference (RFI)**: another transmitter or a local
  electronic device raises the noise floor.

The app shows one *event* at a time: one satellite at one receiver, for a
stretch of minutes when S4 > 0.2. Gaps of up to 5 quiet minutes are included in
the event, so one disturbance is one decision. You pick the cause. Your labels become a
reference ("verification") dataset for testing automatic classifiers, so a
careful "Unsure" is worth more than a confident guess.

## The four labels

| Key | Label | Use when |
|---|---|---|
| 1 | **Scintillation** | the evidence points to the ionosphere (checklist below) |
| 2 | **Multipath** | the evidence points to reflections |
| 3 | **RFI** | many satellites drop at the same moment, regardless of sky position |
| 4 | **Unsure/Artifact** | data problems (gaps, loss of lock, cycle-slip garbage, jumps), a mix you cannot separate, or you cannot tell |

Then set **confidence**: **High** (`H`) if you would bet on it, **Low** (`L`)
otherwise. Tick **Save for reviewer** (`R`) when you want Isaac to look at it,
for example an interesting or confusing case. Notes are optional but help a
lot ("L1 and L2 fade together; repeats yesterday").

Pressing a number saves and moves to the next event. **Skip** (`S`) leaves it
for later. Nothing is lost if you close the browser. Labeled events stay in the
queue (greyed, with a dot in their class colour), so `←` goes back to one you
want to change, and `U` jumps to the last one you labeled. `N` writes a note
(`Enter` returns to the keys).

Switch tabs with `Tab` / `Shift+Tab`, jump back to the previous tab with `` ` ``,
or go to one directly with `Shift+1`… The Overview, 20 Hz, Other satellites and
Sidereal tabs share one time window: zoom in on a fade in one and the others
show the same minutes.

The calendar above the queue list shows which days have events. A deeper blue
day has more left to do, and a tick means every event of that day is decided.
Click a day to limit the queue to it (shown by receiver and time, unless an
Order is picked in the filters), `Shift`-click a second day for a range,
and click it again (or "All days") to go back. `[` and `]` move to the
previous or next day with events left. The filters fold away under
**Filters**.

## How to decide: the evidence, tab by tab

### Overview

- **C/N₀**: scintillation is *fast*, deep, irregular fading (several dB within
  a second), and it starts and stops over minutes. Multipath fading is usually
  *slower* and more regular (quasi-periodic, tens of seconds to minutes per
  cycle), strongest at low elevation, and changes smoothly as the satellite
  moves.
- **Elevation**: below about 20° multipath is common. Above 40° it is rare,
  unless the antenna sits near a wall or metal object (look at the sky map).
- **S4 on both frequencies**: scintillation usually raises S4 on L1 *and* L2
  (L2 often more). Multipath can be very different on the two frequencies.
- **σφ** (bottom panel; phase scintillation, receiver clock removed): high σφ
  together with high S4 points to the ionosphere. Grey open circles are
  flagged values (near a gap or arc edge): ignore them. The axis is scaled to
  the good values, so flagged points may sit above the top.
- **τ** (optional panel, turn on in Settings; the 1/e point of the intensity autocorrelation):
  scintillation gives short τ, typically 0.1–1 s. Multipath gives long τ
  (several seconds or undefined), because it varies slowly.
- **TEC**: a dip or wiggle in TEC around the event often goes with plasma
  bubbles.

### 20 Hz

The raw C/N₀ and phase at full rate. Scintillation looks like noise-like
fading with deep, sharp drops. Multipath looks like smooth oscillations.
Red ✕ marks are cycle slips. Many slips, or phase that jumps by whole cycles,
means the phase data are damaged. Judge from C/N₀ instead, or use
Unsure/Artifact.

### Other satellites

- The heat map shows S4 of every satellite above the elevation mask. Ionospheric
  structures usually affect **several satellites** in the same part of the sky,
  sometimes one after another as the structure drifts.
- **One satellite alone at low elevation**, with every other satellite quiet,
  is usually multipath.
- **Every satellite dropping at the same moment** in the C/N₀ panel (and the
  bar chart jumping) is RFI or a receiver problem, not the ionosphere.

### Sidereal repeat

GPS satellites pass the same point of the sky about 4 minutes earlier each
day. Multipath depends on that geometry, so **the same fading pattern repeats
on the neighbouring day**. Scintillation does not repeat. If the coloured
curves (day before/after) match the black one, choose Multipath. The tab
reports when a satellite has no daily repeat (Galileo, GLONASS and most
BeiDou satellites); then use the other evidence.

### Sky maps

Each dot is one satellite-minute of the day. Multipath shows up as patches
fixed in azimuth and elevation, usually near the horizon or toward buildings,
which every satellite crossing that patch shares. Scintillation fills large
regions at certain times (for example after local sunset) and does not
stick to fixed directions. The red line is the event.

### Network and Kp (optional tabs, turn on in Settings)

Storms (Kp ≥ 5, orange/red bars) make mid-latitude scintillation possible.
Simultaneous high S4 at nearby receivers supports the ionosphere.

## Messy data ("it all looks garbled")

Much of the data near the horizon or from older receivers looks noisy. Some
rules:

1. **Zoom in.** Pinch (or `⌘`-scroll) on the plot to zoom at the cursor, swipe
   sideways to pan, and double-click or press `0` to reset; `Shift`-scroll moves
   a panel up or down and `⌘Shift`-scroll stretches it. A
   20-minute window of 20 Hz data looks like noise at full width. Zoomed to a
   minute, fading versus oscillation is usually clear.
2. **Change the window.** The "Window ±" box sets the context (1–30 min).
   More context helps judge whether the event is part of a long pass with the
   same behaviour.
3. **ScintPi 2 receivers** record C/N₀ only in whole dB-Hz steps, on L1 only,
   with no phase. Small steady flicker between neighbouring integers (for
   example 39↔42 dB-Hz) is measurement noise. If that alone made S4 cross 0.2,
   choose Unsure/Artifact with High confidence.
4. **Data quality filters** (under Filters) hide events with too little to
   judge: by default fewer than 2 candidate minutes or less than 80 % of the
   event's seconds with L1 data. You can also hide events whose data stops at
   their edge (loss of lock), or ask for both frequencies or TEC. The header
   shows each event's coverage (L1 · 2nd band · TEC) and longest gap. A loss of
   lock during strong fading can itself be scintillation, so it is shown, not
   hidden, by default.
5. **Raise the elevation mask** in the queue filter (e.g. 20° or 30°) to work
   on clearer events first. Come back to the low ones later.
6. **Part of the event is different?** Drag across the plot over that part and
   press `1`–`4` for its class (`Esc` cancels). Click a marked range to select
   it: a number changes its class, `Delete` removes it, and its edges can be
   dragged. Ranges snap to whole minutes inside the shaded event and are saved
   with the event label, which then applies to the remaining minutes.
7. **Not sure? Say so.** Unsure/Artifact with a note is a useful label. A
   wrong confident label damages the dataset.

## Working habits

- The queue is in a fixed *stratified* random order: it cycles through groups
  of events (receiver type × S4 level × elevation), so you see strong and weak,
  high and low events in turn. Label in that order so the labeled set stays a
  sample we can reweight. Use filters for practice, not for cherry-picking.
- Take breaks. Speed matters less than consistency.
- If something in the app is broken or confusing, write it in the notes and
  tick Save for reviewer, or tell Isaac.
