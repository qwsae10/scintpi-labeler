"""Plotly figures for the app tabs. Each function returns a figure as a plain dict."""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from scintlab import config, data

CH_COLORS = {1: "#2f6fed", 2: "#e1802f", 3: "#2e9b75"}
CH_DASH = {1: "solid", 2: "solid", 3: "dot"}
SHADE = "#f5b942"
GRID = "rgba(93,108,128,0.15)"
COMP_COLORS = ["#2f6fed", "#23a7a0", "#8a63d2", "#e17b3a", "#cf4d6f", "#5d8f3f", "#8d6e63", "#0086b3",
               "#aa6d00", "#667085", "#b5487d", "#3c7d9e"]


class Missing(Exception):
    """No data for this view; the message is shown to the user."""


def _times(event: dict, settings: dict, pad: float | None = None):
    pad = float(settings.get("pad_minutes", 20) if pad is None else pad)
    start, end = pd.Timestamp(event["start_utc"]), pd.Timestamp(event["end_utc"])
    return start, end, start - pd.Timedelta(minutes=pad), end + pd.Timedelta(minutes=pad)


def _channels(df: pd.DataFrame, prefix: str) -> list[int]:
    return [i for i in (1, 2, 3) if f"{prefix}{i}" in df.columns and df[f"{prefix}{i}"].notna().any()]


def _signal_names(minute: pd.DataFrame) -> dict[int, str]:
    names = {}
    for i in (1, 2, 3):
        col = f"sig_{i}"
        if col in minute and minute[col].notna().any():
            names[i] = str(minute[col].dropna().mode().iloc[0]).replace("_", " ")
        else:
            names[i] = f"ch{i}"
    return names


def _layout(fig, height, start, end, t0, t1, rows, title_rows=None):
    for r in range(1, rows + 1):
        fig.add_vrect(x0=start, x1=end, fillcolor=SHADE, opacity=0.16, line_width=0, row=r, col=1)
    fig.update_xaxes(range=[t0, t1], showgrid=True, gridcolor=GRID, matches="x")
    fig.update_yaxes(showgrid=True, gridcolor=GRID, zeroline=False)
    fig.update_xaxes(title_text="Time (UTC)", row=rows, col=1)
    fig.update_layout(
        height=height, margin={"l": 70, "r": 60, "t": 40, "b": 45}, paper_bgcolor="white", plot_bgcolor="white",
        font={"family": "Inter, system-ui, sans-serif", "size": 12}, hovermode="x unified",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.01, "x": 0, "font": {"size": 11}},
        uirevision="keep",
    )
    for a in fig.layout.annotations:
        if a.text and not a.text.startswith("<"):
            a.font = {"size": 12, "color": "#344054"}
            a.x, a.xanchor = 0, "left"
    return fig.to_plotly_json()


def _line(fig, x, y, name, row, color, dash="solid", width=1.4, secondary=False, show=True, mode="lines",
          gl=True, unit=""):
    cls = go.Scattergl if gl else go.Scatter
    fig.add_trace(cls(x=x, y=y, mode=mode, name=name, showlegend=show, line={"color": color, "width": width,
                                                                               "dash": dash},
                      marker={"size": 4, "color": color},
                      hovertemplate=f"{name} %{{y:.3f}}{unit}<extra></extra>"),
                  row=row, col=1, secondary_y=secondary if secondary else None)


# --------------------------------------------------------------------------- overview

