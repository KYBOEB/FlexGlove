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
        "hint": "Медиана 5 → сглаживание α = 0,35 (как в версиях 0.2–0.3). Задержка на фронте ≈ 60–90 мс.",
        "config": [{"type": "median", "n": 5}, {"type": "ema", "alpha": 0.35}],
    },
    "one_euro": {
        "title": "One-Euro",
        "hint": "Хампель → One-Euro: выбросы убирает Хампель (без задержки медианы), в покое дрожание меньше базового, "
                "при сгибе задержка 15–60 мс вместо ≈ 90 мс.",
        "config": [{"type": "hampel", "n": 7, "k": 3.0},
                   {"type": "one_euro", "min_cutoff": 0.7, "beta": 0.02, "d_cutoff": 1.0}],
    },
    "smooth": {
        "title": "Сильное сглаживание",
        "hint": "Хампель → One-Euro с низкой частотой среза: дрожание в покое почти вдвое меньше базового, "
                "задержка как у базового (у слабых сенсоров чуть больше).",
        "config": [{"type": "hampel", "n": 7, "k": 3.0},
                   {"type": "one_euro", "min_cutoff": 0.3, "beta": 0.005, "d_cutoff": 1.0}],
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
