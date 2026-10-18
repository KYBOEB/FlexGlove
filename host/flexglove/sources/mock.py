"""Демо-режим: имитирует перчатку без железа.

Комбинацию можно задавать кнопками/клавишами в интерфейсе или включить автоперебор.
Сообщения отправляются потоком 20 раз в секунду — как это будет делать настоящее устройство.
"""
from __future__ import annotations

import asyncio
import itertools

from .base import Source

AUTO_SEQUENCE = ["00", "01", "00", "10", "00", "11"]


class MockSource(Source):
    kind = "mock"

    def __init__(self, on_raw, on_status, n_channels: int = 2, rate_hz: float = 20.0):
        super().__init__(on_raw, on_status)
        self.bits = "0" * n_channels
        self.rate_hz = rate_hz
        self.auto = False
        self._task: asyncio.Task | None = None

    def set_bits(self, bits: str) -> None:
        self.bits = bits
        self.auto = False

    def set_auto(self, enabled: bool) -> None:
        self.auto = enabled

    async def start(self) -> None:
        self.on_status(kind=self.kind, name="Демо-перчатка", connected=True, message="Демо-режим: данные имитируются")
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        period = 1.0 / self.rate_hz
        auto_iter = itertools.cycle(AUTO_SEQUENCE)
        ticks_per_step = int(self.rate_hz * 1.5)
        tick = 0
        while True:
            if self.auto:
                if tick % ticks_per_step == 0:
                    self.bits = next(auto_iter)
                tick += 1
            self.on_raw(self.bits + "\n")
            await asyncio.sleep(period)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self.on_status(kind=None, name=None, connected=False, message="Не подключено")