def overview(event: dict, settings: dict) -> dict:
    start, end, t0, t1 = _times(event, settings)
    rx, prn = event["receiver"], event["prn"]
    sec = data.window(rx, "second", t0, t1, prn)
    mins = data.window(rx, "minute", t0.floor("min"), t1, prn)
    if sec.empty and mins.empty:
        raise Missing("No data for this satellite in the window.")
    names = _signal_names(mins)
    mask = float(settings.get("elevation_mask_deg", 10))
    thr = float(config.load()["candidates"]["s4_threshold"])
    fig = make_subplots(rows=6, cols=1, shared_xaxes=True, vertical_spacing=0.035,
                        row_heights=[0.2, 0.13, 0.17, 0.17, 0.17, 0.16],
                        specs=[[{}], [{"secondary_y": True}], [{}], [{}], [{}], [{}]],
                        subplot_titles=(f"C/N₀ · {rx} {prn} (1 s)", "Elevation and azimuth",
                                        "S4 (1 min)", "σφ, clock removed (1 min; grey = quality flag)",
                                        "τ, 1/e decorrelation time (1 min)", "Relative slant TEC (carrier phase)"))
    for i in _channels(sec, "snr"):
        _line(fig, sec["datetime"], sec[f"snr{i}"], f"C/N₀ {names[i]}", 1, CH_COLORS[i], unit=" dB-Hz")
    _line(fig, sec["datetime"], sec["elev"], "Elevation", 2, "#d94a5a", width=1.8)
    _line(fig, sec["datetime"], sec["azim"], "Azimuth", 2, "#7c5ce5", secondary=True)
    fig.add_hline(y=mask, line_dash="dot", line_color="#d94a5a", line_width=1, row=2, col=1)
    for i in _channels(mins, "s4_"):
        _line(fig, mins["datetime"], mins[f"s4_{i}"], f"S4 {names[i]}", 3, CH_COLORS[i], mode="lines+markers",
              gl=False)
    fig.add_hline(y=thr, line_dash="dash", line_color="#c23b4a", line_width=1.2, row=3, col=1)
    for i in _channels(mins, "sigma_phi_"):
        good = mins[f"sigma_phi_quality_flag_{i}"].eq(0) if f"sigma_phi_quality_flag_{i}" in mins else True
        _line(fig, mins["datetime"], mins[f"sigma_phi_{i}"].where(good), f"σφ {names[i]}", 4, CH_COLORS[i],
              mode="lines+markers", gl=False, unit=" rad")
        bad = mins[f"sigma_phi_{i}"].where(~good)
        if bad.notna().any():
            fig.add_trace(go.Scatter(x=mins["datetime"], y=bad, mode="markers", name=f"σφ {names[i]} flagged",
                                     marker={"size": 5, "color": "#98a2b3", "symbol": "circle-open"},
                                     showlegend=False, hovertemplate="flagged σφ %{y:.3f}<extra></extra>"),
                          row=4, col=1)
    for i in _channels(mins, "tau1e_"):
        _line(fig, mins["datetime"], mins[f"tau1e_{i}"], f"τ {names[i]}", 5, CH_COLORS[i], mode="lines+markers",
              gl=False, unit=" s")
    if "tec_cph12" in sec and sec["tec_cph12"].notna().any():
        tec = sec["tec_cph12"] - sec["tec_cph12"].min()
        _line(fig, sec["datetime"], tec, "STEC", 6, "#1d2939", unit=" TECU")
    fig.update_yaxes(title_text="dB-Hz", row=1, col=1)
    fig.update_yaxes(title_text="Elev (°)", range=[0, 92], row=2, col=1, secondary_y=False)
    fig.update_yaxes(title_text="Azim (°)", range=[0, 360], row=2, col=1, secondary_y=True, showgrid=False)
    fig.update_yaxes(title_text="S4", rangemode="tozero", row=3, col=1)
    fig.update_yaxes(title_text="rad", rangemode="tozero", row=4, col=1)
    fig.update_yaxes(title_text="s", rangemode="tozero", row=5, col=1)
    fig.update_yaxes(title_text="TECU", row=6, col=1)
    if event.get("rx_type") == "sc2":
        fig.add_annotation(text="ScintPi 2: L1 C/N₀ only (no phase, L2 or TEC)", xref="paper", yref="paper",
                           x=1, y=1.04, xanchor="right", showarrow=False, font={"color": "#b54708"})
    return _layout(fig, 1150, start, end, t0, t1, 6)


