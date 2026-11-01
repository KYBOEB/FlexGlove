"""Обработка сигнала одного сенсора: фильтрация, калибровка, степень сгиба, состояние 0/1.

Параметры подобраны по записи с ESP32 (docs/analysis/2026-10-25-sensor-stream.md):
поток ~50 Гц, шум в покое — СКО ≈ 3 единицы АЦП, редкие выбросы до ±30 единиц.

Цепочка для каждого канала:
    сырое значение АЦП → медиана по 5 (убирает одиночные выбросы)
                        → экспоненциальное сглаживание (убирает шум)
                        → степень сгиба 0..1 (по калибровке или автокалибровке)
                        → состояние 0/1 с гистерезисом (для жестов-комбинаций)
"""
from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass

MEDIAN_WINDOW = 5
EMA_ALPHA = 0.35            # при 50 Гц постоянная времени ≈ 50 мс
MIN_CAL_SPAN = 15.0         # калибровка с меньшим размахом считается неудачной (≈ 5 СКО шума)
MIN_AUTO_SPAN = 30.0        # автокалибровка включается, когда отклонение от покоя ≥ 30 единиц (≈ 10 СКО)
AUTO_REST_SAMPLES = 25      # первые ~0,5 с после подключения считаем «ладонь раскрыта»
BEND_ON = 0.6               # гистерезис: «согнут», когда сгиб > 0.6
BEND_OFF = 0.4              #             «разогнут», когда сгиб < 0.4
STALE_AFTER_S = 1.0         # нет данных дольше — канал «молчит»
FLAT_AFTER_S = 3.0          # значение не меняется дольше — подозрительно: живой сенсор всегда шумит (СКО ≈ 3)


@dataclass
class Calibration:
    open: float | None = None   # значение при разогнутом пальце
    fist: float | None = None   # значение при согнутом пальце

    @property
    def valid(self) -> bool:
        return self.open is not None and self.fist is not None and abs(self.fist - self.open) >= MIN_CAL_SPAN


class ChannelProcessor:
    def __init__(self) -> None:
        self.cal = Calibration()
        self.reset()

    def reset(self) -> None:
        self.raw: float | None = None
        self.filtered: float | None = None
        self._window: deque[float] = deque(maxlen=MEDIAN_WINDOW)
        self._rest_samples: list[float] = []
        self.auto_rest: float | None = None
        self.auto_max_dev = 0.0
        self.bend = 0.0
        self.bit = 0
        self.last_update = 0.0
        self.last_change = 0.0      # когда сырое значение последний раз отличалось от предыдущего
        self.count = 0

    # ---------- вход ----------
    def update(self, value: float, now: float) -> None:
        if self.raw is None or value != self.raw:
            self.last_change = now
        self.raw = value
        self.count += 1
        self.last_update = now
        self._window.append(value)
        med = statistics.median(self._window)
        self.filtered = med if self.filtered is None else self.filtered + EMA_ALPHA * (med - self.filtered)
        self._update_auto()
        self.bend = self._compute_bend()
        if self.bend > BEND_ON:
            self.bit = 1
        elif self.bend < BEND_OFF:
            self.bit = 0

    def _update_auto(self) -> None:
        assert self.filtered is not None
        if self.auto_rest is None:
            self._rest_samples.append(self.filtered)
            if len(self._rest_samples) >= AUTO_REST_SAMPLES:
                self.auto_rest = statistics.median(self._rest_samples)
            return
        self.auto_max_dev = max(self.auto_max_dev, abs(self.filtered - self.auto_rest))

    def _compute_bend(self) -> float:
        f = self.filtered
        if f is None:
            return 0.0
        if self.cal.valid:
            b = (f - self.cal.open) / (self.cal.fist - self.cal.open)  # type: ignore[operator]
        elif self.auto_rest is not None and self.auto_max_dev >= MIN_AUTO_SPAN:
            b = abs(f - self.auto_rest) / self.auto_max_dev
        else:
            return 0.0
        return min(1.0, max(0.0, b))

    # ---------- состояние ----------
    @property
    def mode(self) -> str:
        if self.cal.valid:
            return "calibrated"
        if self.auto_rest is not None and self.auto_max_dev >= MIN_AUTO_SPAN:
            return "auto"
        return "uncalibrated"

    def stale(self, now: float) -> bool:
        return self.count == 0 or now - self.last_update > STALE_AFTER_S

    def flat(self, now: float) -> bool:
        """Данные приходят, но значение не меняется FLAT_AFTER_S — сенсор отключён/замкнут
        или прошивка шлёт только изменения."""
        return self.count > 0 and not self.stale(now) and now - self.last_change > FLAT_AFTER_S


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
