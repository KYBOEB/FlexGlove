"""Обработка сигналов FlexGlove (этап 3, решение D-18): фильтры-блоки и конвейеры, калибровка, сгиб 0..1,
признаки и детектор «покой / движение», распознаватели «отсчёт → события», ориентация по IMU, синтетика.
Как добавить свой блок — docs/dsp.md."""
from .filters import FILTERS, Filter, Pipeline, SampleClock, register, run
from .presets import DEFAULT_PRESET, PRESETS

__all__ = ["FILTERS", "Filter", "Pipeline", "SampleClock", "register", "run", "DEFAULT_PRESET", "PRESETS"]
