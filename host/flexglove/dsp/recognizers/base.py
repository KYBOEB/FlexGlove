"""Общий интерфейс распознавателей: «отсчёт → события» (решение D-18).

Распознаватель получает Sample — состояние руки в момент t (сгибы, биты, сжатие, IMU, контекст приложения) —
и возвращает список событий Event (пустой, если ничего не произошло):

    rec = ThresholdRecognizer(n_channels=2)
    for sample in samples:
        for ev in rec.update(sample):
            print(ev.kind, ev.t, ev.data)

Распознаватель хранит своё состояние (удержание, история) и сбрасывается reset().
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Sample:
    t: float                                     # время, с
    bends: tuple[float, ...] = ()                # сгиб каждого канала 0..1
    bits: tuple[int, ...] = ()                   # состояние 0/1 каждого канала (после гистерезиса)
    grip: float = 0.0                            # степень сжатия по пальцам сжатия 0..1
    imu: tuple[float, ...] | None = None         # ax, ay, az (мг), gx, gy, gz (0,1 °/с) — протокол v0.3
    context: dict[str, Any] = field(default_factory=dict)   # armed, bindings, debounce_s и т. п.


@dataclass(frozen=True)
class Event:
    kind: str                                    # например "finger", "command", "command_ignored"
    t: float
    data: dict[str, Any] = field(default_factory=dict)


class Recognizer:
    """Базовый класс. Наследник реализует update(); reset() — по необходимости."""

    name = "base"

    def reset(self) -> None:
        pass

    def update(self, sample: Sample) -> list[Event]:  # pragma: no cover - интерфейс
        raise NotImplementedError