# --------------------------------------------------------------------------- high rate

def highrate(event: dict, settings: dict) -> dict:
    start, end, t0, t1 = _times(event, settings)
    rx, prn = event["receiver"], event["prn"]
    hr = data.window(rx, "highrate", t0, t1, prn)
    if hr.empty:
        raise Missing("No 20 Hz data stored for this window.")
    step = max(1, int(round(20 / float(settings.get("highrate_decimate_hz", 20)))))
    hr = hr.iloc[::step]
    mins = data.window(rx, "minute", t0.floor("min"), t1, prn)
    names = _signal_names(mins)
    snr_ch, ph_ch = _channels(hr, "snr"), _channels(hr, "ph")
    rows = len(snr_ch) + len(ph_ch)
    titles = [f"C/N₀ {names[i]} (full rate)" for i in snr_ch] + \
             [f"Detrended phase, receiver clock removed, {names[i]} (0.1 Hz high-pass)" for i in ph_ch]
    fig = make_subplots(rows=rows, cols=1, shared_xaxes=True, vertical_spacing=0.04, subplot_titles=titles)
    r = 1
    for i in snr_ch:
        _line(fig, hr["datetime"], hr[f"snr{i}"], f"C/N₀ {names[i]}", r, CH_COLORS[i], width=0.8, unit=" dB-Hz")
        fig.update_yaxes(title_text="dB-Hz", row=r, col=1)
        r += 1
    for i in ph_ch:
        _line(fig, hr["datetime"], hr[f"ph{i}"], f"φ {names[i]}", r, CH_COLORS[i], width=0.8, unit=" rad")
        slip = hr.get(f"slip{i}")
        if slip is not None and slip.fillna(False).astype(bool).any():
            s = hr[slip.fillna(False).astype(bool)]
            fig.add_trace(go.Scattergl(x=s["datetime"], y=s[f"ph{i}"], mode="markers", name=f"cycle slip {names[i]}",
                                       marker={"color": "#d92d20", "size": 7, "symbol": "x"}), row=r, col=1)
        fig.update_yaxes(title_text="rad", row=r, col=1)
        r += 1
    return _layout(fig, 230 * rows + 80, start, end, t0, t1, rows)


# --------------------------------------------------------------------------- other satellites

def satellites(event: dict, settings: dict) -> dict:
    start, end, t0, t1 = _times(event, settings)
    rx, prn = event["receiver"], event["prn"]
    sec = data.window(rx, "second", t0, t1)
    mins = data.window(rx, "minute", t0.floor("min"), t1)
    if sec.empty:
        raise Missing("No other satellites in the window.")
    mask = float(settings.get("elevation_mask_deg", 10))
    thr = float(config.load()["candidates"]["s4_threshold"])
    n = int(settings.get("comparison_satellites", 10))
    others = sec[sec["prn"].ne(prn) & sec["snr1"].notna() & sec["elev"].gt(mask)]
    comp = others.groupby("prn")["datetime"].nunique().sort_values(ascending=False).head(n).index.tolist()
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.06, row_heights=[0.42, 0.38, 0.2],
                        subplot_titles=(f"C/N₀ L1 of the {len(comp)} best-covered other satellites above {mask:g}° "
                                        f"(target in black)",
                                        "S4 L1 of every satellite above the elevation mask",
                                        f"Satellites with S4 L1 > {thr:g}"))
    for k, p in enumerate(comp):
        s = sec[sec["prn"] == p]
        _line(fig, s["datetime"], s["snr1"], p, 1, COMP_COLORS[k % len(COMP_COLORS)], width=1.0, show=True,
              unit=" dB-Hz")
    tgt = sec[sec["prn"] == prn]
    _line(fig, tgt["datetime"], tgt["snr1"], f"{prn} (target)", 1, "#101828", width=1.8, unit=" dB-Hz")
    m = mins[mins["elev"] > mask]
    if len(m) and "s4_1" in m:
        piv = m.pivot_table(index="prn", columns="datetime", values="s4_1", aggfunc="first")
        piv = piv.loc[sorted(piv.index, key=lambda p: (p != prn, p))]
        fig.add_trace(go.Heatmap(z=piv.to_numpy(), x=piv.columns, y=piv.index, zmin=0, zmax=0.6,
                                 colorscale="Viridis", colorbar={"title": "S4", "len": 0.35, "y": 0.45},
                                 hovertemplate="%{y} %{x|%H:%M} S4 %{z:.2f}<extra></extra>"), row=2, col=1)
        count = m.assign(hi=m["s4_1"] > thr).groupby("datetime")["hi"].sum()
        fig.add_trace(go.Bar(x=count.index, y=count.values, name="N sats S4>thr", marker_color="#667085",
                             showlegend=False), row=3, col=1)
    fig.update_yaxes(title_text="dB-Hz", row=1, col=1)
    fig.update_yaxes(title_text="", row=2, col=1, tickfont={"size": 9})
    fig.update_yaxes(title_text="count", row=3, col=1)
    return _layout(fig, 980, start, end, t0, t1, 3)


