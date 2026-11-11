"""Build the six-panel event plot shown by the labeling application."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pyarrow as pa
import pyarrow.parquet as pq

from config import (
    COMPARISON_SATELLITES,
    DATA_ROOT,
    EXCLUDED_TIMESTAMP_DATES,
    PLOT_PADDING_MINUTES,
    S4_THRESHOLD,
)


PLOT_COLUMNS = [
    "datetime",
    "minbin",
    "prn",
    "elev",
    "azim",
    "snr1",
    "snr2",
    "snr3",
    "sig_1",
    "sig_2",
    "sig_3",
    "tec_cph12",
    "s4_1",
    "s4_2",
    "s4_3",
    "sigma_phi_1",
    "sigma_phi_2",
    "sigma_phi_3",
    "quality_1",
    "quality_2",
    "quality_3",
]

COLORS = {
    "blue": "#2f6fed",
    "cyan": "#28b8c8",
    "orange": "#e68a32",
    "red": "#d94a5a",
    "purple": "#7c5ce5",
    "green": "#2e9b75",
    "slate": "#52606d",
}
COMPARISON_COLORS = [
    "#2f6fed",
    "#23a7a0",
    "#8a63d2",
    "#e17b3a",
    "#cf4d6f",
    "#5d8f3f",
    "#8d6e63",
    "#0086b3",
    "#aa6d00",
    "#667085",
]


@lru_cache(maxsize=24)
def build_event_figure(
    station: str, prn: str, start_utc: str, end_utc: str
) -> dict[str, object]:
    start = pd.Timestamp(start_utc).tz_convert("UTC")
    end = pd.Timestamp(end_utc).tz_convert("UTC")
    window_start = start - pd.Timedelta(minutes=PLOT_PADDING_MINUTES)
    window_end = end + pd.Timedelta(minutes=PLOT_PADDING_MINUTES)
    data = _load_station_window(station, window_start, window_end)
    target = data.loc[data["prn"].eq(prn)].copy()
    if target.empty:
        raise RuntimeError(
            f"No plot data found for {station} {prn} in the requested window"
        )

    comparison = _best_coverage_satellites(data, target_prn=prn)
    comparison_title = " · ".join(comparison) if comparison else "none available"
    figure = make_subplots(
        rows=6,
        cols=1,
        shared_xaxes=False,
        vertical_spacing=0.032,
        row_heights=[0.19, 0.17, 0.12, 0.18, 0.17, 0.17],
        specs=[
            [{"secondary_y": False}],
            [{"secondary_y": False}],
            [{"secondary_y": True}],
            [{"secondary_y": False}],
            [{"secondary_y": False}],
            [{"secondary_y": False}],
        ],
        subplot_titles=(
            f"Comparison C/N₀ · best coverage: {comparison_title}",
            f"Target C/N₀ · {station} {prn}",
            "Viewing geometry",
            "Relative slant TEC · carrier phase L1/L2",
            "Amplitude scintillation index",
            "Phase scintillation index",
        ),
    )

    for index, comparison_prn in enumerate(comparison):
        satellite = data.loc[data["prn"].eq(comparison_prn)]
        figure.add_trace(
            go.Scattergl(
                x=satellite["datetime"],
                y=satellite["snr1"],
                mode="lines",
                name=f"{comparison_prn} SNR1",
                legendgroup="comparison",
                showlegend=False,
                line={"width": 1.2, "color": COMPARISON_COLORS[index]},
                hovertemplate=(
                    f"{comparison_prn}<br>%{{x|%Y-%m-%d %H:%M:%S}} UTC"
                    "<br>C/N₀ %{y:.1f} dB-Hz<extra></extra>"
                ),
            ),
            row=1,
            col=1,
        )

    signal_names = _signal_names(target)
    channel_dashes = ["solid", "dot", "dashdot"]
    for channel in range(1, 4):
        column = f"snr{channel}"
        if target[column].notna().sum() < 2:
            continue
        name = signal_names.get(channel, f"SNR{channel}")
        figure.add_trace(
            go.Scattergl(
                x=target["datetime"],
                y=target[column],
                mode="lines",
                name=name,
                legendgroup="target-snr",
                line={
                    "width": 1.55,
                    "color": COLORS["blue"],
                    "dash": channel_dashes[channel - 1],
                },
                hovertemplate=(
                    f"{name}<br>%{{x|%Y-%m-%d %H:%M:%S}} UTC"
                    "<br>C/N₀ %{y:.1f} dB-Hz<extra></extra>"
                ),
            ),
            row=2,
            col=1,
        )

    figure.add_trace(
        go.Scattergl(
            x=target["datetime"],
            y=target["elev"],
            mode="lines",
            name="Elevation",
            legendgroup="geometry",
            line={"width": 1.7, "color": COLORS["red"]},
            hovertemplate="Elevation %{y:.1f}°<extra></extra>",
        ),
        row=3,
        col=1,
        secondary_y=False,
    )
    figure.add_trace(
        go.Scattergl(
            x=target["datetime"],
            y=target["azim"],
            mode="lines",
            name="Azimuth",
            legendgroup="geometry",
            line={"width": 1.5, "color": COLORS["orange"]},
            hovertemplate="Azimuth %{y:.1f}°<extra></extra>",
        ),
        row=3,
        col=1,
        secondary_y=True,
    )

    finite_tec = pd.to_numeric(target["tec_cph12"], errors="coerce")
    tec_reference = finite_tec.min(skipna=True)
    relative_tec = finite_tec - tec_reference if pd.notna(tec_reference) else finite_tec
    figure.add_trace(
        go.Scattergl(
            x=target["datetime"],
            y=relative_tec,
            mode="lines",
            name="STEC L1/L2",
            legendgroup="tec",
            line={"width": 1.65, "color": "#1d2939"},
            hovertemplate="Relative STEC %{y:.2f} TECU<extra></extra>",
        ),
        row=4,
        col=1,
    )

    minute_target = _minute_level_target(target)
    for channel in range(1, 4):
        column = f"s4_{channel}"
        if minute_target[column].notna().sum() > 0:
            figure.add_trace(
                go.Scatter(
                    x=minute_target["minbin"],
                    y=minute_target[column],
                    mode="lines+markers",
                    name=f"S4 {signal_names.get(channel, f'channel {channel}')}",
                    legendgroup="s4",
                    line={
                        "width": 1.7,
                        "color": COLORS["blue"],
                        "dash": channel_dashes[channel - 1],
                    },
                    marker={"size": 3},
                    hovertemplate="S4 %{y:.3f}<extra></extra>",
                ),
                row=5,
                col=1,
            )
        sigma_column = f"sigma_phi_{channel}"
        quality_column = f"quality_{channel}"
        valid_sigma = minute_target[sigma_column].where(
            minute_target[quality_column].eq(0)
        )
        if valid_sigma.notna().sum() > 0:
            figure.add_trace(
                go.Scatter(
                    x=minute_target["minbin"],
                    y=valid_sigma,
                    mode="lines+markers",
                    name=f"σφ {signal_names.get(channel, f'channel {channel}')}",
                    legendgroup="sigma",
                    line={
                        "width": 1.7,
                        "color": COLORS["purple"],
                        "dash": channel_dashes[channel - 1],
                    },
                    marker={"size": 3},
                    hovertemplate="σφ %{y:.3f} rad<extra></extra>",
                ),
                row=6,
                col=1,
            )

    for row in range(1, 7):
        figure.add_vrect(
            x0=start,
            x1=end,
            fillcolor="#f5b942",
            opacity=0.17,
            line_width=0,
            row=row,
            col=1,
        )
    figure.add_hline(
        y=S4_THRESHOLD,
        line_dash="dash",
        line_width=1.3,
        line_color="#c23b4a",
        annotation_text="S4 = 0.2",
        annotation_position="top left",
        row=5,
        col=1,
    )

    figure.update_yaxes(title_text="C/N₀ (dB-Hz)", row=1, col=1)
    figure.update_yaxes(title_text="C/N₀ (dB-Hz)", row=2, col=1)
    figure.update_yaxes(title_text="Elev. (°)", range=[0, 95], row=3, col=1)
    figure.update_yaxes(
        title_text="Azim. (°)", range=[0, 360], row=3, col=1, secondary_y=True
    )
    figure.update_yaxes(title_text="Relative STEC (TECU)", row=4, col=1)
    figure.update_yaxes(title_text="S4", rangemode="tozero", row=5, col=1)
    figure.update_yaxes(title_text="σφ (rad)", rangemode="tozero", row=6, col=1)
    figure.update_xaxes(
        range=[window_start, window_end],
        showgrid=True,
        gridcolor="rgba(93, 108, 128, 0.13)",
    )
    figure.update_xaxes(title_text="Time (UTC)", row=6, col=1)
    figure.update_layout(
        height=1220,
        margin={"l": 78, "r": 54, "t": 60, "b": 55},
        paper_bgcolor="#ffffff",
        plot_bgcolor="#ffffff",
        font={"family": "Inter, ui-sans-serif, system-ui, sans-serif", "size": 13},
        hovermode="x unified",
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.01,
            "xanchor": "left",
            "x": 0,
            "font": {"size": 11},
            "groupclick": "toggleitem",
        },
        uirevision=f"{station}-{prn}-{start_utc}",
    )
    for annotation in figure.layout.annotations:
        annotation.font = {"size": 13, "color": "#344054"}
        annotation.x = 0
        annotation.xanchor = "left"
    return figure.to_plotly_json()


def _load_station_window(
    station: str, window_start: pd.Timestamp, window_end: pd.Timestamp
) -> pd.DataFrame:
    paths = sorted((DATA_ROOT / station).glob("*_1sec.pq"))
    if not paths:
        raise FileNotFoundError(f"No source files found for station {station}")
    pieces: list[pd.DataFrame] = []
    naive_start = window_start.tz_convert(None).to_pydatetime()
    naive_end = window_end.tz_convert(None).to_pydatetime()
    filters = [("datetime", ">=", naive_start), ("datetime", "<=", naive_end)]
    for path in paths:
        try:
            table = pq.read_table(path, columns=PLOT_COLUMNS, filters=filters)
        except (pa.ArrowInvalid, pa.ArrowNotImplementedError):
            table = pq.read_table(path, columns=PLOT_COLUMNS)
        if table.num_rows:
            pieces.append(table.to_pandas())
    if not pieces:
        return pd.DataFrame(columns=PLOT_COLUMNS)
    frame = pd.concat(pieces, ignore_index=True)
    frame["datetime"] = pd.to_datetime(frame["datetime"], errors="coerce", utc=True)
    frame["minbin"] = pd.to_datetime(frame["minbin"], errors="coerce", utc=True)
    valid = (
        frame["datetime"].between(window_start, window_end, inclusive="both")
        & frame["prn"].notna()
        & ~frame["datetime"].dt.strftime("%Y-%m-%d").isin(EXCLUDED_TIMESTAMP_DATES)
    )
    frame = frame.loc[valid].copy()
    frame = frame.sort_values(["prn", "datetime"]).drop_duplicates(
        ["prn", "datetime"], keep="last"
    )
    return frame.sort_values("datetime").reset_index(drop=True)


def _best_coverage_satellites(frame: pd.DataFrame, target_prn: str) -> list[str]:
    candidates = frame.loc[frame["prn"].ne(target_prn) & frame["snr1"].notna()]
    counts = (
        candidates.groupby("prn")["datetime"]
        .nunique()
        .sort_values(ascending=False, kind="stable")
    )
    return [str(value) for value in counts.head(COMPARISON_SATELLITES).index]


def _signal_names(target: pd.DataFrame) -> dict[int, str]:
    names: dict[int, str] = {}
    for channel in range(1, 4):
        values = target[f"sig_{channel}"].dropna().astype(str)
        values = values.loc[~values.isin({"", "nan", "None"})]
        if not values.empty:
            names[channel] = values.mode().iloc[0]
    return names


def _minute_level_target(target: pd.DataFrame) -> pd.DataFrame:
    metric_columns = [
        "s4_1",
        "s4_2",
        "s4_3",
        "sigma_phi_1",
        "sigma_phi_2",
        "sigma_phi_3",
        "quality_1",
        "quality_2",
        "quality_3",
    ]
    return (
        target.loc[target["minbin"].notna(), ["minbin", *metric_columns]]
        .groupby("minbin", as_index=False)
        .agg({column: "first" for column in metric_columns})
        .sort_values("minbin")
    )
