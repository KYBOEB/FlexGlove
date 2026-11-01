"""Этап 4: оси датчика → оси руки, смещение гироскопа (замер и покой), «обнулить положение», крен кисти,
поворот захвата через контроллер, имитация IMU в демо и сквозной путь через ядро."""
import math
from pathlib import Path

import pytest

from flexglove.control import GripController
from flexglove.dsp.orientation import q_conj, q_from_axis_angle, q_from_euler, q_mul, q_rotate
from flexglove.engine import Engine
from flexglove.imu import (CAPTURE_S, DEFAULT_MOUNT, MOUNT_PRESETS, ImuTracker, apply_mount, mount_catalog,
                           twist_deg)
from flexglove.parser import Frame
from flexglove.sources.mock import IMU_GYRO_BIAS_DPS, MockSource
from flexglove.storage import Storage

DT = 0.02


def raw_from(acc_g, gyro_dps):
    """Отсчёт в единицах протокола v0.3 (мг, 0,1 °/с)."""
    return tuple(round(a * 1000) for a in acc_g) + tuple(round(g * 10) for g in gyro_dps)


def inv(m):          # матрица поворота: обратная = транспонированная
    return tuple(tuple(m[c][r] for c in range(3)) for r in range(3))


# ---------------- оси датчика → оси руки ----------------
def test_eight_mount_presets_are_proper_rotations():
    assert len(MOUNT_PRESETS) == 8 and DEFAULT_MOUNT == "x_fingers_z_up"
    assert MOUNT_PRESETS[DEFAULT_MOUNT]["matrix"] == ((1, 0, 0), (0, 1, 0), (0, 0, 1))
    for mid, p in MOUNT_PRESETS.items():
        m = p["matrix"]
        det = (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
               + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))
        assert det == 1, mid                               # поворот, не отражение
    assert len({p["matrix"] for p in MOUNT_PRESETS.values()}) == 8
    assert [m["id"] for m in mount_catalog()] == list(MOUNT_PRESETS)


@pytest.mark.parametrize("mount, sensor_vec, hand_vec", [
    ("x_left_z_up", (1, 0, 0), (0, 1, 0)),               # X модуля смотрит влево → это ось Y руки
    ("x_wrist_z_up", (1, 0, 0), (-1, 0, 0)),
    ("x_right_z_up", (0, 1, 0), (1, 0, 0)),
    ("x_fingers_z_down", (0, 0, 1), (0, 0, -1)),         # модуль лицом к коже
])
def test_mount_maps_axes(mount, sensor_vec, hand_vec):
    assert apply_mount(MOUNT_PRESETS[mount]["matrix"], sensor_vec) == hand_vec


@pytest.mark.parametrize("mount", list(MOUNT_PRESETS))
def test_any_mount_gives_same_hand_orientation(mount):
    """Одно и то же движение руки, модуль закреплён по-разному → одинаковая поза руки после пересчёта осей."""
    m_inv = inv(MOUNT_PRESETS[mount]["matrix"])
    tr = ImuTracker(mount)
    q = (1.0, 0.0, 0.0, 0.0)
    for i in range(300):                                # 2 с покоя, затем крен 40° за 2 с
        w = (20.0, 0.0, 0.0) if 100 <= i < 200 else (0.0, 0.0, 0.0)
        if i:
            q = q_mul(q, q_from_axis_angle((1, 0, 0), math.radians(w[0]) * DT))
        acc_hand = q_rotate(q_conj(q), (0, 0, 1))
        tr.update(raw_from(apply_mount(m_inv, acc_hand), apply_mount(m_inv, w)), i * DT)
        if i == 99:
            tr.set_zero()
    assert tr.wrist_roll_deg == pytest.approx(40, abs=2.0)


# ---------------- смещение гироскопа ----------------
def still_samples(n, bias=(0.5, -0.4, 0.3), t0=0.0, noise=None):
    import random
    rnd = random.Random(1)
    for i in range(n):
        g = tuple(b + (rnd.gauss(0, noise) if noise else 0.0) for b in bias)
        yield t0 + i * DT, raw_from((0, 0, 1), g)


def test_capture_measures_bias_in_two_seconds():
    tr = ImuTracker()
    tr.start_capture(0.0)
    for t, raw in still_samples(int(CAPTURE_S / DT) + 2, noise=0.03):
        tr.update(raw, t)
    assert tr.bias_ok and tr.bias_source == "замер 2 с" and tr.capture is None
    assert tr.bias == pytest.approx((0.5, -0.4, 0.3), abs=0.03)
    assert "откалиброван" in tr.message


