"""Label storage: one append-only JSON Lines file per user in ``<data_root>/labels/``.

Every save appends one line and calls fsync, so a closed browser, a dropped
SSH tunnel or a killed server loses nothing that was saved. The latest line
for an (event, user) pair is that user's current decision. Each user writes
only their own file, so several people can label at the same time.

Record fields: event_id, user, time_utc, action ("label", "skip", "clear" or
"review"), label, confidence, for_review, notes, segments (minute ranges with
their own label), and for "review" lines final_label.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from scintlab import config


def _append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, default=str) + "\n").encode()
    # A crash can leave a last line without its newline; start on a fresh line
    # so this record is not glued onto the broken one.
    if path.exists() and path.stat().st_size:
        with open(path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                line = b"\n" + line
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o664)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def _read(path: Path) -> list[dict]:
    out = []
    if not path.exists():
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # a line cut by a crash; the rest is intact
    return out


class LabelStore:
    def __init__(self, user: str | None = None, cfg: dict | None = None):
        self.cfg = cfg or config.load()
        self.user = user or config.current_user()
        self.dir = config.labels_dir()
        self.path = self.dir / f"{self.user}.jsonl"
        self.classes = list(self.cfg["labels"]["classes"])
        self.confidence = list(self.cfg["labels"]["confidence"])
        self.reviewers = set(self.cfg["labels"]["reviewers"])
        self.mine: dict[str, dict] = {}
        for r in _read(self.path):
            self._apply(self.mine, r)

    @property
    def is_reviewer(self) -> bool:
        return self.user in self.reviewers

    @staticmethod
    def _apply(table: dict, r: dict) -> None:
        if r.get("action") == "clear":
            table.pop(r["event_id"], None)
        elif r.get("action") in ("label", "skip"):
            table[r["event_id"]] = r

    def save(self, event_id: str, action: str, label=None, confidence=None, for_review=False, notes="",
             segments=None, extra=None) -> dict:
        if action not in ("label", "skip", "clear"):
            raise ValueError(f"unknown action {action}")
        if action == "label":
            if label not in self.classes:
                raise ValueError(f"label must be one of {self.classes}")
            if confidence not in self.confidence:
                raise ValueError(f"confidence must be one of {self.confidence}")
        for s in segments or []:
            if s.get("label") not in self.classes:
                raise ValueError(f"segment label must be one of {self.classes}")
        record = {
            "event_id": event_id, "user": self.user, "time_utc": pd.Timestamp.utcnow().isoformat(),
            "action": action, "label": label, "confidence": confidence, "for_review": bool(for_review),
            "notes": notes or "", "segments": segments or [], **(extra or {}),
        }
        _append(self.path, record)
        self._apply(self.mine, record)
        return record

    def review(self, event_id: str, final_label: str, notes: str = "") -> dict:
        if not self.is_reviewer:
            raise PermissionError(f"{self.user} is not a reviewer")
        if final_label not in self.classes:
            raise ValueError(f"label must be one of {self.classes}")
        record = {"event_id": event_id, "user": self.user, "time_utc": pd.Timestamp.utcnow().isoformat(),
                  "action": "review", "final_label": final_label, "notes": notes}
        _append(self.dir / f"{self.user}.jsonl", record)
        return record

    # ------------------------------------------------------------------ all users

    def all_decisions(self) -> pd.DataFrame:
        """Latest label/skip per (event, user) across every label file."""
        rows = []
        for path in sorted(self.dir.glob("*.jsonl")):
            table: dict[str, dict] = {}
            for r in _read(path):
                self._apply(table, r)
            rows += table.values()
        return pd.DataFrame(rows)

    def reviews(self) -> pd.DataFrame:
        rows = {}
        for path in sorted(self.dir.glob("*.jsonl")):
            for r in _read(path):
                if r.get("action") == "review":
                    rows[r["event_id"]] = r
        return pd.DataFrame(rows.values())

    def summary(self, events: pd.DataFrame) -> dict:
        ids = set(events["event_id"])
        mine = {k: v for k, v in self.mine.items() if k in ids}
        labeled = [r for r in mine.values() if r["action"] == "label"]
        by_class = pd.Series([r["label"] for r in labeled]).value_counts().to_dict() if labeled else {}
        reviews = self.reviews()
        flagged = self.flagged()
        return {
            "user": self.user, "is_reviewer": self.is_reviewer, "total": len(ids),
            "labeled": len(labeled), "skipped": sum(r["action"] == "skip" for r in mine.values()),
            "remaining": len(ids) - len(mine), "for_review_mine": sum(r.get("for_review", False) for r in labeled),
            "by_class": by_class, "high_confidence": sum(r.get("confidence") == "High" for r in labeled),
            "review_queue": int(len(set(flagged) - set(reviews["event_id"]) if len(reviews) else set(flagged))),
        }

    def flagged(self) -> list[str]:
        d = self.all_decisions()
        if d.empty:
            return []
        return sorted(set(d.loc[d["for_review"].fillna(False).astype(bool), "event_id"]))


# --------------------------------------------------------------------------- settings

def load_settings(user: str | None = None) -> dict:
    cfg = config.load()
    base = dict(cfg["display"])
    base["tabs"] = dict(base["tabs"])
    path = config.labels_dir() / "settings" / f"{user or config.current_user()}.json"
    if path.exists():
        saved = json.loads(path.read_text())
        tabs = {**base["tabs"], **saved.pop("tabs", {})}
        base.update(saved)
        base["tabs"] = tabs
    return base


def save_settings(settings: dict, user: str | None = None) -> dict:
    allowed = set(config.load()["display"])
    clean = {k: v for k, v in settings.items() if k in allowed}
    path = config.labels_dir() / "settings" / f"{user or config.current_user()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, indent=2))
    os.replace(tmp, path)
    return load_settings(user)


# --------------------------------------------------------------------------- export

def export() -> dict:
    """Write ``exports/labels_<time>.{csv,parquet}`` (one row per event and labeler) and
    ``exports/minute_labels_<time>.parquet`` (one row per candidate minute, segment labels applied).
    The final label is the reviewer's when there is one, else the labeler's."""
    from scintlab.events import load_events

    store = LabelStore()
    ev = load_events()
    d = store.all_decisions()
    out_dir = config.data_root() / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.utcnow().strftime("%Y%m%dT%H%M%S")
    if d.empty:
        return {"rows": 0}
    d = d[d["action"] == "label"]
    rv = store.reviews()
    table = ev.merge(d, on="event_id", how="inner")
    if len(rv):
        table = table.merge(rv[["event_id", "user", "final_label"]].rename(columns={"user": "reviewer"}),
                            on="event_id", how="left")
    else:
        table["final_label"] = np.nan
        table["reviewer"] = np.nan
    table["final_label"] = table["final_label"].fillna(table["label"])
    table["segments"] = table["segments"].map(json.dumps)
    table.to_parquet(out_dir / f"labels_{stamp}.parquet", index=False)
    table.to_csv(out_dir / f"labels_{stamp}.csv", index=False)

    minutes = []
    for r in table.itertuples():
        t = pd.date_range(r.start_utc, r.end_utc, freq="1min", inclusive="left")
        lab = pd.Series(r.final_label, index=t, dtype=object)
        for s in json.loads(r.segments):
            lab[(t >= pd.Timestamp(s["start"])) & (t < pd.Timestamp(s["end"]))] = s["label"]
        minutes.append(pd.DataFrame({"event_id": r.event_id, "receiver": r.receiver, "prn": r.prn,
                                     "user": r.user, "datetime": t, "label": lab.to_numpy(),
                                     "confidence": r.confidence}))
    m = pd.concat(minutes, ignore_index=True)
    m.to_parquet(out_dir / f"minute_labels_{stamp}.parquet", index=False)
    return {"events": int(len(table)), "minutes": int(len(m)),
            "files": [str(out_dir / f"labels_{stamp}.csv"), str(out_dir / f"minute_labels_{stamp}.parquet")]}