# --------------------------------------------------------------------------- sidereal repeat

def sidereal(event: dict, settings: dict) -> dict:
    start, end, t0, t1 = _times(event, settings)
    rx, prn = event["receiver"], event["prn"]
    sec = data.window(rx, "second", t0, t1, prn)
    mins = data.window(rx, "minute", t0.floor("min"), t1, prn)
    if sec.empty:
        raise Missing("No data for this satellite in the window.")
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.06,
                        subplot_titles=("C/N₀ L1: this day and the same sky track on the neighbouring day(s)",
                                        "S4 L1", "Elevation"))
    _line(fig, sec["datetime"], sec["snr1"], "this day", 1, "#101828", width=1.6, unit=" dB-Hz")
    _line(fig, mins["datetime"], mins.get("s4_1"), "this day", 2, "#101828", mode="lines+markers", gl=False,
          show=False)
    _line(fig, sec["datetime"], sec["elev"], "this day", 3, "#101828", show=False)
    notes = []
    for days, color, label in ((-1, "#e1802f", "day before"), (1, "#2f6fed", "day after")):
        rep = data.geometric_repeat(rx, prn, t0, t1, days)
        if rep is None:
            notes.append(f"{label}: no data")
            continue
        shift, mis = rep
        if mis > 2.0:
            notes.append(f"{label}: no repeat of this sky track (best match {mis:.1f}° off)")
            continue
        dt = pd.Timedelta(seconds=shift)
        o = data.window(rx, "second", t0 + dt, t1 + dt, prn)
        om = data.window(rx, "minute", (t0 + dt).floor("min"), t1 + dt, prn)
        notes.append(f"{label}: shifted {shift / 3600:+.3f} h, track matches within {mis:.1f}°")
        _line(fig, o["datetime"] - dt, o["snr1"], label, 1, color, width=1.1, unit=" dB-Hz")
        if "s4_1" in om:
            _line(fig, om["datetime"] - dt, om["s4_1"], label, 2, color, mode="lines+markers", gl=False, show=False)
        _line(fig, o["datetime"] - dt, o["elev"], label, 3, color, show=False)
    fig.add_annotation(text="<br>".join(notes), xref="paper", yref="paper", x=1, y=1.07, xanchor="right",
                       showarrow=False, align="right", font={"size": 11, "color": "#475467"})
    fig.update_yaxes(title_text="dB-Hz", row=1, col=1)
    fig.update_yaxes(title_text="S4", rangemode="tozero", row=2, col=1)
    fig.update_yaxes(title_text="Elev (°)", row=3, col=1)
    out = _layout(fig, 820, start, end, t0, t1, 3)
    out["layout"]["margin"]["t"] = 80
    return out


# --------------------------------------------------------------------------- skyplot

