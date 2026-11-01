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
