"""Этап 4б: абсолютная ориентация по гравитации (D-28), мастер калибровки, режим «обзор» в демо, кэш статики."""
import math
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flexglove import __version__
from flexglove.app import create_app
from flexglove.calib_wizard import CalibrationWizard, grade, robust_std
from flexglove.dsp.orientation import q_rotate
from flexglove.imu import ImuTracker, heading_deg
from flexglove.sources.mock import TOUR, MockSource, tour_pose

WEB = Path(__file__).resolve().parents[1] / "flexglove" / "web"
DT = 0.02


def run_tracker(mock: MockSource, tr: ImuTracker, seconds: float, t0: float = 0.0) -> float:
    """Прогнать имитацию MPU6050 через трекер (как ядро: первый отсчёт запускает замер 2 с и обнуление)."""
    t = t0
    for _ in range(int(seconds / DT)):
        raw = tuple(float(x) for x in mock.imu_sample(t, DT).split(";"))
        if tr.count == 0:
            tr.start_capture(t, zero_after=True)
        tr.update(raw, t)
        t += DT
    return t


def axes(q):
    """Пальцы и тыльная сторона руки в осях мира (Z — вверх, X — «вперёд» после обнуления рысканья)."""
    return q_rotate(q, (1.0, 0.0, 0.0)), q_rotate(q, (0.0, 0.0, 1.0))


# ---------------- 4 позы: ориентация абсолютная по гравитации ----------------
POSES = {
    #  имя: (крен, тангаж) в имитации; проверка по осям мира
    "fingers_up": ((0, 88), lambda f, b: f[2] > 0.97),
    "fingers_down": ((0, -88), lambda f, b: f[2] < -0.97),
    "forward_palm_down": ((0, 0), lambda f, b: f[0] > 0.97 and b[2] > 0.97),
    "forward_palm_up": ((170, 0), lambda f, b: f[0] > 0.97 and b[2] < -0.95),
}


@pytest.mark.parametrize("name", POSES)
def test_pose_is_absolute_when_connected_in_it(name):
    """Перчатку подключили сразу в позе (с любым курсом): модель показывает ту же позу, а не «пальцы вверх»."""
    (roll, pitch), check = POSES[name]
    m = MockSource(lambda *_: None, lambda **_: None)
    m.angle_target = [roll, pitch, 50.0]
    m.angles = [roll, pitch, 50.0]                 # рука уже в позе в момент подключения
    tr = ImuTracker()
    run_tracker(m, tr, 4.0)
    assert tr.zeroed                               # автообнуление после замера гироскопа прошло
    f, b = axes(tr.q_world)
    assert check(f, b), (name, f, b)
    snap = tr.snapshot(4.0)
    if name == "fingers_up":
        assert snap["pitch"] > 80
    if name == "fingers_down":
        assert snap["pitch"] < -80


@pytest.mark.parametrize("name", POSES)
def test_pose_after_zero_in_other_pose(name):
    """Обнулили, держа руку опущенной; затем поза меняется — наклоны по гравитации, рысканье от обнуления."""
    (roll, pitch), check = POSES[name]
    m = MockSource(lambda *_: None, lambda **_: None)
    m.angle_target = [0.0, -88.0, -30.0]
    m.angles = list(m.angle_target)
    tr = ImuTracker()
    t = run_tracker(m, tr, 3.0)
    m.angle_target = [roll, pitch, -30.0]          # курс тот же, меняются только наклоны
    run_tracker(m, tr, 5.0, t)
    f, b = axes(tr.q_world)
    assert check(f, b), (name, f, b)


def test_zero_changes_only_yaw():
    m = MockSource(lambda *_: None, lambda **_: None)
    m.angle_target = m.angles = [20.0, 30.0, 70.0]
    tr = ImuTracker()
    run_tracker(m, tr, 3.0)
    before = tr.snapshot(3.0)
    tr.set_zero()
    after = tr.snapshot(3.0)
    assert after["roll"] == pytest.approx(before["roll"], abs=0.5)
    assert after["pitch"] == pytest.approx(before["pitch"], abs=0.5)
    assert after["pitch"] == pytest.approx(30, abs=2) and after["roll"] == pytest.approx(20, abs=2)
    assert abs(after["yaw"]) < 1


def test_heading_continuous_through_vertical():
    """Курс не прыгает при наклоне пальцев от горизонта к вертикали (вверх и вниз)."""
    from flexglove.dsp.orientation import q_from_euler
    for yaw in (-120, 0, 45, 170):
        hs = [heading_deg(q_from_euler(0, -p, yaw)) for p in range(-89, 90, 3)]
        diffs = [abs((a - yaw + 180) % 360 - 180) for a in hs]
        assert max(diffs) < 1.0, (yaw, max(diffs))


# ---------------- демо: «обзор» ----------------
def test_tour_is_smooth_and_covers_key_poses():
    assert TOUR[0][1:] == TOUR[-1][1:]             # цикл замкнут — без рывка на стыке
    prev_b, prev_a = tour_pose(0.0)
    max_rate = 0.0
    for i in range(1, 2000):
        b, a = tour_pose(i * 0.01)
        max_rate = max(max_rate, max(abs(x - y) for x, y in zip(a, prev_a)) / 0.01)
        assert max(abs(x - y) for x, y in zip(b, prev_b)) < 0.03
        prev_b, prev_a = b, a
    assert max_rate < 150                            # °/с — плавно
    pitches = [tour_pose(t)[1][1] for t in (2.5, 14.5)]
    assert pitches[0] > 60 and pitches[1] < -60      # есть «пальцы вверх» и «пальцы вниз»


