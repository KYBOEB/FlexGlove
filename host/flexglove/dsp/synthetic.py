"""Генератор синтетических сигналов для тестов и офлайн-оценки (без numpy).

Гибкий сенсор: «истинный» сгиб 0..1 → АЦП = покой + размах · сгиб, отклик сенсора — инерционное звено
с постоянной времени tau (≈ 80 мс у демо), шум (СКО), одиночные выбросы, медленный дрейф.

IMU: известное вращение → «идеальные» показания акселерометра и гироскопа + шум и смещение гироскопа.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Callable

Vec = tuple[float, float, float]
Quat = tuple[float, float, float, float]

RATE_HZ = 50.0


def times(duration_s: float, rate_hz: float = RATE_HZ) -> list[float]:
    return [i / rate_hz for i in range(int(round(duration_s * rate_hz)))]


# ---------------------------------------------------------------- профили «истинного» сгиба 0..1
def step(t0: float, level: float = 1.0) -> Callable[[float], float]:
    return lambda t: level if t >= t0 else 0.0


def ramp(t0: float, t1: float, level: float = 1.0) -> Callable[[float], float]:
    return lambda t: 0.0 if t < t0 else level if t >= t1 else level * (t - t0) / (t1 - t0)


def protocol(rest_s: float = 5.0, bends: int = 5, bend_s: float = 2.0, release_s: float = 2.0) -> Callable[[float], float]:
    """Протокол записи этапа 2: покой → bends × (сгиб / разгиб) → покой."""
    period = bend_s + release_s

    def f(t: float) -> float:
        k = t - rest_s
        if k < 0 or k >= bends * period:
            return 0.0
        return 1.0 if (k % period) < bend_s else 0.0
    return f


def protocol_duration(rest_s: float = 5.0, bends: int = 5, bend_s: float = 2.0, release_s: float = 2.0) -> float:
    return 2 * rest_s + bends * (bend_s + release_s)


# ---------------------------------------------------------------- сенсор
@dataclass
class SensorModel:
    rest: float = 1200.0          # АЦП в покое
    span: float = 450.0           # прирост АЦП при полном сгибе (может быть отрицательным)
    tau_s: float = 0.08           # инерционность сенсора
    noise: float = 3.0            # СКО шума, ед. АЦП
    spike_prob: float = 0.01      # вероятность выброса на отсчёт
    spike_amp: float = 25.0
    drift_per_s: float = 0.0
    seed: int = 1
    quantize: bool = True

    def generate(self, profile: Callable[[float], float], ts: list[float]) -> tuple[list[float], list[float]]:
        """→ (значения АЦП с шумом, «чистый» сигнал без шума и выбросов)."""
        rnd = random.Random(self.seed)
        level, clean, noisy, prev_t = 0.0, [], [], None
        for t in ts:
            dt = 1 / RATE_HZ if prev_t is None else t - prev_t
            prev_t = t
            level += (dt / (self.tau_s + dt)) * (profile(t) - level) if self.tau_s > 0 else profile(t) - level
            c = self.rest + self.span * level + self.drift_per_s * t
            v = c + rnd.gauss(0, self.noise)
            if rnd.random() < self.spike_prob:
                v += rnd.choice((-1, 1)) * self.spike_amp
            clean.append(c)
            noisy.append(float(round(v)) if self.quantize else v)
        return noisy, clean


def ble_jitter(ts: list[float], seed: int = 3, burst_prob: float = 0.08, max_delay_s: float = 0.12) -> list[float]:
    """Время приёма как у BLE: иногда пакеты задерживаются и приходят пачкой (порядок сохраняется)."""
    rnd = random.Random(seed)
    out, hold_until = [], -1.0
    for t in ts:
        if t > hold_until and rnd.random() < burst_prob:
            hold_until = t + rnd.uniform(0.04, max_delay_s)
        arrival = max(t, hold_until) + rnd.uniform(0, 0.002)
        out.append(max(arrival, out[-1] if out else arrival))
    return out


def to_csv(ts: list[float], channels: list[list[float]], names: list[str] | None = None) -> str:
    """Запись в формате приложения (Excel: «;», запятая в дробях)."""
    names = names or [f"S{i + 1} (ADC)" for i in range(len(channels))]
    lines = [";".join(["Time (s)"] + names)]
    for i, t in enumerate(ts):
        vals = [str(int(c[i])) if float(c[i]).is_integer() else f"{c[i]:.2f}".replace(".", ",") for c in channels]
        lines.append(";".join([f"{t:.3f}".replace(".", ",")] + vals))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- IMU
@dataclass
class ImuModel:
    gyro_bias_dps: Vec = (0.0, 0.0, 0.0)
    gyro_noise_dps: float = 0.05
    acc_noise_g: float = 0.003
    seed: int = 5

    def generate(self, omega_dps: Callable[[float], Vec], ts: list[float], q0: Quat = (1.0, 0.0, 0.0, 0.0)
                 ) -> tuple[list[Vec], list[Vec], list[Quat]]:
        """Истинная угловая скорость ω(t) в осях датчика (°/с) → (акселерометр [g], гироскоп [°/с], истинные q)."""
        from .orientation import integrate_gyro, q_conj, q_rotate
        rnd = random.Random(self.seed)
        q, acc, gyro, truth = q0, [], [], []
        for i, t in enumerate(ts):
            if i:
                w_prev = omega_dps(ts[i - 1])
                q = integrate_gyro(q, tuple(math.radians(c) for c in w_prev), t - ts[i - 1])  # type: ignore[arg-type]
            w = omega_dps(t)
            up = q_rotate(q_conj(q), (0.0, 0.0, 1.0))
            acc.append(tuple(c + rnd.gauss(0, self.acc_noise_g) for c in up))  # type: ignore[arg-type]
            gyro.append(tuple(w[k] + self.gyro_bias_dps[k] + rnd.gauss(0, self.gyro_noise_dps) for k in range(3)))  # type: ignore[arg-type]
            truth.append(q)
        return acc, gyro, truth


@dataclass
class Rotation:
    """Участок вращения: ось (в осях датчика), угол, время начала и длительность."""
    axis: Vec
    angle_deg: float
    t0: float
    duration_s: float


def omega_profile(rotations: list[Rotation]) -> Callable[[float], Vec]:
    """Кусочно-постоянная угловая скорость из списка поворотов (каждый — с постоянной скоростью)."""
    def f(t: float) -> Vec:
        wx = wy = wz = 0.0
        for r in rotations:
            if r.t0 <= t < r.t0 + r.duration_s:
                n = math.sqrt(sum(c * c for c in r.axis))
                rate = r.angle_deg / r.duration_s
                wx += r.axis[0] / n * rate
                wy += r.axis[1] / n * rate
                wz += r.axis[2] / n * rate
        return (wx, wy, wz)
    return f


@dataclass
class ProtocolRecording:
    """Готовая синтетическая запись по протоколу этапа 2 (для tools/evaluate.py и тестов)."""
    sensors: list[SensorModel] = field(default_factory=lambda: [SensorModel(), SensorModel(rest=1500, span=0, seed=2)])
    jitter: bool = True
    seed: int = 3

    def build(self) -> tuple[list[float], list[list[float]], list[list[float]]]:
        ts = times(protocol_duration())
        prof = protocol()
        noisy, clean = zip(*(s.generate(prof, ts) for s in self.sensors))
        arrival = ble_jitter(ts, seed=self.seed) if self.jitter else ts
        return arrival, [list(n) for n in noisy], [list(c) for c in clean]
