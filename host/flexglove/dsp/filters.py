"""Фильтры-блоки с общим интерфейсом и конвейер из JSON-описания (решение D-18).

Каждый фильтр обрабатывает ОДИН канал по отсчёту:

    f = Median(n=5)
    y = f.process(x, t)     # x — значение, t — время в секундах (монотонное)
    f.reset()               # забыть состояние (например, при переподключении)

Конвейер — цепочка фильтров:

    Pipeline.from_config([{"type": "median", "n": 5}, {"type": "one_euro", "min_cutoff": 1.0, "beta": 0.005}])

Новый фильтр добавляется классом с декоратором @register("имя") — см. docs/dsp.md.

Про время (решение D-22). Перчатка измеряет равномерно, 50 раз в секунду, но время t — это время ПРИЁМА на ПК:
BLE приносит пакеты пачками (в записи Захара интервалы от 0 до 143 мс при среднем 20 мс). Фильтрам, которым
нужен шаг dt (One-Euro, EMA с постоянной времени), подаётся не «сырой» интервал, а оценка периода отсчётов —
скользящее среднее интервалов (SampleClock). Пачка из трёх пакетов за 1 мс не превращается в dt ≈ 0, а если
прошивка действительно стала слать реже, оценка за ~1 с подстроится.
"""
from __future__ import annotations

import math
import statistics
from collections import deque
from typing import Any, Callable

NOMINAL_RATE_HZ = 50.0

FILTERS: dict[str, type["Filter"]] = {}


def register(name: str) -> Callable[[type["Filter"]], type["Filter"]]:
    """Декоратор: делает фильтр доступным в JSON-описании конвейера под именем name."""
    def deco(cls: type[Filter]) -> type[Filter]:
        cls.type_name = name
        FILTERS[name] = cls
        return cls
    return deco


class SampleClock:
    """Оценка периода отсчётов по времени приёма: экспоненциальное среднее интервалов (≈ 1 с памяти),
    ограниченное 5…200 мс. Устойчиво к пачкам BLE-пакетов."""

    def __init__(self, rate_hz: float = NOMINAL_RATE_HZ, alpha: float = 0.02):
        self.nominal = 1.0 / rate_hz
        self.alpha = alpha
        self.reset()

    def reset(self) -> None:
        self.dt = self.nominal
        self._last: float | None = None

    def tick(self, t: float) -> float:
        if self._last is not None:
            raw = min(0.2, max(0.0, t - self._last))
            self.dt = min(0.2, max(0.005, self.dt + self.alpha * (raw - self.dt)))
        self._last = t
        return self.dt


class Filter:
    """Базовый класс фильтра. Наследник задаёт __init__ с параметрами (все — именованные, с умолчаниями),
    reset() и process(x, t)."""

    type_name = "base"

    def reset(self) -> None:
        raise NotImplementedError

    def process(self, x: float, t: float) -> float:
        raise NotImplementedError

    @property
    def params(self) -> dict[str, Any]:
        return dict(self._params)

    def to_config(self) -> dict[str, Any]:
        return {"type": self.type_name, **self.params}

    def __repr__(self) -> str:
        args = ", ".join(f"{k}={v}" for k, v in self.params.items())
        return f"{self.__class__.__name__}({args})"


# ---------------------------------------------------------------- блоки
@register("median")
class Median(Filter):
    """Скользящая медиана по n последним отсчётам — убирает одиночные выбросы, не размывая ступеньку.
    Задержка ≈ (n − 1) / 2 отсчёта."""

    def __init__(self, n: int = 5):
        if int(n) < 1:
            raise ValueError("median: n ≥ 1")
        self._params = {"n": int(n)}
        self.reset()

    def reset(self) -> None:
        self._w: deque[float] = deque(maxlen=self._params["n"])

    def process(self, x: float, t: float) -> float:
        self._w.append(x)
        return statistics.median(self._w)


