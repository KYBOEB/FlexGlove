"""Этап 4в: доводка 3D-экрана — стартовая поза демо, режим демо в состоянии, кадрирование и тень (статически)."""
import re
from pathlib import Path

from fastapi.testclient import TestClient

from flexglove import __version__
from flexglove.app import create_app
from flexglove.imu import ImuTracker
from flexglove.sources.mock import DEMO_START, MockSource

from test_stage4b import axes, run_tracker

WEB = Path(__file__).resolve().parents[1] / "flexglove" / "web"
ROOT = Path(__file__).resolve().parents[2]


def test_version_is_061():
    assert __version__ == "0.6.1"
    assert "## 0.6.1" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_demo_starts_fingers_up_back_to_viewer():
    """Демо при старте: пальцы вверх (тангаж +90°), тыльная сторона — к оператору (назад, −X мира)."""
    assert DEMO_START == (0.0, 90.0, 0.0)
    m = MockSource(lambda *_: None, lambda **_: None)
    assert m.angles == list(DEMO_START) and m.angle_target == list(DEMO_START)
    tr = ImuTracker()
    run_tracker(m, tr, 4.0)
    fingers, back = axes(tr.q_world)
    assert fingers[2] > 0.99                       # пальцы вверх
    assert back[0] < -0.99                         # тыльная сторона смотрит на оператора (камера — с его стороны)
    assert tr.snapshot(4.0)["pitch"] > 85


def test_demo_state_is_published_on_every_change():
    seen = []
    m = MockSource(lambda *_: None, lambda **kw: seen.append(kw))
    m.set_tour(True)
    assert seen[-1]["demo"] == {"auto": False, "tour": True, "angles": list(DEMO_START)}
    m.set_auto(True)
    assert seen[-1]["demo"]["auto"] and not seen[-1]["demo"]["tour"]
    m.set_angles(pitch=-90)
    assert seen[-1]["demo"]["angles"][1] == -90 and not seen[-1]["demo"]["tour"]
    m.set_bits("11111")
    assert seen[-1]["demo"] == {"auto": False, "tour": False, "angles": [0.0, -90.0, 0.0]}
    m.set_tour(True)
    m.set_tour(False)                              # «обзор» выключили — рука возвращается в исходную позу демо
    assert seen[-1]["demo"]["angles"] == list(DEMO_START)


def test_api_mode_is_visible_in_state(tmp_path):
    """Режим, включённый через API, виден в /api/state (галочки «обзор» / «перебор жестов» берут его оттуда)."""
    with TestClient(create_app(tmp_path / "t.db", demo=True)) as c:
        assert c.get("/api/state").json()["source"]["demo"]["angles"] == list(DEMO_START)
        c.post("/api/mock", json={"tour": True})
        demo = c.get("/api/state").json()["source"]["demo"]
        assert demo["tour"] is True and demo["auto"] is False
        c.post("/api/mock", json={"auto": True})
        demo = c.get("/api/state").json()["source"]["demo"]
        assert demo["auto"] is True and demo["tour"] is False
        c.post("/api/disconnect")
        assert c.get("/api/state").json()["source"].get("demo") is None


def test_ui_reflects_demo_state_and_pitch_range():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert "syncDemo(src.demo)" in js
    assert "setInsets(" in js
    # галочки больше не сбрасываются «вслепую» — только по состоянию
    assert "$(\"#mockAuto\").checked = $(\"#mockTour\").checked = false" not in js
    html = (WEB / "index.html").read_text(encoding="utf-8")
    sl = re.search(r'<input type="range" id="pitchSlider"[^>]*>', html).group(0)
    assert 'min="-90"' in sl and 'max="90"' in sl and 'value="90"' in sl


def test_hand3d_framing_and_shadow():
    """Кадр по ограничивающей сфере, «Вид по умолчанию» — тот же кадр; предплечья («клина») и пола-тени нет."""
    js = (WEB / "hand3d.js").read_text(encoding="utf-8")
    for marker in ("fitDistance()", "setViewOffset(", "setInsets(", "_updateBlob()", "resetView()"):
        assert marker in js, marker
    assert "_buildForearm" not in js and "FOREARM_LEN" not in js
    assert "ShadowMaterial" not in js
    assert "camera.zoom" not in js
