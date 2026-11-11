"""Configuration for the local ScintPi disturbance labeler."""

from __future__ import annotations

import os
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
DATA_ROOT = Path(
    os.environ.get(
        "SCINTPI_DATA_ROOT",
        "/Users/isaac/Documents/MothersDay-Manuscript/data",
    )
).expanduser()
STATE_DIR = Path(os.environ.get("SCINTPI_LABEL_STATE", APP_DIR / "state")).expanduser()
DATABASE_PATH = STATE_DIR / "manual_labels.sqlite3"
EXPORT_DIR = APP_DIR / "exports"

S4_THRESHOLD = 0.2
MIN_SAMPLES = 1190
MIN_ELEVATION_DEG = 30.0
MAX_ELEVATION_DEG = 90.0
PLOT_PADDING_MINUTES = 20
COMPARISON_SATELLITES = 10
EXCLUDED_TIMESTAMP_DATES = {"2024-05-05"}
EXCLUDED_STATIONS = {"MOS"}

VALID_LABELS = ("Scintillation", "Multipath", "RFI", "Other")
VALID_CONFIDENCE = ("High", "Medium", "Low")