@register("hampel")
class Hampel(Filter):
    """Фильтр Хампеля: если отсчёт дальше k·σ от медианы окна (σ = 1,4826·MAD), он заменяется медианой,
    иначе проходит без изменений. В отличие от медианы не трогает нормальные отсчёты (нет размытия)."""

    def __init__(self, n: int = 7, k: float = 3.0, min_sigma: float = 1.0):
        if int(n) < 3:
            raise ValueError("hampel: n ≥ 3")
        self._params = {"n": int(n), "k": float(k), "min_sigma": float(min_sigma)}
        self.reset()

    def reset(self) -> None:
        self._w: deque[float] = deque(maxlen=self._params["n"])

    def process(self, x: float, t: float) -> float:
        self._w.append(x)
        if len(self._w) < 3:
            return x
        med = statistics.median(self._w)
        mad = statistics.median(abs(v - med) for v in self._w)
        sigma = max(1.4826 * mad, self._params["min_sigma"])
        return med if abs(x - med) > self._params["k"] * sigma else x


@register("ema")
class Ema(Filter):
    """Экспоненциальное сглаживание y += α·(x − y). Задаётся либо alpha (на отсчёт, как в этапе 1),
    либо tau_s — постоянная времени в секундах (тогда α = dt / (τ + dt) по оценке периода отсчётов).
    Без параметров — alpha = 0,35."""

    def __init__(self, alpha: float | None = None, tau_s: float | None = None):
        if alpha is not None and tau_s is not None:
            raise ValueError("ema: задайте alpha или tau_s, не оба")
        if alpha is None and tau_s is None:
            alpha = 0.35
        if alpha is not None and not 0 < float(alpha) <= 1:
            raise ValueError("ema: 0 < alpha ≤ 1")
        if tau_s is not None and float(tau_s) <= 0:
            raise ValueError("ema: tau_s > 0")
        self._params = {"alpha": float(alpha)} if alpha is not None else {"tau_s": float(tau_s)}
        self._clock = SampleClock()
        self.reset()

    def reset(self) -> None:
        self._y: float | None = None
        self._clock.reset()

    def process(self, x: float, t: float) -> float:
        dt = self._clock.tick(t)
        if self._y is None:
            self._y = x
            return x
        a = self._params.get("alpha")
        if a is None:
            a = dt / (self._params["tau_s"] + dt)
        self._y += a * (x - self._y)
        return self._y


def _lp_alpha(cutoff_hz: float, dt: float) -> float:
    tau = 1.0 / (2 * math.pi * cutoff_hz)
    return 1.0 / (1.0 + tau / dt)


@register("one_euro")
class OneEuro(Filter):
    """One-Euro фильтр (Casiez и др., 2012): сглаживание с частотой среза, растущей со скоростью сигнала:
        fc = min_cutoff + beta · |скорость|,
    скорость (ед./с) сама сглажена с частотой d_cutoff. В покое fc ≈ min_cutoff — сильное сглаживание,
    при быстром сгибе fc растёт — малая задержка. beta зависит от масштаба сигнала (единицы АЦП)."""

    def __init__(self, min_cutoff: float = 1.0, beta: float = 0.005, d_cutoff: float = 1.0):
        if float(min_cutoff) <= 0 or float(d_cutoff) <= 0 or float(beta) < 0:
            raise ValueError("one_euro: min_cutoff > 0, d_cutoff > 0, beta ≥ 0")
        self._params = {"min_cutoff": float(min_cutoff), "beta": float(beta), "d_cutoff": float(d_cutoff)}
        self._clock = SampleClock()
        self.reset()

    def reset(self) -> None:
        self._x: float | None = None
        self._dx = 0.0
        self._clock.reset()

    def process(self, x: float, t: float) -> float:
        dt = self._clock.tick(t)
        if self._x is None:
            self._x = x
            return x
        p = self._params
        dx = (x - self._x) / dt
        self._dx += _lp_alpha(p["d_cutoff"], dt) * (dx - self._dx)
        cutoff = p["min_cutoff"] + p["beta"] * abs(self._dx)
        self._x += _lp_alpha(cutoff, dt) * (x - self._x)
        return self._x


