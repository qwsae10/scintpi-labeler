"""Flask app. Run with ``scintlab serve`` on ganymede2 and open it through an SSH tunnel."""

from __future__ import annotations

import json
import time
import logging

import numpy as np
import pandas as pd
from flask import Flask, jsonify, render_template, request
from plotly.utils import PlotlyJSONEncoder

from scintlab import __version__, config, figures
from scintlab.events import load_events
from scintlab.labels import LabelStore, load_settings, save_settings

logger = logging.getLogger(__name__)


def _clean(v):
    if isinstance(v, np.ndarray):
        return [_clean(x) for x in v.tolist()]
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if isinstance(v, (pd.Timestamp,)):
        return v.isoformat()
    if isinstance(v, (np.floating,)):
        return None if np.isnan(v) else float(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, float) and np.isnan(v):
        return None
    return v


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.json.sort_keys = False
    cfg = config.load()
    state = {"events": load_events(), "store": LabelStore()}
    state["by_id"] = state["events"].set_index("event_id", drop=False)

    def store() -> LabelStore:
        return state["store"]

    def event_dict(event_id: str) -> dict:
        row = state["by_id"].loc[event_id]
        d = {k: _clean(v) for k, v in row.items()}
        mine = store().mine.get(event_id)
        d["decision"] = mine
        return d

    def filtered(args, status: str | None = None, dates: bool = True) -> pd.DataFrame:
        """Events matching the queue filters. ``status`` overrides the status filter; ``dates=False``
        ignores the day selection (the calendar counts every day under the other filters)."""
        ev = state["events"]
        s = load_settings(store().user)
        mask = float(args.get("elev_mask") or s["elevation_mask_deg"])
        ev = ev[ev["max_elev"] >= mask]
        for key, col in (("receiver", "receiver"), ("rx_type", "rx_type"),
                         ("constellation", "constellation"), ("day_reason", "day_reason")):
            if args.get(key):
                ev = ev[ev[col] == args[key]]
        if dates:  # one UTC day (date) or an inclusive range of them (date_from, date_to)
            lo, hi = args.get("date_from") or args.get("date"), args.get("date_to") or args.get("date")
            if lo:
                ev = ev[ev["date"] >= lo]
            if hi:
                ev = ev[ev["date"] <= hi]
        if args.get("prn"):
            ev = ev[ev["prn"].str.upper() == args["prn"].strip().upper()]
        if args.get("min_s4"):
            ev = ev[ev["peak_s4"] >= float(args["min_s4"])]
        mine = store().mine
        status = status or args.get("status", "unlabeled")
        decided = ev["event_id"].map(lambda e: mine.get(e, {}).get("action"))
        label = ev["event_id"].map(lambda e: mine.get(e, {}).get("label"))
        if status == "unlabeled":
            ev = ev[decided.isna()]
        elif status == "labeled":
            ev = ev[decided.eq("label")]
        elif status == "skipped":
            ev = ev[decided.eq("skip")]
        elif status == "for_review":
            ev = ev[ev["event_id"].map(lambda e: bool(mine.get(e, {}).get("for_review")))]
        elif status == "review_queue":
            flagged = set(store().flagged())
            rv = store().reviews()
            done = set(rv["event_id"]) if len(rv) else set()
            ev = ev[ev["event_id"].isin(flagged - done)]
        elif status in cfg["labels"]["classes"]:
            ev = ev[label.eq(status)]
        order = args.get("order") or s["queue_order"]
        if order == "chronological":
            ev = ev.sort_values(["receiver", "start_utc", "prn"])
        elif order == "peak_s4":
            ev = ev.sort_values("peak_s4", ascending=False)
        elif order == "stratified" and "strat_rank" in ev:
            ev = ev.sort_values("strat_rank")
        else:
            ev = ev.sort_values("queue_rank")
        return ev

    @app.get("/")
    def index():
        return render_template("index.html", version=__version__)

    @app.get("/api/bootstrap")
    def bootstrap():
        ev = state["events"]
        return jsonify({
            "user": store().user, "is_reviewer": store().is_reviewer, "version": __version__,
            "classes": cfg["labels"]["classes"], "confidence": cfg["labels"]["confidence"],
            "settings": load_settings(store().user), "tab_titles": figures.TAB_TITLES,
            "tab_order": list(figures.TAB_TITLES),
            "receivers": sorted(ev["receiver"].unique().tolist()) if len(ev) else [],
            "dates": sorted(ev["date"].unique().tolist()) if len(ev) else [],
            "constellations": sorted(ev["constellation"].unique().tolist()) if len(ev) else [],
            "s4_threshold": cfg["candidates"]["s4_threshold"],
        })

    @app.get("/api/summary")
    def summary():
        s = load_settings(store().user)
        ev = state["events"]
        ev = ev[ev["max_elev"] >= float(request.args.get("elev_mask") or s["elevation_mask_deg"])]
        return jsonify(store().summary(ev))

    @app.get("/api/queue")
    def queue():
        ev = filtered(request.args)
        # Everything the event header needs, and my decision, so moving to the next event needs
        # no request of its own.
        cols = ["event_id", "receiver", "prn", "date", "start_utc", "end_utc", "n_minutes", "local_hour", "peak_s4",
                "peak_s4_other", "min_elev", "max_elev", "mean_azim", "rx_type", "day_reason"]
        cols = [c for c in cols if c in ev]
        mine = store().mine
        items = []
        for r in ev[cols].to_dict("records"):
            d = {k: _clean(v) for k, v in r.items()}
            d["decision"] = mine.get(r["event_id"])
            items.append(d)
        return jsonify({"count": len(items), "events": items})

    @app.get("/api/days")
    def days():
        """Per UTC day: events under the current filters (any status, any day) and how many I decided."""
        ev = filtered(request.args, status="all", dates=False)
        mine = store().mine
        act = ev["event_id"].map(lambda e: mine.get(e, {}).get("action"))
        g = pd.DataFrame({"date": ev["date"], "labeled": act.eq("label"), "skipped": act.eq("skip")})
        g = g.groupby("date").agg(n=("labeled", "size"), labeled=("labeled", "sum"), skipped=("skipped", "sum"))
        return jsonify({d: {k: int(v) for k, v in r.items()} for d, r in g.iterrows()})

    @app.get("/api/event/<event_id>")
    def event(event_id):
        if event_id not in state["by_id"].index:
            return jsonify({"error": "event not found"}), 404
        d = event_dict(event_id)
        if store().is_reviewer:
            others = store().all_decisions()
            if len(others):
                others = others[(others["event_id"] == event_id)]
                d["all_decisions"] = [{k: _clean(v) for k, v in r.items()} for r in others.to_dict("records")]
        return jsonify(d)

    @app.get("/api/event/<event_id>/tab/<tab>")
    def tab(event_id, tab):
        if event_id not in state["by_id"].index or tab not in figures.TABS:
            return jsonify({"error": "not found"}), 404
        settings = load_settings(store().user)
        for key in ("pad_minutes", "skyplot_hours"):
            if request.args.get(key):
                settings[key] = float(request.args[key])
        for key in ("t0", "t1"):
            if request.args.get(key):
                settings[key] = request.args[key]
        began = time.perf_counter()
        try:
            fig = figures.TABS[tab](event_dict(event_id), settings)
        except figures.Missing as e:
            return jsonify({"missing": str(e)})
        except Exception as e:
            logger.exception("figure failed")
            return jsonify({"error": f"could not draw {tab}: {e}"}), 500
        built = time.perf_counter()
        body = json.dumps({"figure": fig}, cls=PlotlyJSONEncoder)
        res = app.response_class(body, mimetype="application/json")
        # read by the app (console) and by the browser's network panel: where a slow tab spends its time
        res.headers["Server-Timing"] = (f"build;dur={1000 * (built - began):.0f}, "
                                        f"json;dur={1000 * (time.perf_counter() - built):.0f}")
        return res

    @app.post("/api/event/<event_id>/label")
    def label(event_id):
        if event_id not in state["by_id"].index:
            return jsonify({"error": "event not found"}), 404
        p = request.get_json(silent=True) or {}
        try:
            rec = store().save(event_id, p.get("action", "label"), label=p.get("label"),
                               confidence=p.get("confidence"), for_review=p.get("for_review", False),
                               notes=p.get("notes", ""), segments=p.get("segments") or [],
                               extra={"elevation_mask_deg": p.get("elevation_mask_deg"), "app_version": __version__,
                                      "seconds_on_event": p.get("seconds_on_event")})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"saved": rec})

    @app.post("/api/event/<event_id>/review")
    def review(event_id):
        p = request.get_json(silent=True) or {}
        try:
            rec = store().review(event_id, p.get("final_label"), p.get("notes", ""))
        except (ValueError, PermissionError) as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"saved": rec})

    @app.get("/api/settings")
    def get_settings():
        return jsonify(load_settings(store().user))

    @app.post("/api/settings")
    def post_settings():
        return jsonify(save_settings(request.get_json(silent=True) or {}, store().user))

    @app.post("/api/reload")
    def reload():
        state["events"] = load_events()
        state["by_id"] = state["events"].set_index("event_id", drop=False)
        state["store"] = LabelStore()
        return jsonify({"events": len(state["events"])})

    return app
