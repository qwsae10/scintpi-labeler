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
from scintlab import data
from scintlab.events import added_path, filter_events, load_events, make_added_event
from scintlab.geomag import dip_latitudes
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
    def events_for_app() -> pd.DataFrame:
        ev = load_events()
        ev["dip_lat"] = dip_latitudes(ev)  # header only; NaN without ppigrf
        return ev

    def added_mtime():
        try:
            return added_path().stat().st_mtime_ns
        except OSError:
            return None

    state = {"events": events_for_app(), "store": LabelStore(), "added_mtime": added_mtime()}
    state["by_id"] = state["events"].set_index("event_id", drop=False)

    def sync_added() -> None:
        """Every labeler runs a server of their own: take in the events others added (or took back)."""
        m = added_mtime()
        if m == state["added_mtime"]:
            return
        state["added_mtime"] = m
        state["events"] = events_for_app()
        state["by_id"] = state["events"].set_index("event_id", drop=False)

    def store() -> LabelStore:
        return state["store"]

    def event_dict(event_id: str) -> dict:
        row = state["by_id"].loc[event_id]
        d = {k: _clean(v) for k, v in row.items()}
        mine = store().mine.get(event_id)
        d["decision"] = mine
        return d

    # URL parameters of the queue's data filters -> rule names of events.filter_events / the config
    DQ_ARGS = {"min_minutes": "min_minutes", "min_snr1": "min_frac_snr1", "max_gap": "max_gap_s",
               "min_snr2": "min_frac_snr2", "min_tec": "min_frac_tec", "lol": "loss_of_lock", "hide_cut": "hide_cut"}

    def quality_filtered(ev: pd.DataFrame, args) -> pd.DataFrame:
        return filter_events(ev, {rule: args.get(arg) for arg, rule in DQ_ARGS.items()})

    def filtered(args, status: str | None = None, dates: bool = True) -> pd.DataFrame:
        """Events matching the queue filters. ``status`` overrides the status filter; ``dates=False``
        ignores the day selection (the calendar counts every day under the other filters)."""
        ev = state["events"]
        s = load_settings(store().user)
        mask = float(args.get("elev_mask") or s["elevation_mask_deg"])
        ev = ev[ev["max_elev"] >= mask]
        ev = quality_filtered(ev, args)
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
        # Order first, then number the events (_pos): the status filter only drops events, so each
        # one keeps its place while labels change, and the queue can be fetched in pages by position.
        order = args.get("order") or s["queue_order"]
        if order == "chronological":
            ev = ev.sort_values(["receiver", "start_utc", "prn"])
        elif order == "peak_s4":
            ev = ev.sort_values(["peak_s4", "event_id"], ascending=[False, True])
        elif order == "stratified" and "strat_rank" in ev:
            ev = ev.sort_values("strat_rank")
        else:
            ev = ev.sort_values("queue_rank")
        ev = ev.assign(_pos=np.arange(len(ev)))
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
            "queue_filters": cfg.get("queue_filters", {}),
            "has_quality": "frac_snr1" in ev,
        })

    @app.get("/api/summary")
    def summary():
        s = load_settings(store().user)
        ev = state["events"]
        ev = ev[ev["max_elev"] >= float(request.args.get("elev_mask") or s["elevation_mask_deg"])]
        return jsonify(store().summary(quality_filtered(ev, request.args)))

    @app.get("/api/queue")
    def queue():
        """One page of the queue: up to ``limit`` events after position ``after`` (-1 = from the
        start), ``count`` = all events matching, ``next`` = the position to ask for next (None at the end)."""
        sync_added()
        ev = filtered(request.args)
        count = len(ev)
        after = int(request.args.get("after", -1))
        limit = max(1, min(int(request.args.get("limit", 500)), 5000))
        ev = ev[ev["_pos"] > after].head(limit)
        # Everything the event header needs, and my decision, so moving to the next event needs
        # no request of its own.
        cols = ["event_id", "receiver", "prn", "date", "start_utc", "end_utc", "n_minutes", "local_hour", "peak_s4",
                "peak_s4_other", "min_elev", "max_elev", "mean_azim", "latitude", "dip_lat", "rx_type", "day_reason",
                "frac_snr1", "frac_snr2", "frac_tec", "max_gap_s", "ends_in_loss_of_lock", "starts_after_gap",
                "cut_at_day_edge", "crosses_midnight"]
        cols = [c for c in cols if c in ev] + ["_pos"]
        mine = store().mine
        items = []
        for r in ev[cols].to_dict("records"):
            d = {k: _clean(v) for k, v in r.items()}
            d["decision"] = mine.get(r["event_id"])
            items.append(d)
        nxt = int(ev["_pos"].iloc[-1]) if len(ev) == limit else None
        return jsonify({"count": count, "events": items, "next": nxt})

    @app.get("/api/days")
    def days():
        """Per UTC day: events under the current filters (any status, any day) and how many I decided."""
        sync_added()
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

    NEAR = pd.Timedelta(hours=3)  # neighbours are looked for this far either side (the plots show less)

    def neighbours_of(row, t0, t1) -> pd.DataFrame:
        ev = state["events"]
        return ev[(ev["receiver"] == row["receiver"]) & (ev["prn"] == row["prn"]) & (ev["event_id"] != row["event_id"])
                  & (ev["start_utc"] < t1) & (ev["end_utc"] > t0)]

    @app.get("/api/event/<event_id>/neighbours")
    def neighbours(event_id):
        """Other events of this receiver and satellite near this one, with my decision on each: the
        plots mark them in the margin around the event, so one labeled before is seen as such."""
        sync_added()
        if event_id not in state["by_id"].index:
            return jsonify({"error": "event not found"}), 404
        row = state["by_id"].loc[event_id]
        near = neighbours_of(row, row["start_utc"] - NEAR, row["end_utc"] + NEAR)
        mine = store().mine
        return jsonify([{"event_id": r.event_id, "start_utc": r.start_utc.isoformat(), "end_utc": r.end_utc.isoformat(),
                         "decision": mine.get(r.event_id)} for r in near.itertuples()])

    def add_rows(rows: pd.DataFrame) -> None:
        state["events"] = pd.concat([state["events"], rows], ignore_index=True)
        state["by_id"] = state["events"].set_index("event_id", drop=False)

    @app.post("/api/event/<event_id>/missed")
    def missed(event_id):
        """Make minutes [start, end) of this event's receiver and satellite, outside the event, an
        event of their own at the end of the queue (scintillation the detector left out)."""
        if event_id not in state["by_id"].index:
            return jsonify({"error": "event not found"}), 404
        src = state["by_id"].loc[event_id]
        p = request.get_json(silent=True) or {}
        try:
            utc = lambda t: t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")  # noqa: E731
            start, end = utc(pd.Timestamp(p["start"])).floor("min"), utc(pd.Timestamp(p["end"])).ceil("min")
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "give start and end (UTC, ISO 8601)"}), 400
        if end <= start or end - start > pd.Timedelta(hours=6):
            return jsonify({"error": "the range must be 1 minute to 6 hours long"}), 400
        if start < src["end_utc"] and end > src["start_utc"]:
            return jsonify({"error": "the range overlaps this event; mark it as a range of the event instead"}), 400
        hit = neighbours_of(src, start, end)
        if len(hit):
            return jsonify({"error": f"the range overlaps event {hit['event_id'].iloc[0]}, already in the queue",
                            "event_id": hit["event_id"].iloc[0]}), 409
        minutes = data.window(src["receiver"], "minute", start, end, src["prn"])
        if minutes.empty:
            return jsonify({"error": "no data for this satellite in that range"}), 400
        ev = state["events"]
        rank = int(max(ev["queue_rank"].max(), ev["strat_rank"].max() if "strat_rank" in ev else 0)) + 1
        row = make_added_event({k: _clean(v) for k, v in src.items()}, start, end, minutes, cfg, store().user, rank)
        from scintlab.labels import _append
        _append(added_path(), {"action": "add", "user": store().user,
                               "time_utc": pd.Timestamp.utcnow().isoformat(), "event": row})
        df = pd.DataFrame([row])
        for c in ("start_utc", "end_utc"):
            df[c] = pd.to_datetime(df[c], utc=True)
        df["added"] = True
        df["dip_lat"] = dip_latitudes(df)
        add_rows(df)
        state["added_mtime"] = added_mtime()
        return jsonify({"event": event_dict(row["event_id"])})

    @app.post("/api/added/<event_id>/remove")
    def remove_added(event_id):
        """Take back an event added by hand (undo)."""
        ev = state["events"]
        if event_id not in state["by_id"].index or not bool(state["by_id"].loc[event_id].get("added", False)):
            return jsonify({"error": "no added event with that id"}), 404
        from scintlab.labels import _append
        _append(added_path(), {"action": "remove", "event_id": event_id, "user": store().user,
                               "time_utc": pd.Timestamp.utcnow().isoformat()})
        state["events"] = ev[ev["event_id"] != event_id].reset_index(drop=True)
        state["by_id"] = state["events"].set_index("event_id", drop=False)
        state["added_mtime"] = added_mtime()
        return jsonify({"removed": event_id})

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
        state["added_mtime"] = added_mtime()
        state["events"] = events_for_app()
        state["by_id"] = state["events"].set_index("event_id", drop=False)
        state["store"] = LabelStore()
        return jsonify({"events": len(state["events"])})

    return app
