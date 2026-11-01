"""Воспроизведение записи CSV через тот же конвейер, что и живые данные.

Понимает записи этого приложения и программы Захара: первая колонка — время в секундах,
остальные — значения сенсоров; разделитель «;», «,» или табуляция; дробная часть — точка или запятая.
Колонки IMU (заголовки ax…gz, протокол v0.3) воспроизводятся строкой "v1;v2|ax;ay;az;gx;gy;gz".
"""
from __future__ import annotations

import asyncio
import csv
import io

from .base import Source


def imu_columns(header: list[str]) -> list[int]:
    """Индексы колонок IMU среди значений (без колонки времени): ax, ay, az, gx, gy, gz."""
    names = [h.strip().lower().split(" ")[0] for h in header[1:]]
    idx = [names.index(k) for k in ("ax", "ay", "az", "gx", "gy", "gz") if k in names]
    return idx if len(idx) == 6 else []


def parse_recording(text: str) -> tuple[list[str], list[tuple[float, list[float]]]]:
    text = text.lstrip("﻿")
    sample = text[:2048]
    delimiter = ";" if sample.count(";") >= sample.count("\t") else "\t"
    if ";" not in sample and "\t" not in sample:
        delimiter = ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    header: list[str] = []
    rows: list[tuple[float, list[float]]] = []
    for row in reader:
        if not row or not any(c.strip() for c in row):
            continue
        try:
            nums = [float(c.strip().replace(",", ".")) if c.strip() else float("nan") for c in row]
        except ValueError:
            if not rows:
                header = [c.strip() for c in row]
            continue
        if len(nums) >= 2:
            rows.append((nums[0], nums[1:]))
    if not rows:
        raise ValueError("В файле нет строк с числами (ожидается: время; значение1; значение2 …)")
    return header, rows


class ReplaySource(Source):
    kind = "replay"

    def __init__(self, on_raw, on_status, text: str, name: str = "запись", loop: bool = True, speed: float = 1.0):
        super().__init__(on_raw, on_status)
        self.name = name
        self.header, self.rows = parse_recording(text)
        self.imu_idx = imu_columns(self.header)
        self.loop = loop
        self.speed = speed
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        dur = self.rows[-1][0] - self.rows[0][0]
        n = len(self.rows[0][1]) - len(self.imu_idx)
        self.on_status(kind=self.kind, name=self.name, connected=True,
                       message=f"Воспроизведение: {len(self.rows)} строк, {n} канал(ов), {dur:.1f} с")
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            t0 = self.rows[0][0]
            loop = asyncio.get_running_loop()
            start = loop.time()
            for t, values in self.rows:
                delay = (t - t0) / self.speed - (loop.time() - start)
                if delay > 0:
                    await asyncio.sleep(delay)
                self.on_raw(self._line(values) + "\n")
            if not self.loop:
                break
            await asyncio.sleep(0.2)
        self.on_status(kind=self.kind, name=self.name, connected=False, message="Воспроизведение закончено")

    def _line(self, values: list[float]) -> str:
        fmt = lambda v: "" if v != v else (str(int(v)) if v.is_integer() else f"{v:.3f}")  # noqa: E731
        if not self.imu_idx:
            return ";".join(fmt(v) for v in values)
        flex = [v for i, v in enumerate(values) if i not in self.imu_idx]
        imu = [values[i] for i in self.imu_idx]
        line = ";".join(fmt(v) for v in flex)
        return line + ("|" + ";".join(fmt(v) for v in imu) if all(v == v for v in imu) else "")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self.on_status(kind=None, name=None, connected=False, message="Не подключено")