SKY_INDICES = [("s4_1", "S4 L1", 0, 0.6, "Viridis"), ("sigma_phi_1", "σφ L1 (rad)", 0, 0.5, "Plasma"),
               ("tau1e_1", "τ L1 (s)", 0, 3, "Cividis_r"), ("snr1_mean", "C/N₀ L1 (dB-Hz)", 25, 50, "Greys_r")]


def skyplot(event: dict, settings: dict) -> dict:
    rx = event["receiver"]
    day = data.day_minutes(rx, event["date"])
    if day is None:
        raise Missing("No minute products for this day.")
    mask = float(settings.get("elevation_mask_deg", 10))
    day = day[day["elev"] > mask]
    hours = settings.get("skyplot_hours")
    start, end = pd.Timestamp(event["start_utc"]), pd.Timestamp(event["end_utc"])
    if hours:
        h = pd.Timedelta(hours=float(hours))
        day = day[(day["datetime"] >= start - h) & (day["datetime"] < end + h)]
    fig = make_subplots(rows=2, cols=2, specs=[[{"type": "polar"}] * 2] * 2, horizontal_spacing=0.1,
                        vertical_spacing=0.1, subplot_titles=[s[1] for s in SKY_INDICES])
    trk = day[(day["prn"] == event["prn"]) & (day["datetime"] >= start) & (day["datetime"] < end)]
    for k, (col, title, lo, hi, cmap) in enumerate(SKY_INDICES):
        r, c = k // 2 + 1, k % 2 + 1
        polar = "polar" if k == 0 else f"polar{k + 1}"
        if col not in day or day[col].isna().all():
            continue
        if col == "sigma_phi_1" and "sigma_phi_quality_flag_1" in day:
            d = day[day["sigma_phi_quality_flag_1"].eq(0)]
        else:
            d = day
        d = d.sort_values(col, na_position="first")
        fig.add_trace(go.Scatterpolargl(
            r=90 - d["elev"], theta=d["azim"], mode="markers",
            marker={"size": 4, "color": d[col], "cmin": lo, "cmax": hi, "colorscale": cmap,
                    "colorbar": {"title": "", "len": 0.38, "x": 0.44 + 0.56 * (c - 1),
                                 "y": 0.79 - 0.58 * (r - 1), "thickness": 10}},
            text=d["prn"] + " " + d["datetime"].dt.strftime("%H:%M"), showlegend=False,
            hovertemplate="%{text}<br>" + title + " %{marker.color:.2f}<extra></extra>"), row=r, col=c)
        fig.add_trace(go.Scatterpolar(r=90 - trk["elev"], theta=trk["azim"], mode="lines+markers",
                                      line={"color": "#d92d20", "width": 3}, marker={"size": 5},
                                      name=f"{event['prn']} event", showlegend=k == 0,
                                      hoverinfo="skip"), row=r, col=c)
        fig.update_layout(**{polar: {"radialaxis": {"range": [0, 90 - min(mask, 0)], "tickvals": [0, 30, 60, 80],
                                                     "ticktext": ["90°", "60°", "30°", "10°"], "showline": False},
                                     "angularaxis": {"rotation": 90, "direction": "clockwise",
                                                     "tickvals": [0, 90, 180, 270],
                                                     "ticktext": ["N", "E", "S", "W"]}}})
    span = f"±{hours} h of the event" if hours else "whole UTC day"
    fig.update_layout(height=900, margin={"l": 30, "r": 30, "t": 70, "b": 30}, paper_bgcolor="white",
                      title={"text": f"{rx} {event['date']} · every satellite-minute above {mask:g}° ({span}); "
                                     f"red = event track", "font": {"size": 13}, "x": 0},
                      legend={"x": 0, "y": -0.02, "orientation": "h"}, uirevision="keep")
    return fig.to_plotly_json()


# --------------------------------------------------------------------------- network

