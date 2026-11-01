"""Признаки сигнала по скользящему окну: среднее, СКО, размах, скорость изменения.

Окно задаётся числом отсчётов (n), а не секундами: перчатка измеряет равномерно (50 Гц), а время приёма
на ПК «скачет» из-за пачек BLE (см. filters.py, D-22). Скорость — наклон прямой по МНК в единицах/с
по оценке периода отсчётов.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from .filters import SampleClock


@dataclass(frozen=True)
class Features:
    n: int
    mean: float
    std: float
    range: float          # max − min
    slope: float          # ед./с (наклон прямой по МНК)


def window_features(xs: list[float], dt: float = 0.02) -> Features:
    """Признаки по готовому окну отсчётов с шагом dt."""
    n = len(xs)
    if n == 0:
        return Features(0, 0.0, 0.0, 0.0, 0.0)
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / n
    if n > 1:
        ic = (n - 1) / 2                                  # центр индексов
        sxx = sum((i - ic) ** 2 for i in range(n))
        slope = sum((i - ic) * (x - mean) for i, x in enumerate(xs)) / sxx / dt
    else:
        slope = 0.0
    return Features(n, mean, math.sqrt(var), max(xs) - min(xs), slope)


class FeatureWindow:
    """Скользящее окно одного канала: update(x, t) → Features по последним n отсчётам."""

    def __init__(self, n: int = 15):
        if n < 2:
            raise ValueError("окно признаков: n ≥ 2")
        self.n = n
        self._w: deque[float] = deque(maxlen=n)
        self._clock = SampleClock()

    def reset(self) -> None:
        self._w.clear()
        self._clock.reset()

    @property
    def full(self) -> bool:
        return len(self._w) == self.n

    def update(self, x: float, t: float) -> Features:
        dt = self._clock.tick(t)
        self._w.append(x)
        return window_features(list(self._w), dt)
