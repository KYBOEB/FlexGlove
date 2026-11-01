"""Пресеты конвейера в ядре и API: выбор, сохранение, применение к каналам, ошибки."""
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flexglove.app import create_app
from flexglove.dsp.presets import PRESETS
from flexglove.engine import Engine
from flexglove.parser import Frame
from flexglove.storage import Storage


def feed(e, values, t0=0.0):
    for i, v in enumerate(values):
        e._on_frame(Frame(kind="values", values=(v,)), t0 + i * 0.02)


def test_default_is_basic_and_switch_applies_to_existing_channels(tmp_path):
    st = Storage(tmp_path / "p.db")
    e = Engine(st, tmp_path)
    assert e.pipeline_setting == {"preset": "basic"}
    feed(e, [213.0] * 30)
    e.channels[0].cal.open, e.channels[0].cal.fist = 200.0, 800.0
    e.set_pipeline("one_euro")
    assert e.channels[0].pipeline.to_config()[1]["type"] == "one_euro"
    assert e.channels[0].cal.fist == 800.0                       # калибровка не сброшена
    assert Engine(Storage(tmp_path / "p.db"), tmp_path).pipeline_setting == {"preset": "one_euro"}
    assert e.snapshot()["pipeline"]["title"] == "One-Euro"


def test_custom_pipeline_and_errors(tmp_path):
    e = Engine(Storage(":memory:"), tmp_path)
    cfg = [{"type": "median", "n": 3}]
    e.set_pipeline("custom", cfg)
    feed(e, [1.0, 100.0, 1.0, 1.0])
    assert e.channels[0].filtered == 1.0
    with pytest.raises(ValueError):
        e.set_pipeline("custom", [{"type": "nope"}])
    assert e.pipeline_config == cfg                              # ошибка не меняет настройку


def test_corrupted_stored_setting_falls_back_to_basic(tmp_path):
    st = Storage(":memory:")
    st.put("pipeline", {"preset": "custom", "config": [{"type": "nope"}]})
    e = Engine(st, tmp_path)
    assert e.pipeline_config == PRESETS["basic"]["config"]


def test_motion_flag_in_snapshot(tmp_path):
    e = Engine(Storage(":memory:"), tmp_path)
    feed(e, [213.0] * 40)
    assert e.snapshot()["motion"] is False
    feed(e, [213.0 + 30 * i for i in range(15)], t0=1.0)
    assert e.snapshot()["motion"] is True


def test_pipeline_api(tmp_path):
    app = create_app(tmp_path / "t.db", demo=True)
    with TestClient(app) as c:
        info = c.get("/api/pipelines").json()
        assert [p["id"] for p in info["presets"]] == ["basic", "one_euro", "smooth"] and "one_euro" in info["filters"]
        assert c.post("/api/pipeline", json={"preset": "smooth"}).json()["title"] == "Сильное сглаживание"
        r = c.post("/api/pipeline", json={"preset": "custom", "config": [{"type": "median", "n": "x"}]})
        assert r.status_code == 400 and "Блок 1" in r.json()["detail"]
        assert c.post("/api/pipeline", json={"preset": "custom", "config": [{"type": "ema", "alpha": 0.5}]}).status_code == 200
        time.sleep(0.2)
        assert c.get("/api/state").json()["pipeline"]["config"] == [{"type": "ema", "alpha": 0.5}]
