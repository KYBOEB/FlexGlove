"""Демо-режим: имитирует перчатку с 5 (или любым числом 1–8, например 2 — как прототип) сенсоров без железа.

Сигнал похож на настоящий (по записи с ESP32): значения АЦП около 213 в покое,
шум с СКО ≈ 3, редкие выбросы, поток 50 Гц, строка "v1;v2;v3;v4;v5".
При сгибе значение плавно растёт (постоянная времени ≈ 80 мс).

С этапа 4 демо имитирует и MPU6050 (протокол v0.3, строка "v1;…|ax;ay;az;gx;gy;gz"): кисть плавно следует за
целевыми креном/тангажем/рысканьем (ползунки и клавиши в демо-полосе, в автопереборе — медленное покачивание),
шум как у MPU6050 (гироскоп ≈ 0,03 °/с, акселерометр ≈ 3 мг), смещение нуля гироскопа ≈ 0,5 °/с,
квантование как в протоколе (1 мг, 0,1 °/с). Исходная поза — ладонь вниз, пальцы вперёд, оси модуля = оси руки.
Углы целевой позы (этап 4б): крен > 0 — поворот вокруг оси пальцев (у правой руки большой палец вверх),
тангаж > 0 — пальцы вверх, рысканье > 0 — пальцы влево.

Режим «обзор» (этап 4б): кисть и пальцы плавно проходят по ключевым позам (TOUR) — пальцы вверх и вниз, ладонью
вверх, кулак, «указательный + средний» и т. д., — чтобы рука на экране выглядела живой без железа.
"""
from __future__ import annotations

import asyncio
import itertools
import math
import random

from ..dsp.orientation import q_conj, q_from_euler, q_mul, q_rotate

from .base import Source

REST = [213.0, 228.0, 205.0, 220.0, 210.0]
BEND_DELTA = [620.0, 760.0, 700.0, 680.0, 560.0]   # прирост АЦП при полном сгибе (у каждого сенсора свой)
NOISE_STD = 3.0
SPIKE_PROB = 0.01
TAU_S = 0.08

IMU_GYRO_BIAS_DPS = (0.5, -0.4, 0.3)    # смещение нуля гироскопа, °/с
IMU_GYRO_NOISE_DPS = 0.03
IMU_ACC_NOISE_G = 0.003
IMU_MAX_RATE_DPS = 120.0                # как быстро кисть догоняет цель
IMU_TAU_S = 0.15

# автоперебор: (сгибы по каналам, длительность, с)
AUTO_SEQUENCE = [
    ([0, 0, 0, 0, 0], 1.5),
    ([0, 1, 0, 0, 0], 1.5),
    ([0, 0, 0, 0, 0], 1.0),
    ([0, 1, 1, 0, 0], 1.5),
    ([0, 0, 0, 0, 0], 1.0),
    ([0.3, 0.3, 0.3, 0.3, 0.3], 1.2),
    ([0.6, 0.6, 0.6, 0.6, 0.6], 1.2),
    ([1, 1, 1, 1, 1], 1.5),
    ([0, 0, 0, 0, 0], 1.5),
]

# «обзор»: (время, с; сгибы каналов 0..1; крен, тангаж, рысканье, °). Между позами — плавный переход (smoothstep).
TOUR = [
    (0.0,  [0, 0, 0, 0, 0],       0,   0,   0),     # вперёд, ладонь вниз
    (2.5,  [0, 0, 0, 0, 0],       0,  75,   0),     # пальцы вверх
    (4.5,  [0.8, 0, 0, 1, 1],   -15,  70,  20),     # указательный + средний
    (6.5,  [1, 1, 1, 1, 1],       0,  20, -20),     # кулак
    (8.5,  [0, 0, 0, 0, 0],      85,   5,   0),     # ребром, большой палец вверх
    (10.5, [0.8, 0, 1, 1, 1],     0, -10,  30),     # указательный
    (12.5, [0, 0, 0, 0, 0],     165,   0,   0),     # ладонью вверх
    (14.5, [0.3, 0.3, 0.3, 0.3, 0.3], 0, -75, 0),   # пальцы вниз, полусогнуты
    (17.0, [0, 0, 0, 0, 0],       0,   0,   0),
]
TOUR_PERIOD = TOUR[-1][0]


def tour_pose(t: float) -> tuple[list[float], list[float]]:
    """Сгибы (5 каналов) и углы (крен, тангаж, рысканье) режима «обзор» в момент t."""
    t %= TOUR_PERIOD
    for (t0, b0, *a0), (t1, b1, *a1) in zip(TOUR, TOUR[1:]):
        if t0 <= t <= t1:
            x = (t - t0) / (t1 - t0)
            k = x * x * (3 - 2 * x)
            return ([u + (v - u) * k for u, v in zip(b0, b1)], [u + (v - u) * k for u, v in zip(a0, a1)])
    return list(TOUR[0][1]), list(TOUR[0][2:])


