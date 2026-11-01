"""Калибровка канала: ручная «ладонь / кулак» и автоматическая от уровня покоя (перенесено из processing.py).

Параметры — по записи с ESP32 (docs/analysis/2026-10-25-sensor-stream.md): шум в покое СКО ≈ 3 единицы АЦП.
Все уровни — в единицах выхода конвейера фильтров (у всех фильтров коэффициент передачи на постоянном сигнале
равен 1, поэтому калибровка не зависит от выбранного пресета).
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

MIN_CAL_SPAN = 15.0         # ручная калибровка с меньшим размахом считается неудачной (≈ 5 СКО шума)
MIN_AUTO_SPAN = 30.0        # автокалибровка включается, когда отклонение от покоя ≥ 30 единиц (≈ 10 СКО)
AUTO_REST_SAMPLES = 25      # первые ~0,5 с после подключения считаем «ладонь раскрыта»


@dataclass
class Calibration:
    open: float | None = None   # значение при разогнутом пальце
    fist: float | None = None   # значение при согнутом пальце

    @property
    def valid(self) -> bool:
        return self.open is not None and self.fist is not None and abs(self.fist - self.open) >= MIN_CAL_SPAN


class AutoCalibrator:
    """Автокалибровка: медиана первых AUTO_REST_SAMPLES отсчётов — уровень покоя; дальше запоминается
    максимальное отклонение от него. Работает при любой полярности сенсора (сгиб увеличивает или уменьшает АЦП)."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.rest_samples: list[float] = []
        self.rest: float | None = None
        self.max_dev = 0.0

    def update(self, value: float) -> None:
        if self.rest is None:
            self.rest_samples.append(value)
            if len(self.rest_samples) >= AUTO_REST_SAMPLES:
                self.rest = statistics.median(self.rest_samples)
            return
        self.max_dev = max(self.max_dev, abs(value - self.rest))

    @property
    def active(self) -> bool:
        return self.rest is not None and self.max_dev >= MIN_AUTO_SPAN


class CalibrationCapture:
    """Сбор отфильтрованных значений за несколько секунд для калибровки «ладонь» / «кулак»."""

    def __init__(self, target: str, n_channels: int, started: float, duration: float = 1.5):
        self.target = target
        self.started = started
        self.duration = duration
        self.samples: list[list[float]] = [[] for _ in range(n_channels)]

    def add(self, channel: int, value: float) -> None:
        if channel < len(self.samples):
            self.samples[channel].append(value)

    def done(self, now: float) -> bool:
        return now - self.started >= self.duration

    def result(self) -> list[float | None]:
        return [statistics.median(s) if s else None for s in self.samples]
