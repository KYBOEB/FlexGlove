"""Заготовка: распознавание статических поз методом k ближайших соседей (без реализации).

Идея на будущее (этапы 5–6, датасеты 2–3 человек): поза = вектор сгибов всех пальцев (и, возможно, углы кисти).
Обучение — несколько секунд записи каждой позы; распознавание — k ближайших эталонов по евклидову расстоянию,
голосование, ответ «не поза», если ближайший эталон дальше порога (решение D-03: ответ «не жест»). Событие —
после удержания позы, как у комбинаций.
"""
from __future__ import annotations

from .base import Event, Recognizer, Sample


class KnnPoseRecognizer(Recognizer):
    name = "knn_pose"

    def __init__(self, k: int = 3, reject_distance: float = 0.35, hold_s: float = 0.3):
        self.k, self.reject_distance, self.hold_s = k, reject_distance, hold_s

    def fit(self, samples: list[tuple[float, ...]], labels: list[str]) -> None:
        """Эталоны: векторы сгибов и названия поз."""
        raise NotImplementedError("kNN-позы запланированы на этапы 5–6")

    def update(self, sample: Sample) -> list[Event]:
        raise NotImplementedError("kNN-позы запланированы на этапы 5–6")
