"""Демо-режим: имитирует перчатку с 5 сенсорами без железа.

Сигнал похож на настоящий (по записи с ESP32): значения АЦП около 213 в покое,
шум с СКО ≈ 3, редкие выбросы, поток 50 Гц, строка "v1;v2;v3;v4;v5".
При сгибе значение плавно растёт (постоянная времени ≈ 80 мс).
"""
from __future__ import annotations

import asyncio
import itertools
import random

from .base import Source

REST = [213.0, 228.0, 205.0, 220.0, 210.0]
BEND_DELTA = [620.0, 760.0, 700.0, 680.0, 560.0]   # прирост АЦП при полном сгибе (у каждого сенсора свой)
NOISE_STD = 3.0
SPIKE_PROB = 0.01
TAU_S = 0.08

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


class MockSource(Source):
    kind = "mock"

    def __init__(self, on_raw, on_status, n_channels: int = 5, rate_hz: float = 50.0):
        super().__init__(on_raw, on_status)
        self.n = n_channels
        self.rate_hz = rate_hz
        self.target = [0.0] * n_channels
        self.level = [0.0] * n_channels
        self.auto = False
        self._task: asyncio.Task | None = None
        self._rng = random.Random(42)

    @property
    def bits(self) -> str:
        return "".join("1" if t >= 0.5 else "0" for t in self.target)

    def set_bits(self, bits: str) -> None:
        self.target = [float(c == "1") for c in bits[: self.n].ljust(self.n, "0")]
        self.auto = False

    def set_bends(self, bends: list[float]) -> None:
        self.target = [min(1.0, max(0.0, float(b))) for b in (bends + [0.0] * self.n)[: self.n]]
        self.auto = False

    def set_auto(self, enabled: bool) -> None:
        self.auto = enabled

    async def start(self) -> None:
        self.on_status(kind=self.kind, name="Демо-перчатка", connected=True,
                       message=f"Демо-режим: {self.n} сенсоров, {self.rate_hz:.0f} Гц, данные имитируются")
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        period = 1.0 / self.rate_hz
        alpha = period / (TAU_S + period)
        seq = itertools.cycle(AUTO_SEQUENCE)
        step_left = 0.0
        while True:
            if self.auto:
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
            self.on_raw(";".join(values) + "\n")
            await asyncio.sleep(period)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self.on_status(kind=None, name=None, connected=False, message="Не подключено")
