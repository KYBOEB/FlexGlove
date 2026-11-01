"""Признаки по окну и детектор «покой / движение»."""
from pathlib import Path

import pytest

from flexglove.dsp import synthetic as syn
from flexglove.dsp.features import FeatureWindow, window_features
from flexglove.dsp.filters import Pipeline, run
from flexglove.dsp.presets import PRESETS
from flexglove.dsp.segmentation import MOTION, REST, HandSegmenter, RestMotionDetector
from flexglove.sources import parse_recording

DATA = Path(__file__).parent / "data" / "esp32_one_sensor_2026-10.csv"


def test_window_features_known_values():
    f = window_features([1.0, 2.0, 3.0, 4.0, 5.0], dt=0.02)
    assert f.mean == 3.0 and f.range == 4.0
    assert f.std == pytest.approx(2 ** 0.5)
    assert f.slope == pytest.approx(50.0)                 # 1 ед. за отсчёт = 50 ед./с при 50 Гц


def test_feature_window_slides():
    w = FeatureWindow(n=3)
    for i, x in enumerate([0, 0, 0, 9]):
        f = w.update(float(x), i * 0.02)
    assert w.full and f.n == 3 and f.range == 9


def test_rest_motion_on_protocol():
    ts = syn.times(syn.protocol_duration())
    raw, _ = syn.SensorModel().generate(syn.protocol(), ts)
    filt = run(Pipeline.from_config(PRESETS["basic"]["config"]), ts, raw)
    det = RestMotionDetector()
    states = [det.update(v, t) for t, v in zip(ts, filt)]
    starts = [ts[i] for i in range(1, len(states)) if states[i] == MOTION and states[i - 1] == REST]
    assert len(starts) == 10                              # 5 сгибов + 5 разгибов
    for expected, got in zip([5 + 2 * k for k in range(10)], starts):
        assert 0 <= got - expected < 0.15                 # движение замечено меньше чем за 150 мс
    assert states[int(4 * 50)] == REST and states[-1] == REST


def test_real_rest_recording_has_no_motion():
    _, rows = parse_recording(DATA.read_text(encoding="utf-8-sig"))
    ts = [i * 0.02 for i in range(len(rows))]
    filt = run(Pipeline.from_config(PRESETS["basic"]["config"]), ts, [r[1][0] for r in rows])
    det = RestMotionDetector()
    assert all(det.update(v, t) == REST for t, v in zip(ts, filt))


def test_hand_segmenter_any_finger():
    h = HandSegmenter()
    for i in range(30):
        h.update([213.0, 213.0], i * 0.02)
    assert not h.moving
    for i in range(30, 40):
        h.update([213.0, 213.0 + 40 * (i - 30)], i * 0.02)
    assert h.moving