@register("biquad_lp")
class BiquadLowpass(Filter):
    """Фильтр нижних частот 2-го порядка (Баттерворт при q = 0,707), коэффициенты по RBJ Audio EQ Cookbook,
    прямая транспонированная форма II. Рассчитан на номинальную частоту отсчётов fs (50 Гц). Начальное
    состояние — установившееся для первого отсчёта (без всплеска при старте)."""

    def __init__(self, cutoff_hz: float = 5.0, q: float = 0.7071, fs: float = NOMINAL_RATE_HZ):
        if not 0 < float(cutoff_hz) < float(fs) / 2:
            raise ValueError("biquad_lp: 0 < cutoff_hz < fs/2")
        if float(q) <= 0:
            raise ValueError("biquad_lp: q > 0")
        self._params = {"cutoff_hz": float(cutoff_hz), "q": float(q), "fs": float(fs)}
        w0 = 2 * math.pi * float(cutoff_hz) / float(fs)
        alpha = math.sin(w0) / (2 * float(q))
        cw = math.cos(w0)
        a0 = 1 + alpha
        self.b0 = (1 - cw) / 2 / a0
        self.b1 = (1 - cw) / a0
        self.b2 = (1 - cw) / 2 / a0
        self.a1 = -2 * cw / a0
        self.a2 = (1 - alpha) / a0
        self.reset()

    def reset(self) -> None:
        self._z1 = self._z2 = 0.0
        self._started = False

    def process(self, x: float, t: float) -> float:
        if not self._started:          # установившийся режим для постоянного x (коэффициент передачи на DC = 1)
            self._started = True
            self._z1 = x - self.b0 * x
            self._z2 = self.b2 * x - self.a2 * x
        y = self.b0 * x + self._z1
        self._z1 = self.b1 * x - self.a1 * y + self._z2
        self._z2 = self.b2 * x - self.a2 * y
        return y


@register("deadband")
class Deadband(Filter):
    """Мёртвая зона (люфт): выход не меняется, пока вход в пределах ±width от него; при выходе за зону
    выход тянется за входом на расстоянии width. Убирает мелкое дрожание, сохраняя форму больших движений."""

    def __init__(self, width: float = 2.0):
        if float(width) < 0:
            raise ValueError("deadband: width ≥ 0")
        self._params = {"width": float(width)}
        self.reset()

    def reset(self) -> None:
        self._y: float | None = None

    def process(self, x: float, t: float) -> float:
        w = self._params["width"]
        if self._y is None:
            self._y = x
        elif x > self._y + w:
            self._y = x - w
        elif x < self._y - w:
            self._y = x + w
        return self._y


# ---------------------------------------------------------------- конвейер
class Pipeline(Filter):
    """Цепочка фильтров; сама тоже Filter (можно вкладывать)."""

    type_name = "pipeline"

    def __init__(self, filters: list[Filter] | None = None):
        self.filters = list(filters or [])
        self._params = {}

    @classmethod
    def from_config(cls, config: list[dict[str, Any]]) -> Pipeline:
        """[{"type": "median", "n": 5}, …] → Pipeline. Ошибки описания — ValueError с понятным текстом."""
        if not isinstance(config, list):
            raise ValueError("Конвейер — это список блоков, например [{\"type\": \"median\", \"n\": 5}]")
        if len(config) > 16:
            raise ValueError("Слишком длинный конвейер (больше 16 блоков)")
        filters = []
        for i, block in enumerate(config, start=1):
            if not isinstance(block, dict) or "type" not in block:
                raise ValueError(f"Блок {i}: нужен объект с полем \"type\"")
            kind = block["type"]
            if kind not in FILTERS:
                raise ValueError(f"Блок {i}: неизвестный фильтр «{kind}». Доступны: {', '.join(sorted(FILTERS))}")
            params = {k: v for k, v in block.items() if k != "type"}
            try:
                filters.append(FILTERS[kind](**params))
            except TypeError as exc:
                raise ValueError(f"Блок {i} ({kind}): неверные параметры {sorted(params)} — {exc}") from exc
            except ValueError as exc:
                raise ValueError(f"Блок {i}: {exc}") from exc
        return cls(filters)

    def to_config(self) -> list[dict[str, Any]]:  # type: ignore[override]
        return [f.to_config() for f in self.filters]

    def reset(self) -> None:
        for f in self.filters:
            f.reset()

    def process(self, x: float, t: float) -> float:
        for f in self.filters:
            x = f.process(x, t)
        return x

    def __repr__(self) -> str:
        return " → ".join(repr(f) for f in self.filters) or "Pipeline()"


def run(pipeline: Filter, ts: list[float], xs: list[float]) -> list[float]:
    """Прогнать запись через фильтр с чистого состояния (для офлайн-оценки и тестов)."""
    pipeline.reset()
    return [pipeline.process(x, t) for t, x in zip(ts, xs)]