def network(event: dict, settings: dict) -> dict:
    start, end = pd.Timestamp(event["start_utc"]), pd.Timestamp(event["end_utc"])
    t0, t1 = start - pd.Timedelta(hours=3), end + pd.Timedelta(hours=3)
    mask = float(settings.get("elevation_mask_deg", 10))
    others = data.receivers_on(event["date"])
    if len(others) < 2:
        raise Missing("No other receiver was processed for this day.")
    rec = __import__("pandas").read_csv(config.data_root() / "inventory" / "receivers.csv").set_index("receiver")
    lat, lon = event.get("latitude"), event.get("longitude")

    def dist(r):
        if r not in rec.index or lat is None:
            return 1e9
        return float(np.hypot(rec.loc[r, "latitude"] - lat, (rec.loc[r, "longitude"] - lon)
                              * np.cos(np.radians(lat))) * 111.2)
    others = sorted(others, key=dist)[:12]
    fig = make_subplots(rows=len(others), cols=1, shared_xaxes=True, vertical_spacing=0.012,
                        subplot_titles=[f"{r} ({dist(r):.0f} km)" for r in others])
    for k, r in enumerate(others, start=1):
        m = data.window(r, "minute", t0.floor("min"), t1)
        if m.empty or "s4_1" not in m:
            continue
        m = m[m["elev"] > mask]
        mx = m.groupby("datetime")["s4_1"].max()
        color = "#d92d20" if r == event["receiver"] else "#475467"
        _line(fig, mx.index, mx.values, r, k, color, show=False, gl=False)
        fig.update_yaxes(range=[0, 1], row=k, col=1, tickfont={"size": 9})
    fig.update_layout(title={"text": f"Highest S4 L1 among satellites above {mask:g}°, nearest receivers "
                                     f"(±3 h)", "font": {"size": 13}, "x": 0})
    out = _layout(fig, 110 * len(others) + 120, start, end, t0, t1, len(others))
    out["layout"]["margin"]["t"] = 70
    return out


# --------------------------------------------------------------------------- indices

def indices(event: dict, settings: dict) -> dict:
    kp = data.kp_table()
    if kp is None:
        raise Missing("Kp file not found; run scintlab fetch-indices.")
    d = pd.Timestamp(event["date"])
    days = [(d + pd.Timedelta(days=k)).strftime("%Y-%m-%d") for k in (-2, -1, 0, 1)]
    t, v = [], []
    for day in days:
        if day in kp.index:
            for i, val in enumerate(kp.loc[day, "kp3h"]):
                t.append(pd.Timestamp(day) + pd.Timedelta(hours=3 * i + 1.5))
                v.append(val)
    colors = ["#12b76a" if x < 4 else "#f79009" if x < 6 else "#d92d20" for x in v]
    fig = go.Figure(go.Bar(x=t, y=v, marker_color=colors, width=3 * 3600 * 1000 * 0.9,
                           hovertemplate="Kp %{y:.2f}<extra></extra>"))
    start = pd.Timestamp(event["start_utc"]).tz_localize(None)
    fig.add_vline(x=start, line_color="#101828", line_width=2)
    f107 = kp["f107"].get(event["date"], np.nan)
    fig.update_layout(height=360, margin={"l": 60, "r": 30, "t": 60, "b": 40}, paper_bgcolor="white",
                      plot_bgcolor="white", yaxis={"range": [0, 9], "title": "Kp", "gridcolor": GRID},
                      title={"text": f"Kp, two days before to one day after (line = event start). "
                                     f"F10.7 on the day: {f107:.0f} sfu", "font": {"size": 13}, "x": 0})
    return fig.to_plotly_json()


TABS = {"overview": overview, "highrate": highrate, "satellites": satellites, "sidereal": sidereal,
        "skyplot": skyplot, "network": network, "indices": indices}
TAB_TITLES = {"overview": "Overview", "highrate": "20 Hz", "satellites": "Other satellites",
              "sidereal": "Sidereal repeat", "skyplot": "Sky maps", "network": "Network", "indices": "Kp / F10.7"}
