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
    ev = pd.DataFrame(events_for_day(day, cfg, rx, days))
    assert sorted(zip(ev["prn"], ev["n_minutes"])) == [("E11", 1), ("G01", 1), ("G01", 2)]
    assert (ev["day_reason"] == "forced").all()
