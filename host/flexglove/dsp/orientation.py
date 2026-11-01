"""Ориентация кисти по MPU6050 (ядро для этапа 4, решение D-19): кватернионы, смещение гироскопа,
комплементарный фильтр и фильтр Madgwick. Чистый Python, без numpy.

Соглашения
- Кватернион q = (w, x, y, z), единичный. Он поворачивает вектор из системы ДАТЧИКА в систему ЗЕМЛИ:
      v_земля = q ⊗ v_датчик ⊗ q*.
- Земля: ось Z вверх. Неподвижный акселерометр измеряет +1 g вдоль «вверх», выраженного в осях датчика:
      a_датчик = q* ⊗ (0, 0, 1) ⊗ q.
- Углы — по схеме Z-Y-X (рысканье ψ вокруг Z, затем тангаж θ вокруг Y, затем крен φ вокруг X), в градусах.
- Гироскоп — угловая скорость в осях датчика; производная ориентации: q̇ = ½ · q ⊗ (0, ω).
- Единицы протокола v0.3: ускорение — мг, угловая скорость — десятые доли °/с (imu_from_v03).

Почему рысканье дрейфует: наклоны (крен, тангаж) исправляются по направлению силы тяжести, а поворот вокруг
вертикали акселерометр не видит — рысканье получается только интегрированием гироскопа, и любая ошибка
смещения нарастает со временем. Поэтому важны оценка смещения в покое и кнопка «обнулить положение».
"""
from __future__ import annotations

import math
from collections import deque

Quat = tuple[float, float, float, float]
Vec = tuple[float, float, float]

G_MG = 1000.0   # 1 g в мг


# ---------------------------------------------------------------- кватернионы
def q_mul(a: Quat, b: Quat) -> Quat:
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (aw * bw - ax * bx - ay * by - az * bz,
            aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw)


def q_conj(q: Quat) -> Quat:
    return (q[0], -q[1], -q[2], -q[3])


def q_norm(q: Quat) -> float:
    return math.sqrt(sum(c * c for c in q))


def q_normalize(q: Quat) -> Quat:
    n = q_norm(q)
    if n == 0:
        return (1.0, 0.0, 0.0, 0.0)
    return (q[0] / n, q[1] / n, q[2] / n, q[3] / n)


def q_from_axis_angle(axis: Vec, angle_rad: float) -> Quat:
    n = math.sqrt(sum(c * c for c in axis))
    if n == 0 or angle_rad == 0:
        return (1.0, 0.0, 0.0, 0.0)
    s = math.sin(angle_rad / 2) / n
    return (math.cos(angle_rad / 2), axis[0] * s, axis[1] * s, axis[2] * s)


def q_rotate(q: Quat, v: Vec) -> Vec:
    """Поворот вектора: q ⊗ v ⊗ q* (из системы датчика в систему Земли)."""
    r = q_mul(q_mul(q, (0.0, *v)), q_conj(q))
    return (r[1], r[2], r[3])


def q_from_euler(roll_deg: float, pitch_deg: float, yaw_deg: float) -> Quat:
    cr, sr = math.cos(math.radians(roll_deg) / 2), math.sin(math.radians(roll_deg) / 2)
    cp, sp = math.cos(math.radians(pitch_deg) / 2), math.sin(math.radians(pitch_deg) / 2)
    cy, sy = math.cos(math.radians(yaw_deg) / 2), math.sin(math.radians(yaw_deg) / 2)
    return (cr * cp * cy + sr * sp * sy,
            sr * cp * cy - cr * sp * sy,
            cr * sp * cy + sr * cp * sy,
            cr * cp * sy - sr * sp * cy)


def q_to_euler(q: Quat) -> tuple[float, float, float]:
    """(крен, тангаж, рысканье) в градусах, схема Z-Y-X."""
    w, x, y, z = q
    roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    sp = max(-1.0, min(1.0, 2 * (w * y - z * x)))
    pitch = math.asin(sp)
    yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    return math.degrees(roll), math.degrees(pitch), math.degrees(yaw)


def q_angle_deg(a: Quat, b: Quat) -> float:
    """Угол между двумя ориентациями, градусы (0…180)."""
    d = abs(sum(x * y for x, y in zip(q_normalize(a), q_normalize(b))))
    return math.degrees(2 * math.acos(min(1.0, d)))


def integrate_gyro(q: Quat, gyro_rad_s: Vec, dt: float) -> Quat:
    """Точный шаг интегрирования при постоянной угловой скорости за dt: q ⊗ exp(ω·dt/2)."""
    wx, wy, wz = gyro_rad_s
    rate = math.sqrt(wx * wx + wy * wy + wz * wz)
    if rate * dt < 1e-12:
        return q
    return q_normalize(q_mul(q, q_from_axis_angle((wx, wy, wz), rate * dt)))