def test_capture_rejected_when_hand_moves():
    tr = ImuTracker()
    tr.start_capture(0.0)
    for i in range(110):
        tr.update(raw_from((0, 0, 1), (30 * math.sin(i / 5), 0, 0)), i * DT)
    assert not tr.bias_ok and "двигалась" in tr.message


def test_bias_learned_from_first_rest_and_tracked():
    tr = ImuTracker()
    for t, raw in still_samples(40, bias=(1.0, 0.0, 0.0)):
        tr.update(raw, t)
    assert tr.bias_ok and tr.bias_source == "покой" and tr.bias[0] == pytest.approx(1.0, abs=1e-6)
    for t, raw in still_samples(1500, bias=(1.3, 0.0, 0.0), t0=1.0):    # температура сдвинула смещение
        tr.update(raw, t)
    assert tr.bias[0] == pytest.approx(1.3, abs=0.02)


def test_bias_not_learned_while_moving():
    tr = ImuTracker()
    for i in range(200):
        tr.update(raw_from((0, 0, 1), (40 * math.sin(i / 4), 10 * math.cos(i / 3), 0)), i * DT)
    assert not tr.bias_ok


def test_yaw_holds_with_bias_correction():
    tr = ImuTracker()
    tr.start_capture(0.0)
    for t, raw in still_samples(3000, noise=0.03):                      # 60 с неподвижно
        tr.update(raw, t)
    assert abs(tr.snapshot(60.0)["yaw"]) < 1.0


# ---------------- обнуление и крен кисти ----------------
def test_zero_makes_current_pose_origin():
    tr = ImuTracker()
    q = q_from_euler(25, -15, 40)
    acc = q_rotate(q_conj(q), (0, 0, 1))
    tr.filter.reset(q)
    for i in range(10):
        tr.update(raw_from(acc, (0, 0, 0)), i * DT)
    tr.set_zero()
    snap = tr.snapshot(0.2)
    assert abs(snap["roll"]) < 0.5 and abs(snap["pitch"]) < 0.5 and abs(snap["yaw"]) < 0.5
    assert snap["q"][0] == pytest.approx(1.0, abs=1e-4) and snap["zeroed"]


def test_twist_about_fingers_ignores_pitch():
    q = q_mul(q_from_axis_angle((0, 1, 0), math.radians(30)), q_from_axis_angle((1, 0, 0), math.radians(50)))
    assert twist_deg(q_from_axis_angle((1, 0, 0), math.radians(50))) == pytest.approx(50)
    assert twist_deg(q_from_axis_angle((0, 1, 0), math.radians(60))) == pytest.approx(0)
    assert twist_deg(q) == pytest.approx(50, abs=6)       # наклон не превращается в крен


# ---------------- поворот захвата через контроллер ----------------
def run_rot(c, angle, seconds, t0):
    t = t0
    while t < t0 + seconds:
        c.step_rotation(angle, t)
        t += DT
    return t


def test_rotation_follows_only_when_armed_and_respects_limits():
    c = GripController(link_ok=True)
    t = run_rot(c, 45, 1.0, 0.0)
    assert c.rot_command == 0 and c.rot_target == 45        # без ARM не двигается
    c.armed = True
    t = run_rot(c, 45, 0.25, t)
    assert c.rot_command == pytest.approx(90 * 0.25, abs=2.5)   # скорость ≤ 90 °/с
    t = run_rot(c, 150, 3.0, t)
    assert c.rot_command == pytest.approx(90)               # предел ±90°
    t = run_rot(c, -150, 3.0, t)
    assert c.rot_command == pytest.approx(-90)


def test_rotation_deadband_hold_and_link_loss():
    c = GripController(link_ok=True, armed=True)
    t = run_rot(c, 30, 1.0, 0.0)
    t = run_rot(c, 32, 1.0, t)
    assert c.rot_command == pytest.approx(30)               # 2° < мёртвой зоны 3°
    c.hold = True
    t = run_rot(c, 60, 1.0, t)
    assert c.rot_command == pytest.approx(30)               # фиксация
    c.hold = False
    c.set_link(False)
    t = run_rot(c, 60, 1.0, t)
    assert c.rot_command == pytest.approx(30) and not c.armed   # потеря связи: заморозка и снятие ARM
    run_rot(c, None, 1.0, t)
    assert c.rot_command == pytest.approx(30)               # нет ориентации — команда не меняется


