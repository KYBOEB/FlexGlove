"""Заготовка: динамические жесты — траектория во времени (без реализации).

Идея на будущее: жест = последовательность отсчётов (сгибы + IMU) между началом и концом движения
(детектор «покой / движение» из dsp/segmentation.py нарезает окна). Сравнение с эталонами — DTW (динамическое
выравнивание по времени) или небольшая модель по признакам из dsp/features.py.
"""
from __future__ import annotations

from .base import Event, Recognizer, Sample


class DynamicGestureRecognizer(Recognizer):
    name = "dynamic"

    def __init__(self, max_duration_s: float = 2.0):
        self.max_duration_s = max_duration_s

    def fit(self, sequences: list[list[Sample]], labels: list[str]) -> None:
        raise NotImplementedError("динамические жесты запланированы после этапа 4")

    def update(self, sample: Sample) -> list[Event]:
        raise NotImplementedError("динамические жесты запланированы после этапа 4")
