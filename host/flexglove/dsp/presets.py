"""Пресеты конвейера фильтров (выбираются на вкладке «Сигналы», хранятся в базе).

Параметры One-Euro подобраны tools/evaluate.py на синтетической записи по протоколу (размах 450, шум СКО 3)
и проверены на записи Захара — см. docs/analysis/2026-11-01-dsp-presets.md.
"""
from __future__ import annotations

from typing import Any

from .filters import Pipeline

DEFAULT_PRESET = "basic"

PRESETS: dict[str, dict[str, Any]] = {
    "basic": {
        "title": "Базовый",
        "hint": "Медиана 5 → сглаживание α = 0,35 (как в версиях 0.2–0.3). Задержка ≈ 60 мс.",
        "config": [{"type": "median", "n": 5}, {"type": "ema", "alpha": 0.35}],
    },
    "one_euro": {
        "title": "One-Euro",
        "hint": "Медиана 5 → One-Euro: в покое сглаживает сильнее базового, при быстром сгибе почти не запаздывает.",
        "config": [{"type": "median", "n": 5}, {"type": "one_euro", "min_cutoff": 1.0, "beta": 0.005, "d_cutoff": 1.0}],
    },
    "smooth": {
        "title": "Сильное сглаживание",
        "hint": "Хампель → медиана 7 → фильтр 2-го порядка 3 Гц: минимум дрожания ценой задержки (≈ 0,15 с).",
        "config": [{"type": "hampel", "n": 7, "k": 3.0}, {"type": "median", "n": 7}, {"type": "biquad_lp", "cutoff_hz": 3.0}],
    },
}


def resolve(preset: str, config: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Имя пресета (или "custom" + своё описание) → проверенное описание конвейера. Ошибка — ValueError."""
    if preset == "custom":
        if config is None:
            raise ValueError("Для своего конвейера нужно описание JSON")
        Pipeline.from_config(config)          # проверка
        return config
    if preset not in PRESETS:
        raise ValueError(f"Неизвестный пресет «{preset}». Доступны: {', '.join(PRESETS)} и custom")
    return PRESETS[preset]["config"]


def catalog() -> list[dict[str, Any]]:
    return [{"id": k, **v} for k, v in PRESETS.items()]