# ---------------- имитация IMU ----------------
def test_mock_imu_line_format_and_physics():
    m = MockSource(lambda *a, **k: None, lambda **k: None, n_channels=2)
    samples = [m.imu_sample(i * DT, DT) for i in range(200)]
    vals = [tuple(int(v) for v in s.split(";")) for s in samples]
    assert all(len(v) == 6 for v in vals)
    ax, ay, az = (sum(v[k] for v in vals) / len(vals) for k in range(3))
    assert abs(ax) < 10 and abs(ay) < 10 and abs(az - 1000) < 10        # ладонь вниз: 1 g по Z
    gx = sum(v[3] for v in vals) / len(vals) / 10
    assert gx == pytest.approx(IMU_GYRO_BIAS_DPS[0], abs=0.05)         # в покое гироскоп = смещение
    m.set_angles(roll=45)
    for i in range(200, 400):
        last = m.imu_sample(i * DT, DT)
    ay = int(last.split(";")[1]) / 1000
    assert ay == pytest.approx(math.sin(math.radians(45)), abs=0.03)    # сила тяжести «ушла» в Y


def engine_with_mock():
    e = Engine(Storage(":memory:"), Path("."))
    e.source_info["connected"] = True
    m = MockSource(lambda *a, **k: None, lambda **k: None, n_channels=2)
    return e, m


def feed(e, m, seconds, t0):
    t = t0
    for _ in range(int(seconds / DT)):
        line = "213;300|" + m.imu_sample(t, DT)
        e._on_frame(Frame(kind="values", values=(213.0, 300.0), imu=tuple(float(x) for x in line.split("|")[1].split(";"))), t)
        e.tick(t)
        t += DT
    return t


def test_engine_end_to_end_capture_zero_and_gripper_rotation():
    e, m = engine_with_mock()
    t = feed(e, m, 2.5, 0.0)                                 # 2 с калибровки + обнуление
    o = e.snapshot()["orientation"]
    assert o["bias_ok"] and o["zeroed"] and not o["calibrating"]
    assert o["bias"] == pytest.approx(list(IMU_GYRO_BIAS_DPS), abs=0.05)
    assert any("откалиброван" in ev["text"] for ev in e.events)
    e.controller.armed = True
    m.set_angles(roll=40)
    t = feed(e, m, 3.0, t)
    o = e.snapshot()["orientation"]
    assert o["wrist_roll"] == pytest.approx(40, abs=2.5)
    assert e.controller.rot_command == pytest.approx(40, abs=4)
    e.imu_zero()
    assert abs(e.snapshot()["orientation"]["wrist_roll"]) < 0.5


def test_engine_settings_mount_and_hand(tmp_path):
    st = Storage(tmp_path / "s.db")
    e = Engine(st, tmp_path)
    e.set_settings(imu_mount="x_wrist_z_down", hand="left")
    e2 = Engine(Storage(tmp_path / "s.db"), tmp_path)
    assert e2.imu_tracker.mount == "x_wrist_z_down" and e2.hand == "left"
    with pytest.raises(ValueError):
        e.set_settings(imu_mount="nope")
    with pytest.raises(RuntimeError):
        e.imu_zero()


def test_imu_api_in_demo(tmp_path):
    import time

    from fastapi.testclient import TestClient

    from flexglove.app import create_app
    app = create_app(tmp_path / "t.db", demo=True)
    with TestClient(app) as c:
        assert len(c.get("/api/meta").json()["imu_mounts"]) == 8
        t0 = time.time()
        while time.time() - t0 < 5:
            o = c.get("/api/state").json()["orientation"]
            if o and o["zeroed"]:
                break
            time.sleep(0.1)
        assert o and o["bias_ok"] and o["zeroed"]
        assert c.post("/api/mock", json={"roll": 30}).json()["angles"][0] == 30
        time.sleep(1.5)
        assert c.get("/api/state").json()["orientation"]["wrist_roll"] == pytest.approx(30, abs=4)
        assert c.post("/api/imu/zero").status_code == 200
        assert c.post("/api/imu/calibrate").json()["calibrating"] is True
        assert c.post("/api/settings", json={"imu_mount": "bad"}).status_code == 400
        assert c.post("/api/settings", json={"hand": "left"}).json()["hand"] == "left"
