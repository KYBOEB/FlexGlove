"""Запись сессий в CSV в том же формате, что и программа Захара (удобно открывать в Excel):

    Time (s);S1 (ADC);S2 (ADC);...
    0,024;216;213;...

Разделитель «;», дробная часть времени через запятую. Пишутся СЫРЫЕ значения — для датасетов.
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path


def _fmt(v: float | None) -> str:
    if v is None:
        return ""
    return str(int(v)) if float(v).is_integer() else f"{v:.2f}".replace(".", ",")


class Recorder:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.path: Path | None = None
        self._file = None
        self._t0 = 0.0
        self._n = 0
        self.rows = 0
        self._last_flush = 0.0

    @property
    def active(self) -> bool:
        return self._file is not None

    def start(self, n_channels: int) -> Path:
        self.stop()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / f"{datetime.now():%Y-%m-%d_%H-%M-%S}.csv"
        self._file = self.path.open("w", encoding="utf-8-sig", newline="")
        self._n = max(1, n_channels)
        header = ["Time (s)"] + [f"S{i + 1} (ADC)" for i in range(self._n)]
        self._file.write(";".join(header) + "\r\n")
        self._t0 = time.monotonic()
        self.rows = 0
        return self.path

    def write(self, values: list[float | None], now: float | None = None) -> None:
        if self._file is None:
            return
        now = time.monotonic() if now is None else now
        vals = list(values[: self._n]) + [None] * max(0, self._n - len(values))
        t = f"{now - self._t0:.3f}".replace(".", ",")
        self._file.write(";".join([t] + [_fmt(v) for v in vals]) + "\r\n")
        self.rows += 1
        if now - self._last_flush > 1.0:
            self._file.flush()
            self._last_flush = now

    def stop(self) -> Path | None:
        if self._file is not None:
            self._file.close()
            self._file = None
        return self.path

    def list(self) -> list[dict]:
        if not self.folder.exists():
            return []
        files = sorted(self.folder.glob("*.csv"), reverse=True)
        return [{"name": f.name, "size": f.stat().st_size} for f in files]
