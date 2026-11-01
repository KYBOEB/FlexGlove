"""Детектор «покой / движение».

По каналу: по окну n отсчётов (≈ 0,3 с) считается размах отфильтрованного сигнала.
    покой → движение: размах > motion_range (сразу — важна малая задержка);
    движение → покой: размах < rest_range непрерывно min_rest отсчётов (≈ 0,3 с) — без дребезга.
Пороги по умолчанию — из записи Захара: после фильтра шум в покое ±5 ед. (размах ≈ 10), поэтому
движением считаем размах больше 15, покоем — меньше 10.

По руке (HandSegmenter): движение, если движется хотя бы один канал.
Применение: окна для признаков и будущих динамических жестов, оценка смещения гироскопа только в покое (этап 4),
маска покоя для офлайн-оценки шума (tools/evaluate.py).
"""
from __future__ import annotations

from collections import deque

from .features import FeatureWindow

REST, MOTION = "rest", "motion"


class RestMotionDetector:
    def __init__(self, n: int = 15, motion_range: float = 15.0, rest_range: float = 10.0, min_rest: int = 15):
        if rest_range > motion_range:
            raise ValueError("rest_range ≤ motion_range")
        self.motion_range, self.rest_range, self.min_rest = motion_range, rest_range, min_rest
        self.window = FeatureWindow(n)
        self.reset()

    def reset(self) -> None:
        self.window.reset()
        self.state = REST
        self._calm = 0

    def update(self, x: float, t: float) -> str:
        f = self.window.update(x, t)
        if not self.window.full:
            return self.state
        if self.state == REST:
            if f.range > self.motion_range:
                self.state, self._calm = MOTION, 0
        else:
            self._calm = self._calm + 1 if f.range < self.rest_range else 0
            if self._calm >= self.min_rest:
                self.state = REST
        return self.state

    @property
    def moving(self) -> bool:
        return self.state == MOTION


class HandSegmenter:
    """Несколько каналов: рука движется, если движется хотя бы один палец."""

    def __init__(self, **detector_kwargs):
        self._kw = detector_kwargs
        self.channels: list[RestMotionDetector] = []

    def reset(self) -> None:
        self.channels = []

    def update(self, values: list[float | None], t: float) -> str:
        while len(self.channels) < len(values):
            self.channels.append(RestMotionDetector(**self._kw))
        for det, v in zip(self.channels, values):
            if v is not None:
                det.update(v, t)
        return MOTION if any(d.moving for d in self.channels) else REST

    @property
    def moving(self) -> bool:
        return any(d.moving for d in self.channels)


class ImuRestDetector:
    """Покой по IMU за окно n отсчётов (≈ 0,5 с):
    - разброс (СКО) каждой оси гироскопа < gyro_std_dps — нет дрожания и рывков;
    - средняя угловая скорость за вычетом известного смещения < max_rate_dps — нет и плавного равномерного
      вращения (иначе поворот с постоянной скоростью «выучился» бы как смещение);
    - модуль ускорения отличается от 1 g меньше чем на acc_tol_g.
    Пока смещение не измерено, порог скорости шире (max_rate_uncal_dps): смещение MPU6050 бывает до нескольких °/с.
    Вращение медленнее порога от покоя не отличить — ограничение любого MEMS-гироскопа без внешней опоры."""

    def __init__(self, n: int = 25, gyro_std_dps: float = 0.6, acc_tol_g: float = 0.05,
                 max_rate_dps: float = 2.0, max_rate_uncal_dps: float = 8.0):
        self.n, self.gyro_std, self.acc_tol = n, gyro_std_dps, acc_tol_g
        self.max_rate, self.max_rate_uncal = max_rate_dps, max_rate_uncal_dps
        self.reset()

    def reset(self) -> None:
        self._g: deque = deque(maxlen=self.n)
        self._a_ok: deque = deque(maxlen=self.n)
        self.state = MOTION

    def update(self, gyro_dps: tuple[float, float, float], acc_g: tuple[float, float, float],
               bias: tuple[float, float, float] | None = None) -> str:
        """bias — известное смещение гироскопа (None — ещё не измерено)."""
        self._g.append(gyro_dps)
        self._a_ok.append(abs(sum(c * c for c in acc_g) ** 0.5 - 1.0) < self.acc_tol)
        if len(self._g) < self.n:
            return self.state
        if not all(self._a_ok):
            self.state = MOTION
            return self.state
        b = bias or (0.0, 0.0, 0.0)
        limit = self.max_rate if bias is not None else self.max_rate_uncal
        quiet = True
        rate2 = 0.0
        for axis in range(3):
            vals = [g[axis] for g in self._g]
            m = sum(vals) / len(vals)
            quiet = quiet and (sum((v - m) ** 2 for v in vals) / len(vals)) ** 0.5 < self.gyro_std
            rate2 += (m - b[axis]) ** 2
        self.state = REST if quiet and rate2 ** 0.5 < limit else MOTION
        return self.state

    @property
    def at_rest(self) -> bool:
        return self.state == REST
