"""Local browser application for manual GNSS disturbance labeling."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from plotly.utils import PlotlyJSONEncoder

from config import DATABASE_PATH, DATA_ROOT, VALID_CONFIDENCE, VALID_LABELS
from database import (
    export_reviews,
    get_event,
    initialize_database,
    list_events,
    save_review,
    summary,
)
from event_index import build_event_index, needs_rebuild
from plotting import build_event_figure


APP_DIR = Path(__file__).resolve().parent
app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["JSON_SORT_KEYS"] = False


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/health")
def health():
    return jsonify({"status": "ok", "database": str(DATABASE_PATH)})


@app.get("/api/summary")
def api_summary():
    return jsonify(summary())


@app.get("/api/events")
def api_events():
    try:
        minimum = _optional_float(request.args.get("min_s4"))
        maximum = _optional_float(request.args.get("max_s4"))
        events = list_events(
            station=_optional_text(request.args.get("station")),
            prn=_optional_text(request.args.get("prn")),
            date=_optional_text(request.args.get("date")),
            status=request.args.get("status", "unlabeled"),
            min_s4=minimum,
            max_s4=maximum,
        )
        return jsonify({"events": events, "count": len(events)})
    except ValueError as error:
        return jsonify({"error": str(error)}), 400


@app.get("/api/events/<event_id>")
def api_event(event_id: str):
    event = get_event(event_id)
    if event is None:
        return jsonify({"error": "Event not found"}), 404
    try:
        figure = build_event_figure(
            str(event["station"]),
            str(event["prn"]),
            str(event["start_utc"]),
            str(event["end_utc"]),
        )
    except Exception as error:
        app.logger.exception("Could not build event plot")
        return jsonify({"error": f"Could not build plot: {error}"}), 500
    return app.response_class(
        json.dumps({"event": event, "figure": figure}, cls=PlotlyJSONEncoder),
        mimetype="application/json",
    )


@app.post("/api/events/<event_id>/review")
def api_save_review(event_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        review = save_review(
            event_id,
            state=str(payload.get("state", "labeled")),
            label=payload.get("label"),
            confidence=payload.get("confidence"),
            notes=str(payload.get("notes", "")),
        )
        return jsonify({"review": review, "summary": summary()})
    except (ValueError, KeyError) as error:
        return jsonify({"error": str(error)}), 400


@app.post("/api/export")
def api_export():
    try:
        return jsonify(export_reviews())
    except Exception as error:
        app.logger.exception("Could not export reviews")
        return jsonify({"error": f"Export failed: {error}"}), 500


def _optional_text(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return value.strip()


def _optional_float(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    return float(value)


def ensure_index(force: bool = False) -> dict[str, object] | None:
    initialize_database()
    if force or needs_rebuild():
        return build_event_index(DATA_ROOT)
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manual labeling for GNSS disturbance events"
    )
    subparsers = parser.add_subparsers(dest="command", required=False)
    build = subparsers.add_parser("build-index", help="Scan the Parquet repository")
    build.add_argument("--force", action="store_true", help="Rebuild an unchanged index")
    serve = subparsers.add_parser("serve", help="Start the local labeling app")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8765, type=int)
    serve.add_argument("--rebuild", action="store_true")
    subparsers.add_parser("export", help="Write CSV and Parquet review exports")
    subparsers.add_parser("stats", help="Print current progress")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    command = args.command or "serve"
    if command == "build-index":
        result = ensure_index(force=args.force)
        print(json.dumps(result or {"status": "index is current"}, indent=2))
        return 0
    if command == "export":
        ensure_index()
        print(json.dumps(export_reviews(), indent=2))
        return 0
    if command == "stats":
        ensure_index()
        print(json.dumps(summary(), indent=2))
        return 0
    if command == "serve":
        ensure_index(force=args.rebuild)
        app.run(host=args.host, port=args.port, debug=False, threaded=True)
        return 0
    raise AssertionError(f"Unexpected command: {command}")


if __name__ == "__main__":
    sys.exit(main())
