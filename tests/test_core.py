import json
import zipfile

import numpy as np
import pandas as pd
import pytest

from scintlab import config
from scintlab.process import tau_at_level, highrate_windows, candidate_mask
from scintlab.sc2 import RECORD, read_sc2


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("SCINTLAB_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("SCINTLAB_USER", "tester")
    config.load.cache_clear()
    yield tmp_path
    config.load.cache_clear()


def test_tau_white_noise_is_short_and_ar1_matches_theory():
    rng = np.random.default_rng(0)
    fs = 20.0
    white = 45 + rng.normal(0, 1, 1200)
    assert tau_at_level(white, fs, np.exp(-1), 50) < 0.1
    # AR(1) intensity with known 1/e time: rho^k = e^-1 at k = -1/ln(rho)
    rho = 0.95
    x = np.zeros(1200 * 50)
    for i in range(1, len(x)):
        x[i] = rho * x[i - 1] + rng.normal()
    snr = 10 * np.log10(1e4 * (1 + 0.02 * x[:1200]))
    expected = -1 / np.log(rho) / fs
    assert tau_at_level(snr, fs, np.exp(-1), 50) == pytest.approx(expected, rel=0.35)
    assert np.isnan(tau_at_level(np.full(1200, 45.0), fs, np.exp(-1), 50))
    assert np.isnan(tau_at_level(white[:500], fs, np.exp(-1), 50))


def test_read_sc2_layout_and_time(tmp_path):
    rec = np.zeros(4, dtype=RECORD)
    rec["tod_ms"] = [64800050, 64800050, 64800100, 64800100]
    rec["ms"] = [50, 50, 100, 100]
    rec["svid"] = [10, 131, 10, 4]
    rec["gnss"] = [0, 1, 0, 2]
    rec["cno"] = [45, 46, 0, 47]
    rec["elev"] = [39, 30, 39, 20]
    rec["azim"] = [295, 100, 295, 200]
    raw = tmp_path / "scintpi2_20230902_1800_96.75728W_32.99184N_v226f.bin"
    raw.write_bytes(rec.tobytes())
    z = tmp_path / (raw.name + ".zip")
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(raw, raw.name)
    df = read_sc2(z)
    assert list(df["prn"]) == ["G10", "S31", "G10", "E04"]
    assert df["datetime"].iloc[0] == pd.Timestamp("2023-09-02 18:00:00.050")
    assert np.isnan(df["snr1"].iloc[2])
    assert df["elev"].iloc[0] == 39 and df["azim"].iloc[3] == 200


def _minutes(s4, elev=30.0, prn="G01"):
    t = pd.date_range("2024-10-10 02:00", periods=len(s4), freq="1min")
    return pd.DataFrame({"datetime": t, "prn": prn, "s4_1": s4, "elev": elev, "s4_quality_flag_1": 0})


def test_candidates_and_highrate_windows(root):
    cfg = config.load()
    m = _minutes([0.1, 0.3, 0.25, 0.1, 0.1, 0.4])
    m.loc[5, "elev"] = 4.0  # below the 5 deg floor
    mask = candidate_mask(m, cfg)
    assert mask.tolist() == [False, True, True, False, False, False]
    w = highrate_windows(m, cfg)
    pad = pd.Timedelta(minutes=cfg["processing"]["highrate_pad_minutes"])
    assert w["G01"] == [(m["datetime"][1] - pad, m["datetime"][2] + pd.Timedelta(minutes=1) + pad)]


def test_label_store_append_latest_wins_and_survives_bad_line(root):
    from scintlab.labels import LabelStore

    s = LabelStore()
    s.save("ev1", "label", label="Multipath", confidence="Low")
    s.save("ev1", "label", label="Scintillation", confidence="High", for_review=True,
           segments=[{"start": "2024-10-10T02:01:00Z", "end": "2024-10-10T02:02:00Z", "label": "RFI"}])
    s.save("ev2", "skip")
    with open(s.path, "a") as f:
        f.write('{"event_id": "ev3", "act')  # simulated crash mid-write
    s2 = LabelStore()
    assert s2.mine["ev1"]["label"] == "Scintillation"
    assert s2.mine["ev2"]["action"] == "skip"
    assert "ev3" not in s2.mine
    assert s2.flagged() == ["ev1"]
    with pytest.raises(ValueError):
        s2.save("ev1", "label", label="Banana", confidence="High")
    with pytest.raises(ValueError):
        s2.save("ev1", "label", label="RFI", confidence="Medium")
    s2.save("ev1", "clear")
    assert "ev1" not in LabelStore().mine


def test_settings_roundtrip(root):
    from scintlab.labels import load_settings, save_settings

    s = save_settings({"tabs": {"network": True}, "pad_minutes": 7, "evil": 1})
    assert s["tabs"]["network"] is True and s["tabs"]["overview"] is True
    assert s["pad_minutes"] == 7 and "evil" not in s
    assert load_settings()["pad_minutes"] == 7


def test_events_group_consecutive_minutes(root):
    from scintlab.events import events_for_day

    cfg = config.load()
    day = root / "products" / "TE-HO3" / "20241010"
    day.mkdir(parents=True)
    m = pd.concat([_minutes([0.3, 0.3, 0.1, 0.5], prn="G01"), _minutes([0.1, 0.25], prn="E11")])
    m["azim"] = 180.0
    m.to_parquet(day / "minute.parquet")
    (day / "meta.json").write_text(json.dumps({"receiver": "TE-HO3", "rx_type": "sc3", "date": "2024-10-10"}))
    rx = pd.DataFrame({"receiver": ["TE-HO3"], "latitude": [14.09], "longitude": [-87.16]})
    days = pd.DataFrame({"receiver": ["TE-HO3"], "date": ["2024-10-10"], "reason": ["forced"]})
    cfg0 = {**cfg, "candidates": {**cfg["candidates"], "bridge_gap_minutes": 0}}
    ev = pd.DataFrame(events_for_day(day, cfg0, rx, days))
    assert sorted(zip(ev["prn"], ev["n_minutes"])) == [("E11", 1), ("G01", 1), ("G01", 2)]
    assert (ev["day_reason"] == "forced").all()
    # Default 5-min bridging joins the G01 fragments across the one quiet minute.
    ev = pd.DataFrame(events_for_day(day, cfg, rx, days))
    g = ev[ev["prn"] == "G01"].iloc[0]
    assert len(ev[ev["prn"] == "G01"]) == 1 and g["n_minutes"] == 3 and g["span_minutes"] == 4
    assert g["candidate_minutes"] == ["2024-10-10T02:00Z", "2024-10-10T02:01Z", "2024-10-10T02:03Z"]


def test_stratified_order_cycles_strata():
    from scintlab.events import add_queue_order

    rng = np.random.default_rng(1)
    n = 300
    ev = pd.DataFrame({"event_id": [f"e{i}" for i in range(n)], "rx_type": "sc3",
                       "peak_s4": np.r_[np.full(280, 0.22), np.full(20, 0.9)],
                       "max_elev": np.r_[np.full(280, 8.0), rng.uniform(35, 80, 20)]})
    add_queue_order(ev, seed=5)
    first = ev.sort_values("strat_rank").head(2)
    assert set(first["stratum"]) == {"sc3|s4_0.2-0.3|el_5-10", "sc3|s4_0.5+|el_30+"}
    assert sorted(ev["queue_rank"]) == list(range(1, n + 1))


def test_app_endpoints_on_built_events(root):
    """Every JSON endpoint the page calls at start-up must serialize (list columns included)."""
    from scintlab.events import build_events
    from scintlab.app import create_app

    day = root / "products" / "TE-HO3" / "20241010"
    day.mkdir(parents=True)
    m = _minutes([0.3, 0.3, 0.1, 0.5], prn="G01")
    m["azim"] = 180.0
    m.to_parquet(day / "minute.parquet")
    (day / "meta.json").write_text(json.dumps({"receiver": "TE-HO3", "rx_type": "sc3", "date": "2024-10-10"}))
    (root / "inventory").mkdir()
    pd.DataFrame({"receiver": ["TE-HO3"], "latitude": [14.09], "longitude": [-87.16]}).to_csv(
        root / "inventory" / "receivers.csv", index=False)
    pd.DataFrame({"receiver": ["TE-HO3"], "date": ["2024-10-10"], "reason": ["forced"]}).to_csv(
        root / "days.csv", index=False)
    build_events()
    c = create_app().test_client()
    for url in ("/api/bootstrap", "/api/summary", "/api/queue"):
        assert c.get(url).status_code == 200, url
    eid = c.get("/api/queue").json["events"][0]["event_id"]
    r = c.get(f"/api/event/{eid}")
    assert r.status_code == 200 and isinstance(r.json["candidate_minutes"], list)
    r = c.post(f"/api/event/{eid}/label", json={"action": "label", "label": "Multipath", "confidence": "High"})
    assert r.status_code == 200
    assert c.get(f"/api/event/{eid}").json["decision"]["label"] == "Multipath"


def test_snapshot_builds_self_contained_folder(root, tmp_path):
    from scintlab.events import build_events
    from scintlab.snapshot import make_snapshot

    day = root / "products" / "TE-HO3" / "20241010"
    day.mkdir(parents=True)
    m = _minutes([0.3, 0.3, 0.1, 0.5], prn="G01", elev=30.0)
    m["azim"] = 180.0
    m.to_parquet(day / "minute.parquet")
    sec = pd.DataFrame({"datetime": pd.date_range("2024-10-10 01:30", "2024-10-10 03:00", freq="1s"), "prn": "G01"})
    sec["elev"], sec["azim"], sec["snr1"] = 30.0, 180.0, 45.0
    sec.to_parquet(day / "second.parquet")
    (day / "meta.json").write_text(json.dumps({"receiver": "TE-HO3", "rx_type": "sc3", "date": "2024-10-10"}))
    (root / "inventory").mkdir()
    pd.DataFrame({"receiver": ["TE-HO3"], "latitude": [14.09], "longitude": [-87.16]}).to_csv(
        root / "inventory" / "receivers.csv", index=False)
    pd.DataFrame({"receiver": ["TE-HO3"], "date": ["2024-10-10"], "reason": ["forced"]}).to_csv(
        root / "days.csv", index=False)
    build_events()
    out = tmp_path / "snap"
    res = make_snapshot(out, n=5)
    assert res["events"] >= 1 and (out / "events" / "events.parquet").exists()
    assert (out / "app" / "scintlab" / "app.py").exists() and (out / "run_mac_linux.sh").exists()
    s = pd.read_parquet(out / "products" / "TE-HO3" / "20241010" / "second.parquet")
    assert s["datetime"].dt.tz is None and len(s) > 0
    assert (tmp_path / "snap.zip").exists()


def test_snapshot_overlapping_windows_across_midnight(root):
    from scintlab.snapshot import make_snapshot

    start = pd.Timestamp("2024-10-11 00:01", tz="UTC")
    events = pd.DataFrame([
        dict(event_id=str(i), receiver="RX", date="2024-10-11", prn="G01",
             start_utc=start + pd.Timedelta(minutes=i), end_utc=start + pd.Timedelta(minutes=i+1),
             max_elev=40, strat_rank=i) for i in range(2)
    ])
    (root / "events").mkdir()
    events.to_parquet(config.events_path(), index=False)
    before = pd.Timestamp("2024-10-10 23:59")
    for day, times in [("20241010", [before - pd.Timedelta(hours=1), before]),
                       ("20241011", [start.tz_convert(None)])]:
        folder = root / "products" / "RX" / day
        folder.mkdir(parents=True)
        frame = pd.DataFrame([(t, p) for t in times for p in ["G01", "G02"]], columns=["datetime", "prn"])
        for kind in ("minute", "second", "highrate"):
            frame.to_parquet(folder / f"{kind}.parquet", index=False)

    out = root / "demo"
    result = make_snapshot(out)
    assert result["rows"] == {"minute": 4, "second": 4, "highrate": 2}
    assert pd.read_parquet(out / "events/events.parquet")["event_id"].tolist() == ["0", "1"]
    prev = out / "products/RX/20241010"
    assert pd.read_parquet(prev / "second.parquet")["datetime"].tolist() == [before, before]
    assert pd.read_parquet(prev / "highrate.parquet")["prn"].tolist() == ["G01"]
    with zipfile.ZipFile(result["zip"]) as archive:
        assert archive.testzip() is None
        assert "demo/run_windows.bat" in archive.namelist()


def _day_products(root, rx, date, minute, second=None, rx_type="sc3"):
    day = root / "products" / rx / date.replace("-", "")
    day.mkdir(parents=True)
    minute.assign(azim=180.0).to_parquet(day / "minute.parquet")
    if second is not None:
        second.to_parquet(day / "second.parquet")
    (day / "meta.json").write_text(json.dumps({"receiver": rx, "rx_type": rx_type, "date": date}))


def _cand(times, prn):
    t = pd.to_datetime(times)
    return pd.DataFrame({"datetime": t, "prn": prn, "s4_1": 0.3, "elev": 30.0, "s4_quality_flag_1": 0})


def test_events_join_over_midnight_and_mark_cut_ones(root):
    from scintlab.events import build_events, events_for_day

    _day_products(root, "TE-HO3", "2024-10-10", pd.concat([
        _cand(["2024-10-10 23:57", "2024-10-10 23:58", "2024-10-10 23:59"], "G01"),
        _cand(["2024-10-10 00:00", "2024-10-10 00:01"], "E11")]))  # 2024-10-09 is not processed
    _day_products(root, "TE-HO3", "2024-10-11", _cand(["2024-10-11 00:00", "2024-10-11 00:01"], "G01"))
    (root / "inventory").mkdir()
    pd.DataFrame({"receiver": ["TE-HO3"], "latitude": [14.09], "longitude": [-87.16]}).to_csv(
        root / "inventory" / "receivers.csv", index=False)
    pd.DataFrame({"receiver": ["TE-HO3"] * 2, "date": ["2024-10-10", "2024-10-11"], "reason": ["storm"] * 2}).to_csv(
        root / "days.csv", index=False)
    ev = build_events(write=False).set_index("prn")
    g = ev.loc["G01"]
    assert g["n_minutes"] == 5 and g["crosses_midnight"] and not g["cut_at_day_edge"]
    assert g["date"] == "2024-10-10" and str(g["end_utc"]) == "2024-10-11 00:02:00"
    assert ev.loc["E11", "cut_at_day_edge"] and not ev.loc["E11", "crosses_midnight"]
    # One day on its own still cuts the pass at midnight, and says so.
    rx = pd.read_csv(root / "inventory" / "receivers.csv")
    one = pd.DataFrame(events_for_day(root / "products" / "TE-HO3" / "20241010", config.load(), rx,
                                      pd.read_csv(root / "days.csv", dtype={"date": str}))).set_index("prn")
    assert one.loc["G01", "n_minutes"] == 3 and one.loc["G01", "cut_at_day_edge"]


def test_audit_quality_finds_loss_of_lock_and_gaps(root):
    from scintlab.audit import id_map, quality
    from scintlab.events import build_events

    t = pd.date_range("2024-10-10 01:50", "2024-10-10 02:20", freq="1s", inclusive="left")
    sec = pd.concat([pd.DataFrame({"datetime": t, "prn": p, "elev": 40.0, "snr1": 45.0, "snr2": 40.0,
                                   "tec_cph12": 10.0}) for p in ("G01", "G05", "G07", "G09")], ignore_index=True)
    g01 = sec["prn"].eq("G01")
    sec = sec[~(g01 & (sec["datetime"] >= "2024-10-10 02:03:30"))]          # G01 lost from 02:03:30 on
    sec.loc[sec["prn"].eq("G01") & sec["datetime"].between("2024-10-10 02:01:00", "2024-10-10 02:01:39"),
            "snr2"] = np.nan                                                # 40 s without channel 2
    _day_products(root, "TE-HO3", "2024-10-10", _cand(["2024-10-10 02:00", "2024-10-10 02:01", "2024-10-10 02:02",
                                                       "2024-10-10 02:03"], "G01"), second=sec)
    (root / "inventory").mkdir()
    pd.DataFrame({"receiver": ["TE-HO3"], "latitude": [14.09], "longitude": [-87.16]}).to_csv(
        root / "inventory" / "receivers.csv", index=False)
    pd.DataFrame({"receiver": ["TE-HO3"], "date": ["2024-10-10"], "reason": ["storm"]}).to_csv(
        root / "days.csv", index=False)
    ev = build_events(write=False)
    q = quality(ev, config.load()).iloc[0]
    assert q["frac_snr1"] == round(210 / 240, 4) and q["frac_snr2"] == round(170 / 240, 4)
    assert q["max_gap_s"] == 30 and q["ends_in_loss_of_lock"] and not q["starts_after_gap"]
    m = id_map(ev.assign(event_id="old", end_utc=ev["end_utc"] - pd.Timedelta(minutes=1)), ev)
    assert m.iloc[0]["status"] == "merged" and m.iloc[0]["new_id"] == ev["event_id"].iloc[0]


def test_audit_install_carries_labels_and_feeds_the_queue_filters(root, tmp_path):
    from scintlab.app import create_app
    from scintlab.audit import run
    from scintlab.events import events_for_day, install_events
    from scintlab.labels import LabelStore

    t = pd.date_range("2024-10-10 23:40", "2024-10-11 00:20", freq="1s", inclusive="left")
    sec = pd.concat([pd.DataFrame({"datetime": t, "prn": p, "elev": 40.0, "snr1": 45.0}) for p in ("G01", "E11")])
    sec.loc[sec["prn"].eq("E11") & (sec["datetime"] >= "2024-10-11 00:05"), "snr1"] = np.nan  # E11: L1 half gone
    day1, day2 = sec[sec["datetime"] < "2024-10-11"], sec[sec["datetime"] >= "2024-10-11"]
    _day_products(root, "TE-HO3", "2024-10-10", _cand(["2024-10-10 23:58", "2024-10-10 23:59"], "G01"), second=day1)
    _day_products(root, "TE-HO3", "2024-10-11", pd.concat([
        _cand(["2024-10-11 00:00", "2024-10-11 00:01"], "G01"),
        _cand([f"2024-10-11 00:{m:02d}" for m in range(4, 10)], "E11")]), second=day2)
    (root / "inventory").mkdir()
    rx = pd.DataFrame({"receiver": ["TE-HO3"], "latitude": [14.09], "longitude": [-87.16]})
    rx.to_csv(root / "inventory" / "receivers.csv", index=False)
    days = pd.DataFrame({"receiver": ["TE-HO3"] * 2, "date": ["2024-10-10", "2024-10-11"], "reason": ["storm"] * 2})
    days.to_csv(root / "days.csv", index=False)
    # the index as built before: each day on its own, so G01 is two events
    old = pd.DataFrame(events_for_day(root / "products" / "TE-HO3" / "20241010", config.load(), rx, days)
                       + events_for_day(root / "products" / "TE-HO3" / "20241011", config.load(), rx, days))
    old["queue_rank"] = old["strat_rank"] = range(1, len(old) + 1)
    install_events(old)
    half = old[(old["prn"] == "G01") & (old["date"] == "2024-10-10")]["event_id"].iloc[0]
    LabelStore().save(half, "label", label="Scintillation", confidence="High")

    res = run(tmp_path / "audit", install=True)
    assert "merged into 1 longer events: 2" in res["report"]
    new = pd.read_parquet(config.events_path())
    g01 = new[new["prn"] == "G01"]
    assert len(g01) == 1 and g01["n_minutes"].iloc[0] == 4
    assert LabelStore().mine[g01["event_id"].iloc[0]]["label"] == "Scintillation"  # followed the merge

    c = create_app().test_client()
    assert c.get("/api/bootstrap").json["has_quality"] is True
    ids = lambda q: {e["prn"] for e in c.get(f"/api/queue?status=all&{q}").json["events"]}  # noqa: E731
    assert ids("") == {"G01", "E11"}
    assert ids("min_snr1=80") == {"G01"}           # E11 has L1 for 1 of its 6 minutes
    assert ids("min_minutes=5") == {"E11"}
    assert ids("max_gap=60") == {"G01"}
    assert c.get("/api/summary?min_snr1=80").json["total"] == 1
