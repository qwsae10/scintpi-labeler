"""Command line: ``scintlab <command>``. Run ``scintlab -h`` for the list."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.request

import pandas as pd

from scintlab import config

KP_URL = "https://kp.gfz-potsdam.de/app/files/Kp_ap_Ap_SN_F107_since_1932.txt"
logger = logging.getLogger("scintlab")


def _inventory_dir():
    return config.data_root() / "inventory"


def cmd_inventory(args):
    from scintlab import inventory

    cfg = config.load()
    raw = inventory.scan_archive(cfg["paths"]["raw_root"], cfg["sampling"]["exclude_folders"])
    files = inventory.drop_unusable_receivers(inventory.parse_files(raw))
    d = _inventory_dir()
    d.mkdir(parents=True, exist_ok=True)
    files.to_parquet(d / "files.parquet", index=False)
    inventory.receivers(files).to_csv(d / "receivers.csv", index=False)
    inventory.day_coverage(files).to_parquet(d / "coverage.parquet", index=False)
    print(f"{len(files)} raw files, {files['receiver'].nunique()} receivers -> {d}")


def cmd_fetch_indices(args):
    d = config.data_root() / "ref"
    d.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(KP_URL, d / "kp_ap_f107.txt")
    print(f"wrote {d / 'kp_ap_f107.txt'}")


def cmd_select_days(args):
    from scintlab import inventory, select_days

    cfg = config.load()
    d = _inventory_dir()
    files = pd.read_parquet(d / "files.parquet")
    kp = select_days.read_kp(config.data_root() / "ref" / "kp_ap_f107.txt")
    days = select_days.choose_days(pd.read_parquet(d / "coverage.parquet"), inventory.receivers(files), kp, cfg)
    out = config.data_root() / "days.csv"
    if out.exists() and not args.overwrite:
        sys.exit(f"{out} exists; pass --overwrite to replace it")
    days.to_csv(out, index=False)
    print(f"{len(days)} days for {days['receiver'].nunique()} receivers -> {out}")
    print(days.groupby(["rx_type", "reason"]).size().to_string())


def _day_list(args) -> pd.DataFrame:
    days = pd.read_csv(config.data_root() / "days.csv", dtype={"date": str})
    if args.receiver:
        days = days[days["receiver"] == args.receiver]
    if args.date:
        days = days[days["date"] == args.date]
    # Storm days first, so the most useful days are ready early in a long array run.
    order = {"forced": 0, "storm": 1, "season": 2, "random": 3}
    days = days.assign(_o=days["reason"].map(order).fillna(9)).sort_values(["_o", "receiver", "date"],
                                                                           kind="stable").drop(columns="_o")
    if args.task_id is not None:
        days = days.iloc[args.task_id :: args.n_tasks]
    return days


def cmd_process(args):
    from scintlab.process import process_day

    files = pd.read_parquet(_inventory_dir() / "files.parquet")
    failed = []
    for row in _day_list(args).itertuples():
        f = files[(files["receiver"] == row.receiver) & (files["date"] == row.date)]
        try:
            process_day(row.receiver, row.rx_type, row.date, f, overwrite=args.overwrite)
        except Exception:
            logger.exception("FAILED %s %s", row.receiver, row.date)
            failed.append((row.receiver, row.date))
    if failed:
        print("failed:", failed)
        sys.exit(1)


def cmd_build_events(args):
    from scintlab.events import build_events

    ev = build_events()
    print(f"{len(ev)} events from {ev['receiver'].nunique() if len(ev) else 0} receivers -> {config.events_path()}")


def cmd_serve(args):
    from scintlab.app import create_app

    cfg = config.load()
    app = create_app()
    host = args.host or cfg["server"]["host"]
    port = args.port or cfg["server"]["port"]
    print(f"\n  scintlab is running for user {config.current_user()}.\n"
          f"  Open http://localhost:{port} in your browser (through your SSH tunnel).\n"
          f"  Stop with Ctrl-C. Every label is already saved.\n")
    app.run(host=host, port=port, debug=False, threaded=True)


def cmd_snapshot(args):
    from scintlab.snapshot import make_snapshot

    out = args.out or str(config.data_root() / "snapshots" / f"snapshot_{pd.Timestamp.utcnow():%Y%m%d_%H%M}")
    res = make_snapshot(out, n=args.n, elev_mask=args.elev_mask, receivers=args.receiver or None,
                        make_zip=not args.no_zip)
    print(json.dumps(res, indent=2))
    if "zip" in res:
        print(f"\nCopy it to a laptop with:\n  scp {config.current_user()}@ganymede2.utdallas.edu:{res['zip']} .")


def cmd_export(args):
    from scintlab.labels import export

    print(json.dumps(export(), indent=2))


def cmd_stats(args):
    from scintlab.labels import LabelStore
    from scintlab.events import load_events

    ev = load_events()
    store = LabelStore()
    print(json.dumps(store.summary(ev), indent=2))


def main(argv=None):
    p = argparse.ArgumentParser(prog="scintlab", description="ScintPi scintillation labeling")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("inventory", help="scan the raw archive (cluster)").set_defaults(func=cmd_inventory)
    sub.add_parser("fetch-indices", help="download Kp, ap and F10.7 from GFZ").set_defaults(func=cmd_fetch_indices)
    s = sub.add_parser("select-days", help="choose receiver-days to process")
    s.add_argument("--overwrite", action="store_true")
    s.set_defaults(func=cmd_select_days)
    s = sub.add_parser("process", help="process receiver-days from days.csv")
    s.add_argument("--receiver")
    s.add_argument("--date")
    s.add_argument("--task-id", type=int)
    s.add_argument("--n-tasks", type=int, default=1)
    s.add_argument("--overwrite", action="store_true")
    s.set_defaults(func=cmd_process)
    sub.add_parser("build-events", help="find candidate events in the products").set_defaults(func=cmd_build_events)
    s = sub.add_parser("serve", help="start the labeling app")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.set_defaults(func=cmd_serve)
    s = sub.add_parser("snapshot", help="write N events and the app to a folder + zip for offline use")
    s.add_argument("--n", type=int, default=50)
    s.add_argument("--elev-mask", type=float, default=10.0)
    s.add_argument("--receiver", action="append", help="limit to these receivers (repeatable)")
    s.add_argument("--out", help="output folder (default <data_root>/snapshots/snapshot_<time>)")
    s.add_argument("--no-zip", action="store_true")
    s.set_defaults(func=cmd_snapshot)
    sub.add_parser("export", help="write the merged labels to CSV and Parquet").set_defaults(func=cmd_export)
    sub.add_parser("stats", help="print labeling progress").set_defaults(func=cmd_stats)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
