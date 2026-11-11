"""Configuration: package defaults, then <data_root>/config.yaml, then $SCINTLAB_CONFIG."""

from __future__ import annotations

import copy
import getpass
import os
from functools import lru_cache
from pathlib import Path

import yaml

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = PACKAGE_DIR / "default_config.yaml"


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _expand(value):
    if isinstance(value, str):
        return os.path.expandvars(os.path.expanduser(value))
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _read(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


@lru_cache(maxsize=1)
def load() -> dict:
    cfg = _read(DEFAULT_CONFIG)
    if os.environ.get("SCINTLAB_DATA_ROOT"):
        cfg["paths"]["data_root"] = os.environ["SCINTLAB_DATA_ROOT"]
    shared = Path(_expand(cfg["paths"]["data_root"])) / "config.yaml"
    if shared.exists():
        cfg = _merge(cfg, _read(shared))
    user_file = os.environ.get("SCINTLAB_CONFIG")
    if user_file:
        cfg = _merge(cfg, _read(Path(user_file)))
    if os.environ.get("SCINTLAB_DATA_ROOT"):
        cfg["paths"]["data_root"] = os.environ["SCINTLAB_DATA_ROOT"]
    return _expand(cfg)


def data_root() -> Path:
    return Path(load()["paths"]["data_root"])


def products_dir() -> Path:
    return data_root() / "products"


def events_path() -> Path:
    return data_root() / "events" / "events.parquet"


def labels_dir() -> Path:
    return data_root() / "labels"


def current_user() -> str:
    return os.environ.get("SCINTLAB_USER") or getpass.getuser()
