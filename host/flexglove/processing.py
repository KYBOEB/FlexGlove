"""Обработка сигнала одного сенсора: конвейер фильтров → калибровка → степень сгиба → состояние 0/1.

С этапа 3 блоки живут в модуле dsp/ (решение D-18), здесь — сборка канала с прежним интерфейсом:

    сырое значение АЦП → конвейер фильтров (пресет «Базовый»: медиана 5 → сглаживание α 0,35; dsp/filters.py)
                        → степень сгиба 0..1 по калибровке или автокалибровке (dsp/calibration.py, dsp/normalize.py)
                        → состояние 0/1 с гистерезисом 0,4 / 0,6 (dsp/recognizers/threshold.py)

Параметры подобраны по записи с ESP32 (docs/analysis/2026-10-25-sensor-stream.md):
поток ~50 Гц, шум в покое — СКО ≈ 3 единицы АЦП, редкие выбросы до ±30 единиц.
"""
from __future__ import annotations

from typing import Any

from .dsp import normalize
from .dsp.calibration import (  # noqa: F401 - прежние имена для остального кода и инструментов
    AUTO_REST_SAMPLES,
    MIN_AUTO_SPAN,
    MIN_CAL_SPAN,
    AutoCalibrator,
    Calibration,
    CalibrationCapture,
)
from .dsp.filters import Pipeline
from .dsp.presets import DEFAULT_PRESET, PRESETS
from .dsp.recognizers.threshold import BEND_OFF, BEND_ON, Hysteresis

MEDIAN_WINDOW = 5
EMA_ALPHA = 0.35            # при 50 Гц постоянная времени ≈ 50 мс (пресет «Базовый»)
STALE_AFTER_S = 1.0         # нет данных дольше — канал «молчит»
FLAT_AFTER_S = 3.0          # значение не меняется дольше — подозрительно: живой сенсор всегда шумит (СКО ≈ 3)


class ChannelProcessor:
    def __init__(self, pipeline_config: list[dict[str, Any]] | None = None) -> None:
        self.cal = Calibration()
        self.pipeline_config = pipeline_config or PRESETS[DEFAULT_PRESET]["config"]
        self.reset()

    def reset(self) -> None:
        self.pipeline = Pipeline.from_config(self.pipeline_config)
        self.auto = AutoCalibrator()
        self.hysteresis = Hysteresis(BEND_ON, BEND_OFF)
        self.raw: float | None = None
        self.filtered: float | None = None
        self.bend = 0.0
        self.bit = 0
        self.last_update = 0.0
        self.last_change = 0.0      # когда сырое значение последний раз отличалось от предыдущего
        self.count = 0

    def set_pipeline(self, config: list[dict[str, Any]]) -> None:
        """Сменить конвейер: состояние фильтров начинается заново, калибровка и автокалибровка сохраняются
        (у всех фильтров коэффициент передачи на постоянном сигнале = 1)."""
        self.pipeline = Pipeline.from_config(config)
        self.pipeline_config = config

    # ---------- автокалибровка: прежние имена атрибутов ----------
    @property
    def auto_rest(self) -> float | None:
        return self.auto.rest

    @auto_rest.setter
    def auto_rest(self, value: float | None) -> None:
        self.auto.rest = value

    @property
    def auto_max_dev(self) -> float:
        return self.auto.max_dev

    @auto_max_dev.setter
    def auto_max_dev(self, value: float) -> None:
        self.auto.max_dev = value

    @property
    def _rest_samples(self) -> list[float]:
        return self.auto.rest_samples

    @_rest_samples.setter
    def _rest_samples(self, value: list[float]) -> None:
        self.auto.rest_samples = value

    # ---------- вход ----------
    def update(self, value: float, now: float) -> None:
        if self.raw is None or value != self.raw:
            self.last_change = now
        self.raw = value
        self.count += 1
        self.last_update = now
        self.filtered = self.pipeline.process(value, now)
        self.auto.update(self.filtered)
        self.bend = normalize.bend(self.filtered, self.cal, self.auto)
        self.bit = self.hysteresis.update(self.bend)

    # ---------- состояние ----------
    @property
    def mode(self) -> str:
        return normalize.mode(self.cal, self.auto)

    def stale(self, now: float) -> bool:
        return self.count == 0 or now - self.last_update > STALE_AFTER_S

    def flat(self, now: float) -> bool:
        """Данные приходят, но значение не меняется FLAT_AFTER_S — сенсор отключён/замкнут
        или прошивка шлёт только изменения."""
        return self.count > 0 and not self.stale(now) and now - self.last_change > FLAT_AFTER_S
