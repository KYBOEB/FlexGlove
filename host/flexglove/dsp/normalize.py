"""Степень сгиба 0..1 из отфильтрованного значения (перенесено из processing.py).

    ручная калибровка:  сгиб = (x − ладонь) / (кулак − ладонь)
    автокалибровка:     сгиб = |x − покой| / максимальное отклонение
    иначе:              0 (калибровки нет — не выдумываем сгиб из шума)
Результат ограничивается отрезком [0, 1].
"""
from __future__ import annotations

from .calibration import AutoCalibrator, Calibration


def clamp01(x: float) -> float:
    return min(1.0, max(0.0, x))


def bend(value: float | None, cal: Calibration, auto: AutoCalibrator | None = None) -> float:
    if value is None:
        return 0.0
    if cal.valid:
        return clamp01((value - cal.open) / (cal.fist - cal.open))  # type: ignore[operator]
    if auto is not None and auto.active:
        return clamp01(abs(value - auto.rest) / auto.max_dev)  # type: ignore[operator]
    return 0.0


def mode(cal: Calibration, auto: AutoCalibrator | None = None) -> str:
    """calibrated | auto | uncalibrated — для интерфейса."""
    if cal.valid:
        return "calibrated"
    if auto is not None and auto.active:
        return "auto"
    return "uncalibrated"