def test_tour_api_in_demo(tmp_path):
    with TestClient(create_app(tmp_path / "t.db", demo=True)) as c:
        r = c.post("/api/mock", json={"tour": True}).json()
        assert r["tour"] and not r["auto"]
        assert c.post("/api/mock", json={"yaw": 30}).json()["tour"] is False


# ---------------- мастер калибровки ----------------
def test_grades():
    assert grade(None, 3) == "no_data"
    assert grade(5, 3) == "flat"
    assert grade(-40, 3) == "weak"
    assert grade(100, 20) == "weak"
    assert grade(-500, 3) == "good"
    assert robust_std([10, 10, 10, 10, 200]) == 0          # выброс не раздувает шум


def test_wizard_requires_open_before_fist():
    w = CalibrationWizard()
    with pytest.raises(RuntimeError):
        w.start("fist", 2, 0.0)


def wait(c, cond, timeout=6.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = c.get("/api/state").json()
        if cond(st):
            return st
        time.sleep(0.05)
    raise AssertionError("не дождались состояния")


def test_wizard_api_in_demo(tmp_path):
    with TestClient(create_app(tmp_path / "t.db", demo=True)) as c:
        wait(c, lambda s: s["mode"] == "values" and s["n_channels"] == 5)
        assert c.post("/api/calibration/wizard/fist").status_code == 409        # сначала ладонь
        c.post("/api/mock", json={"bends": [0, 0, 0, 0, 0]})
        r = c.post("/api/calibration/wizard/open?duration=0.6")
        assert r.status_code == 200 and r.json()["capturing"] == "open"
        st = wait(c, lambda s: s["wizard"]["step"] == "open_done")
        assert st["wizard"]["capturing"] is None
        c.post("/api/mock", json={"bends": [1, 1, 1, 1, 0]})                    # мизинец «не работает»
        time.sleep(0.5)
        c.post("/api/calibration/wizard/fist?duration=0.6")
        st = wait(c, lambda s: s["wizard"]["step"] == "review")
        rows = st["wizard"]["summary"]
        assert len(rows) == 5
        assert [r["grade"] for r in rows[:4]] == ["good"] * 4 and rows[4]["grade"] == "flat"
        assert rows[1]["span"] > 500 and 0 < rows[1]["noise"] < 8
        # до «Сохранить» калибровка не записана
        assert all(ch["cal"]["open"] is None for ch in st["channels"])
        assert c.post("/api/calibration/wizard-save").json()["valid_channels"] == 4
        st = c.get("/api/state").json()
        assert st["wizard"]["step"] == "idle"
        assert [ch["mode"] for ch in st["channels"][:4]] == ["calibrated"] * 4
        assert c.post("/api/calibration/wizard-save").status_code == 409        # нечего сохранять


def test_wizard_needs_value_stream(tmp_path):
    with TestClient(create_app(tmp_path / "t.db")) as c:
        assert c.post("/api/calibration/wizard/open").status_code == 409
        assert c.post("/api/calibration/wizard/open?duration=60").status_code == 400


# ---------------- кэш статики и версия ----------------
def test_index_links_static_with_version(tmp_path):
    with TestClient(create_app(tmp_path / "t.db")) as c:
        r = c.get("/")
        html = r.text
        assert r.headers["cache-control"] == "no-store"
        assert "__V__" not in html
        refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
        local = [x for x in refs if x.endswith((".js", ".css")) or ".js?" in x or ".css?" in x]
        assert local, "в index.html нет ссылок на js/css"
        for ref in local:
            assert ref.endswith(f"?v={__version__}"), ref
        imports = re.search(r'<script type="importmap">(.*?)</script>', html, re.S).group(1)
        assert f"three.module.min.js?v={__version__}" in imports
        s = c.get(f"/static/app.js?v={__version__}")
        assert s.status_code == 200 and s.headers["cache-control"] == "no-cache"
        assert "etag" in s.headers                       # повторный запрос — 304 без тела
        again = c.get("/static/app.js", headers={"If-None-Match": s.headers["etag"]})
        assert again.status_code == 304




def test_robot_scene_and_controller_hidden_from_ui():
    """D-26: «Рабочей зоны» с захватом и карточки контроллера в интерфейсе нет (код ядра остаётся)."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for marker in ('id="scene"', "Рабочая зона", "Контроллер команд", 'id="jawL"'):
        assert marker not in html, marker
    assert (WEB.parent / "control.py").exists()


# ---------------- запуск ярлыком: не открывать старую версию ----------------
def test_ping_reports_version_and_folder(tmp_path):
    from flexglove.app import PROJECT_ROOT
    from flexglove.desktop import same_instance
    with TestClient(create_app(tmp_path / "t.db")) as c:
        info = c.get("/api/ping").json()
        assert info["version"] == __version__ and Path(info["root"]) == PROJECT_ROOT
        assert same_instance(info)
        assert not same_instance({**info, "version": "0.5.0"})                 # старая версия
        assert not same_instance({**info, "root": str(tmp_path)})              # копия из другой папки
        assert not same_instance({"app": "flexglove", "version": "0.5.0"})     # v0.5.0 не сообщала папку
        assert c.post("/api/shutdown").status_code == 409                      # не через ярлык — не останавливаем


def test_shutdown_calls_desktop_hook(tmp_path):
    app = create_app(tmp_path / "t.db")
    called = []
    app.state.shutdown = lambda: called.append(True)
    with TestClient(app) as c:
        assert c.post("/api/shutdown").json() == {"stopping": True}
        time.sleep(0.4)
    assert called