def tilt_from_accel(acc: Vec) -> Quat:
    """Начальная ориентация по силе тяжести (рысканье = 0)."""
    ax, ay, az = acc
    roll = math.degrees(math.atan2(ay, az))
    pitch = math.degrees(math.atan2(-ax, math.sqrt(ay * ay + az * az)))
    return q_from_euler(roll, pitch, 0.0)


# ---------------------------------------------------------------- единицы и смещение гироскопа
def imu_from_v03(raw: tuple[float, ...]) -> tuple[Vec, Vec]:
    """(ax, ay, az [мг], gx, gy, gz [0,1 °/с]) → (ускорение в g, угловая скорость в °/с)."""
    ax, ay, az, gx, gy, gz = raw
    return (ax / G_MG, ay / G_MG, az / G_MG), (gx / 10, gy / 10, gz / 10)


class GyroBiasEstimator:
    """Смещение нуля гироскопа по участкам покоя.

    Покой: за окно n отсчётов (≈ 1 с) разброс каждой оси гироскопа (СКО) < rest_std_dps и |a| отличается от 1 g
    меньше чем на acc_tol_g. Первое смещение — среднее по такому окну; дальше в покое оно медленно уточняется
    (экспоненциальное среднее). В движении смещение не трогается.
    """

    def __init__(self, n: int = 50, rest_std_dps: float = 0.6, acc_tol_g: float = 0.05, track_alpha: float = 0.01):
        self.n, self.rest_std, self.acc_tol, self.alpha = n, rest_std_dps, acc_tol_g, track_alpha
        self.reset()

    def reset(self) -> None:
        self._g: deque[Vec] = deque(maxlen=self.n)
        self._a_ok: deque[bool] = deque(maxlen=self.n)
        self.bias: Vec = (0.0, 0.0, 0.0)
        self.calibrated = False
        self.at_rest = False

    def update(self, gyro_dps: Vec, acc_g: Vec) -> bool:
        self._g.append(gyro_dps)
        self._a_ok.append(abs(math.sqrt(sum(c * c for c in acc_g)) - 1.0) < self.acc_tol)
        self.at_rest = False
        if len(self._g) < self.n or not all(self._a_ok):
            return False
        means, ok = [], True
        for axis in range(3):
            vals = [g[axis] for g in self._g]
            m = sum(vals) / len(vals)
            std = math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))
            ok = ok and std < self.rest_std
            means.append(m)
        if not ok:
            return False
        self.at_rest = True
        if not self.calibrated:
            self.bias, self.calibrated = (means[0], means[1], means[2]), True
        else:
            self.bias = tuple(b + self.alpha * (g - b) for b, g in zip(self.bias, gyro_dps))  # type: ignore[assignment]
        return True

    def correct(self, gyro_dps: Vec) -> Vec:
        return (gyro_dps[0] - self.bias[0], gyro_dps[1] - self.bias[1], gyro_dps[2] - self.bias[2])


# ---------------------------------------------------------------- фильтры ориентации
class ComplementaryFilter:
    """Гироскоп даёт быстрые и гладкие изменения, акселерометр — правильный «низ» без дрейфа.
    Каждый отсчёт: поворот по гироскопу, затем небольшая доля (1 − alpha) поправки наклона к направлению
    силы тяжести. alpha = 0,98 при 50 Гц ≈ постоянная времени коррекции 1 с. При сильных ускорениях
    (|a| дальше 0,15 g от 1 g) поправка пропускается — акселерометр тогда показывает не только тяжесть."""

    name = "complementary"

    def __init__(self, alpha: float = 0.98, acc_gate_g: float = 0.15):
        self.alpha, self.acc_gate = alpha, acc_gate_g
        self.reset()

    def reset(self, q: Quat | None = None) -> None:
        self.q: Quat = q or (1.0, 0.0, 0.0, 0.0)
        self._init = q is not None

    def update(self, gyro_dps: Vec, acc_g: Vec, dt: float) -> Quat:
        an = math.sqrt(sum(c * c for c in acc_g))
        if not self._init and an > 0:
            self.q, self._init = tilt_from_accel(acc_g), True
            return self.q
        self.q = integrate_gyro(self.q, tuple(math.radians(g) for g in gyro_dps), dt)  # type: ignore[arg-type]
        if an > 0 and abs(an - 1.0) < self.acc_gate:
            meas = (acc_g[0] / an, acc_g[1] / an, acc_g[2] / an)
            pred = q_rotate(q_conj(self.q), (0.0, 0.0, 1.0))         # где «верх» по текущей оценке (оси датчика)
            axis = (pred[1] * meas[2] - pred[2] * meas[1],             # pred × meas
                    pred[2] * meas[0] - pred[0] * meas[2],
                    pred[0] * meas[1] - pred[1] * meas[0])
            s = math.sqrt(sum(c * c for c in axis))
            if s > 1e-9:
                angle = math.atan2(s, sum(p * m for p, m in zip(pred, meas)))
                # поворот оценки в осях датчика на долю угла: предсказанный «верх» сдвигается к измеренному
                self.q = q_normalize(q_mul(self.q, q_from_axis_angle(axis, -(1 - self.alpha) * angle)))
        return self.q


