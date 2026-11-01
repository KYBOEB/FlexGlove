"""Порог с гистерезисом: сгиб 0..1 → состояние пальца 0/1 (перенесено из processing.py).

«Согнут», когда сгиб > on (0,6); «разогнут», когда сгиб < off (0,4); между порогами состояние не меняется —
так шум у границы не вызывает дребезга.
"""
from __future__ import annotations

from .base import Event, Recognizer, Sample

BEND_ON = 0.6
BEND_OFF = 0.4


class Hysteresis:
    """Один канал: update(сгиб) → бит."""

    def __init__(self, on: float = BEND_ON, off: float = BEND_OFF):
        if not 0 <= off < on <= 1:
            raise ValueError("гистерезис: 0 ≤ off < on ≤ 1")
        self.on, self.off = on, off
        self.bit = 0

    def reset(self) -> None:
        self.bit = 0

    def update(self, value: float) -> int:
        if value > self.on:
            self.bit = 1
        elif value < self.off:
            self.bit = 0
        return self.bit


class ThresholdRecognizer(Recognizer):
    """Все каналы: по Sample.bends выдаёт события {"kind": "finger", channel, state} при смене состояния."""

    name = "threshold"

    def __init__(self, on: float = BEND_ON, off: float = BEND_OFF):
        self.on, self.off = on, off
        self.channels: list[Hysteresis] = []

    def reset(self) -> None:
        self.channels = []

    @property
    def bits(self) -> tuple[int, ...]:
        return tuple(h.bit for h in self.channels)

    def update(self, sample: Sample) -> list[Event]:
        while len(self.channels) < len(sample.bends):
            self.channels.append(Hysteresis(self.on, self.off))
        events = []
        for i, b in enumerate(sample.bends):
            before = self.channels[i].bit
            if self.channels[i].update(b) != before:
                events.append(Event("finger", sample.t, {"channel": i, "state": self.channels[i].bit}))
        return events