class MockSource(Source):
    kind = "mock"

    def __init__(self, on_raw, on_status, n_channels: int = 5, rate_hz: float = 50.0, imu: bool = True):
        super().__init__(on_raw, on_status)
        self.imu = imu
        self.angle_target = [0.0, 0.0, 0.0]       # крен, тангаж, рысканье, °
        self.angles = [0.0, 0.0, 0.0]
        self._q_prev = None
        self.n = n_channels
        self.rate_hz = rate_hz
        self.target = [0.0] * n_channels
        self.level = [0.0] * n_channels
        self.auto = False
        self.tour = False
        self._t = 0.0
        self._task: asyncio.Task | None = None
        self._rng = random.Random(42)

    @property
    def bits(self) -> str:
        return "".join("1" if t >= 0.5 else "0" for t in self.target)

    def set_bits(self, bits: str) -> None:
        self.target = [float(c == "1") for c in bits[: self.n].ljust(self.n, "0")]
        self.auto = self.tour = False

    def set_bends(self, bends: list[float]) -> None:
        self.target = [min(1.0, max(0.0, float(b))) for b in (bends + [0.0] * self.n)[: self.n]]
        self.auto = self.tour = False

    def set_auto(self, enabled: bool) -> None:
        self.auto = enabled
        if enabled:
            self.tour = False

    def set_tour(self, enabled: bool) -> None:
        """Режим «обзор»: плавные движения кисти и пальцев по ключевым позам (TOUR)."""
        self.tour = enabled
        if enabled:
            self.auto = False
        else:                                           # остановиться в спокойной позе
            self.target = [0.0] * self.n
            self.angle_target = [0.0, 0.0, 0.0]

    def set_angles(self, roll: float | None = None, pitch: float | None = None, yaw: float | None = None) -> None:
        for i, v in enumerate((roll, pitch, yaw)):
            if v is not None:
                self.angle_target[i] = max(-170.0, min(170.0, float(v)))
        self.tour = False

    def imu_sample(self, t: float, dt: float) -> str:
        """Один отсчёт MPU6050 в единицах протокола v0.3: ax;ay;az (мг), gx;gy;gz (0,1 °/с)."""
        target = list(self.angle_target)
        if self.tour:
            target = tour_pose(t)[1]
        elif self.auto:                                   # медленное покачивание кисти в автопереборе
            target = [35 * math.sin(2 * math.pi * t / 6), 20 * math.sin(2 * math.pi * t / 9), 15 * math.sin(2 * math.pi * t / 14)]
        a = dt / (IMU_TAU_S + dt)
        for i in range(3):
            step = a * (target[i] - self.angles[i])
            lim = IMU_MAX_RATE_DPS * dt
            self.angles[i] += max(-lim, min(lim, step))
        roll, pitch, yaw = self.angles
        q = q_from_euler(roll, -pitch, yaw)            # в q_from_euler тангаж > 0 опускает пальцы
        prev = self._q_prev or q
        self._q_prev = q
        d = q_mul(q_conj(prev), q)                      # поворот за шаг в осях кисти
        if d[0] < 0:
            d = (-d[0], -d[1], -d[2], -d[3])
        s = math.sqrt(d[1] ** 2 + d[2] ** 2 + d[3] ** 2)
        ang = 2 * math.atan2(s, d[0])
        omega = [0.0, 0.0, 0.0] if s < 1e-12 else [math.degrees(ang / dt) * c / s for c in d[1:]]
        up = q_rotate(q_conj(q), (0.0, 0.0, 1.0))
        rnd = self._rng
        acc = [round(1000 * (u + rnd.gauss(0, IMU_ACC_NOISE_G))) for u in up]
        gyr = [round(10 * (w + b + rnd.gauss(0, IMU_GYRO_NOISE_DPS))) for w, b in zip(omega, IMU_GYRO_BIAS_DPS)]
        return ";".join(str(v) for v in acc + gyr)

    async def start(self) -> None:
        self.on_status(kind=self.kind, name="Демо-перчатка", connected=True,
                       message=f"Демо-режим: {self.n} сенсоров{' + MPU6050' if self.imu else ''}, {self.rate_hz:.0f} Гц, данные имитируются")
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        period = 1.0 / self.rate_hz
        alpha = period / (TAU_S + period)
        seq = itertools.cycle(AUTO_SEQUENCE)
        step_left = 0.0
        t_sim = 0.0
        while True:
            if self.tour:
                bends = tour_pose(t_sim)[0]
                self.target = [bends[i % 5] for i in range(self.n)]
            elif self.auto:
                step_left -= period
                if step_left <= 0:
                    bends, step_left = next(seq)
                    self.target = list(bends)
            values = []
            for i in range(self.n):
                self.level[i] += alpha * (self.target[i] - self.level[i])
                v = REST[i % 5] + BEND_DELTA[i % 5] * self.level[i] + self._rng.gauss(0, NOISE_STD)
                if self._rng.random() < SPIKE_PROB:
                    v += self._rng.choice([-1, 1]) * self._rng.uniform(15, 30)
                values.append(str(int(round(v))))
            line = ";".join(values)
            if self.imu:
                line += "|" + self.imu_sample(t_sim, period)
            t_sim += period
            self.on_raw(line + "\n")
            await asyncio.sleep(period)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self.on_status(kind=None, name=None, connected=False, message="Не подключено")