class MadgwickFilter:
    """Фильтр Madgwick (2010), вариант для акселерометра и гироскопа (6 осей): интегрирование гироскопа
    плюс шаг градиентного спуска, уменьшающий расхождение предсказанной и измеренной силы тяжести.
    beta (рад/с) — вес коррекции; 0,1 — типичное значение для MEMS-гироскопа вроде MPU6050."""

    name = "madgwick"

    def __init__(self, beta: float = 0.1):
        self.beta = beta
        self.reset()

    def reset(self, q: Quat | None = None) -> None:
        self.q: Quat = q or (1.0, 0.0, 0.0, 0.0)
        self._init = q is not None

    def update(self, gyro_dps: Vec, acc_g: Vec, dt: float) -> Quat:
        an = math.sqrt(sum(c * c for c in acc_g))
        if not self._init and an > 0:
            self.q, self._init = tilt_from_accel(acc_g), True
            return self.q
        q0, q1, q2, q3 = self.q
        gx, gy, gz = (math.radians(g) for g in gyro_dps)
        # производная по гироскопу: ½ q ⊗ (0, ω)
        d0 = 0.5 * (-q1 * gx - q2 * gy - q3 * gz)
        d1 = 0.5 * (q0 * gx + q2 * gz - q3 * gy)
        d2 = 0.5 * (q0 * gy - q1 * gz + q3 * gx)
        d3 = 0.5 * (q0 * gz + q1 * gy - q2 * gx)
        if an > 0:
            ax, ay, az = (c / an for c in acc_g)
            # целевая функция f = q* ⊗ (0,0,1) ⊗ q − a и её градиент (Якобиан^T · f)
            f1 = 2 * (q1 * q3 - q0 * q2) - ax
            f2 = 2 * (q0 * q1 + q2 * q3) - ay
            f3 = 2 * (0.5 - q1 * q1 - q2 * q2) - az
            s0 = -2 * q2 * f1 + 2 * q1 * f2
            s1 = 2 * q3 * f1 + 2 * q0 * f2 - 4 * q1 * f3
            s2 = -2 * q0 * f1 + 2 * q3 * f2 - 4 * q2 * f3
            s3 = 2 * q1 * f1 + 2 * q2 * f2
            sn = math.sqrt(s0 * s0 + s1 * s1 + s2 * s2 + s3 * s3)
            if sn > 0:
                d0 -= self.beta * s0 / sn
                d1 -= self.beta * s1 / sn
                d2 -= self.beta * s2 / sn
                d3 -= self.beta * s3 / sn
        self.q = q_normalize((q0 + d0 * dt, q1 + d1 * dt, q2 + d2 * dt, q3 + d3 * dt))
        return self.q


class OrientationEstimator:
    """Сборка для этапа 4: сырые IMU v0.3 → смещение гироскопа → фильтр → кватернион и углы;
    «обнулить положение» (текущая поза становится исходной)."""

    def __init__(self, method: str = "madgwick", rate_hz: float = 50.0, **kwargs):
        self.filter = MadgwickFilter(**kwargs) if method == "madgwick" else ComplementaryFilter(**kwargs)
        self.bias = GyroBiasEstimator()
        self.dt = 1.0 / rate_hz
        self.zero: Quat = (1.0, 0.0, 0.0, 0.0)

    def reset(self) -> None:
        self.filter.reset()
        self.bias.reset()
        self.zero = (1.0, 0.0, 0.0, 0.0)

    def update_raw(self, raw_v03: tuple[float, ...], dt: float | None = None) -> Quat:
        acc, gyro = imu_from_v03(raw_v03)
        self.bias.update(gyro, acc)
        return self.filter.update(self.bias.correct(gyro), acc, dt or self.dt)

    def set_zero(self) -> None:
        self.zero = q_conj(self.filter.q)

    @property
    def q(self) -> Quat:
        return q_normalize(q_mul(self.zero, self.filter.q))

    @property
    def euler(self) -> tuple[float, float, float]:
        return q_to_euler(self.q)
