"""Ориентация по IMU: кватернионы, смещение гироскопа, комплементарный фильтр и Madgwick на синтетике."""
import math

import pytest

from flexglove.dsp import synthetic as syn
from flexglove.dsp.orientation import (ComplementaryFilter, GyroBiasEstimator, MadgwickFilter, OrientationEstimator,
                                       imu_from_v03, q_angle_deg, q_conj, q_from_axis_angle, q_from_euler, q_mul,
                                       q_rotate, q_to_euler, tilt_from_accel)

FILTERS = [lambda: ComplementaryFilter(alpha=0.98), lambda: MadgwickFilter(beta=0.1)]


def angle_diff(a, b):
    return abs((a - b + 180) % 360 - 180)


# ---------------- кватернионы ----------------
def test_rotation_about_z_by_90():
    q = q_from_axis_angle((0, 0, 1), math.pi / 2)
    assert q_rotate(q, (1, 0, 0)) == pytest.approx((0, 1, 0), abs=1e-12)


@pytest.mark.parametrize("rpy", [(30, -20, 45), (-170, 10, -90), (5, 80, 120)])
def test_euler_roundtrip(rpy):
    assert q_to_euler(q_from_euler(*rpy)) == pytest.approx(rpy, abs=1e-9)


def test_mul_conj_identity_and_angle():
    q = q_from_euler(10, 20, 30)
    assert q_mul(q, q_conj(q)) == pytest.approx((1, 0, 0, 0), abs=1e-12)
    assert q_angle_deg(q, q_mul(q, q_from_axis_angle((1, 0, 0), math.radians(25)))) == pytest.approx(25, abs=1e-9)


@pytest.mark.parametrize("roll, pitch", [(0, 0), (30, 0), (0, -40), (25, 35)])
def test_tilt_from_gravity(roll, pitch):
    q = q_from_euler(roll, pitch, 0)
    acc = q_rotate(q_conj(q), (0, 0, 1))
    r, p, y = q_to_euler(tilt_from_accel(acc))
    assert (r, p) == pytest.approx((roll, pitch), abs=1e-9) and y == pytest.approx(0, abs=1e-9)


def test_units_v03():
    acc, gyro = imu_from_v03((12, -4, 998, 5, -12, 3))
    assert acc == pytest.approx((0.012, -0.004, 0.998)) and gyro == pytest.approx((0.5, -1.2, 0.3))


# ---------------- фильтры на синтетических вращениях ----------------
SCENARIOS = {
    "крен 60°": [syn.Rotation((1, 0, 0), 60, 1.0, 1.5)],
    "тангаж −45°": [syn.Rotation((0, 1, 0), -45, 1.0, 1.0)],
    "рысканье 90°": [syn.Rotation((0, 0, 1), 90, 1.0, 2.0)],
    "три оси подряд": [syn.Rotation((1, 0, 0), 30, 1.0, 1.0), syn.Rotation((0, 1, 0), -25, 2.5, 1.0),
                       syn.Rotation((0, 0, 1), 45, 4.0, 1.0)],
}


@pytest.mark.parametrize("make", FILTERS, ids=["complementary", "madgwick"])
@pytest.mark.parametrize("name", list(SCENARIOS))
def test_known_rotation_within_2_degrees(make, name):
    ts = syn.times(7.0)
    acc, gyro, truth = syn.ImuModel().generate(syn.omega_profile(SCENARIOS[name]), ts)
    f = make()
    worst = 0.0
    for i in range(len(ts)):
        q = f.update(gyro[i], acc[i], 0.02)
        if ts[i] > 0.5:
            est, ref = q_to_euler(q), q_to_euler(truth[i])
            worst = max(worst, *(angle_diff(a, b) for a, b in zip(est, ref)))
    assert worst < 2.0, f"{name}: ошибка до {worst:.2f}°"


@pytest.mark.parametrize("make", FILTERS, ids=["complementary", "madgwick"])
def test_tilt_recovers_from_wrong_start(make):
    """Стартовали «вслепую» с ошибкой 20° по крену — акселерометр вытягивает наклон за несколько секунд."""
    ts = syn.times(10.0)
    q0 = q_from_euler(15, -10, 0)
    acc, gyro, _ = syn.ImuModel().generate(lambda t: (0.0, 0.0, 0.0), ts, q0=q0)
    f = make()
    f.reset(q_from_euler(35, -10, 0))
    for i in range(len(ts)):
        q = f.update(gyro[i], acc[i], 0.02)
    r, p, _ = q_to_euler(q)
    assert angle_diff(r, 15) < 1.0 and angle_diff(p, -10) < 1.0


# ---------------- смещение гироскопа и дрейф рысканья ----------------
def run_still(seconds, bias_dps, use_estimator):
    ts = syn.times(seconds)
    acc, gyro, _ = syn.ImuModel(gyro_bias_dps=bias_dps).generate(lambda t: (0.0, 0.0, 0.0), ts)
    est = GyroBiasEstimator()
    f = MadgwickFilter()
    for i in range(len(ts)):
        g = gyro[i]
        if use_estimator:
            est.update(g, acc[i])
            g = est.correct(g)
        q = f.update(g, acc[i], 0.02)
    return q_to_euler(q), est


def test_yaw_drifts_without_bias_estimation_and_holds_with_it():
    bias = (0.3, -0.4, 0.5)                                    # °/с — типично для MPU6050 без калибровки
    (_, _, yaw_raw), _ = run_still(60, bias, use_estimator=False)
    (roll, pitch, yaw_fixed), est = run_still(60, bias, use_estimator=True)
    assert abs(yaw_raw) > 20                                   # ≈ 0,5 °/с × 60 с = 30°
    assert abs(yaw_fixed) < 1.5                                # за первую секунду оценка ещё не готова
    assert abs(roll) < 0.5 and abs(pitch) < 0.5                # наклоны держит сила тяжести
    assert est.calibrated and est.bias == pytest.approx(bias, abs=0.05)


def test_bias_not_learned_while_rotating():
    ts = syn.times(3.0)
    acc, gyro, _ = syn.ImuModel(gyro_bias_dps=(0.5, 0, 0)).generate(lambda t: (20.0 * math.sin(3 * t), 0.0, 0.0), ts)
    est = GyroBiasEstimator()
    for a, g in zip(acc, gyro):
        est.update(g, a)
    assert not est.calibrated


def test_orientation_estimator_zero_button():
    o = OrientationEstimator()
    ts = syn.times(4.0)
    acc, gyro, _ = syn.ImuModel().generate(syn.omega_profile([syn.Rotation((0, 0, 1), 40, 2.0, 1.0)]), ts,
                                           q0=q_from_euler(10, 0, 0))
    for a, g in zip(acc, gyro):
        o.update_raw((a[0] * 1000, a[1] * 1000, a[2] * 1000, g[0] * 10, g[1] * 10, g[2] * 10))
    assert angle_diff(o.euler[2], 40) < 2.0
    o.set_zero()
    assert all(abs(x) < 1e-6 for x in o.euler)
