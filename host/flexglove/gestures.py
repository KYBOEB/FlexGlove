"""Обучаемые статические позы: непрерывные сгибы, отказ от неизвестных, временной автомат.

Время передаёт ядро; модуль не зависит от часов ОС, HTTP, ориентации или аудио.
"""
from __future__ import annotations

import math
import statistics
from copy import deepcopy
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Thresholds:
    distance: float = 0.12       # RMS по нормализованным сгибам
    margin: float = 0.06         # отрыв от второго эталона в той же метрике
    release_distance: float = 0.22
    hold_s: float = 0.700
    release_s: float = 0.350
    stable_range: float = 0.10   # максимальный размах одного канала
    stable_window_s: float = 0.16
    train_s: float = 1.0
    min_samples: int = 20
    fresh_s: float = 0.20
    max_gap_s: float = 0.12


DEFAULT_THRESHOLDS = Thresholds()


def distance(a: list[float], b: list[float]) -> float:
    if not a or len(a) != len(b):
        return math.inf
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)) / len(a))


def spread(rows: list[list[float]]) -> list[float]:
    return [max(col) - min(col) for col in zip(*rows)] if rows else []


class GestureSystem:
    def __init__(self, storage, thresholds: Thresholds = DEFAULT_THRESHOLDS):
        self.storage = storage
        self.thresholds = thresholds
        self.templates = storage.gestures()
        self.context: dict | None = None
        self.training: dict | None = None
        self.training_result: dict | None = None
        self.last_event: dict | None = None
        self.sequence = 0
        self.reset()

    def reset(self, reason: str = "Сначала выйдите из текущей позы на 350 мс") -> None:
        self.candidate = None
        self.since = None
        self.progress = 0.0
        self.latched = None
        self.gated = True
        self.baseline_set = False
        self.exit_since = None
        self.history: list[tuple[float, list[float]]] = []
        self.last_sample = None
        self.reason = reason
        self.distances: list[tuple[float, str]] = []

    def reload(self) -> None:
        self.templates = self.storage.gestures()
        self.reset()

    def compatible(self, template: dict) -> bool:
        return self.context is not None and template["context"] == self.context

    def begin(self, name: str, now: float, context: dict, template_id: str | None = None) -> None:
        if self.training:
            raise ValueError("Уже идёт обучение")
        name = name.strip()
        if not name or len(name) > 80:
            raise ValueError("Название должно содержать от 1 до 80 символов")
        if template_id and not any(t["id"] == template_id for t in self.templates):
            raise ValueError("Жест не найден")
        old = next((t for t in self.templates if t["id"] == template_id), None)
        if old and old["context"]["profile"] != context["profile"]:
            raise ValueError("Нельзя переобучить жест другого профиля: создайте новый жест в текущем профиле")
        self.reset("Обучение: держите позу неподвижно около секунды")
        self.training_result = None
        self.training = {"name": name, "id": template_id, "started": now, "context": deepcopy(context),
                         "samples": [], "times": []}

    def cancel_training(self, reason: str) -> None:
        if self.training:
            self.training_result = {"status": "error", "message": reason}
            self.training = None

    def suspend(self, reason: str) -> None:
        self.cancel_training(reason)
        self.reset(reason)

    def update(self, values: list[float] | None, context: dict | None, now: float,
               sample_time: float | None, reason: str = "Нет свежих откалиброванных данных") -> dict | None:
        th = self.thresholds
        if context != self.context:
            self.suspend("Изменились профиль, каналы или калибровка")
            self.context = deepcopy(context)
        if (values is None or not values or any(not math.isfinite(x) for x in values)
                or sample_time is None or now - sample_time > th.fresh_s):
            self.suspend(reason)
            return None
        if self.last_sample is not None and sample_time <= self.last_sample:
            return None                      # такты и вкладки не создают дополнительные отсчёты
        if self.last_sample is not None and sample_time - self.last_sample > th.max_gap_s:
            self.suspend("Разрыв потока данных")
        self.last_sample = sample_time
        if self.training:
            tr = self.training
            if context != tr["context"]:
                self.suspend("Изменилась шкала нормализации")
                return None
            tr["samples"].append(values[:])
            tr["times"].append(sample_time)
            if max(spread(tr["samples"]), default=0) > th.stable_range:
                self.suspend("Обнаружено движение: повторите обучение неподвижной позы")
                return None
            if now - tr["started"] >= th.train_s:
                if (len(tr["samples"]) < th.min_samples
                        or tr["times"][-1] - tr["times"][0] < th.train_s - th.max_gap_s):
                    self.suspend("Недостаточно данных за секунду: нужно не менее 20 отсчётов")
                    return None
                cols = list(zip(*tr["samples"]))
                reference = [statistics.median(c) for c in cols]
                mad = [statistics.median(abs(x - m) for x in c) for c, m in zip(cols, reference)]
                similar = [t["name"] for t in self.templates if t["id"] != tr["id"]
                           and self.compatible(t) and distance(reference, t["reference"]) < 2 * th.distance + th.margin]
                old = next((t for t in self.templates if t["id"] == tr["id"]), {})
                saved = self.storage.save_gesture({"id": tr["id"], "name": tr["name"], "context": context,
                    "reference": reference, "spread": spread(tr["samples"]), "mad": mad,
                    "samples": len(tr["samples"]), "duration_s": tr["times"][-1] - tr["times"][0],
                    "thresholds": asdict(th), "audio": old.get("audio")})
                self.training = None
                self.training_result = {"status": "saved", "id": saved["id"],
                    "message": "Эталон сохранён", "similar": similar}
                self.reload()
            return None

        self.history.append((sample_time, values[:]))
        self.history = [(t, v) for t, v in self.history if sample_time - t <= th.stable_window_s + 1e-9]
        stable = (len(self.history) >= 3 and self.history[-1][0] - self.history[0][0] >= th.stable_window_s * 0.7
                  and max(spread([v for _, v in self.history]), default=1) <= th.stable_range)
        self.distances = sorted((distance(values, t["reference"]), t["id"])
                                for t in self.templates if self.compatible(t))
        best = self.distances[0] if self.distances else (math.inf, None)
        second = self.distances[1][0] if len(self.distances) > 1 else math.inf
        match = best[1] if best[0] <= th.distance and second - best[0] >= th.margin else None
        self.candidate = match
        self.progress = 0.0
        if not stable:
            self.since = None
            self.exit_since = None
            self.reason = "Дождитесь устойчивой позы"
            return None
        if not self.baseline_set:
            self.latched = match
            self.baseline_set = True
        if self.gated or self.latched is not None:
            previous = next((t for t in self.templates if t["id"] == self.latched), None)
            away = (match is not None and match != self.latched) or (
                match is None and (previous is None or distance(values, previous["reference"]) >= th.release_distance))
            if away:
                if self.exit_since is None:
                    self.exit_since = now
                if now - self.exit_since + 1e-9 >= th.release_s:
                    self.gated = False
                    self.latched = None
                    self.exit_since = None
            else:
                self.exit_since = None
            if self.gated or self.latched == match and match is not None:
                self.since = None
                self.reason = "Выйдите из текущего жеста на 350 мс"
                return None
        if match is None:
            self.since = None
            self.reason = "Неизвестный жест"
            return None
        if self.since is None or self.since[0] != match:
            self.since = (match, now)
        self.progress = min(1.0, (now - self.since[1]) / th.hold_s)
        self.reason = "Удерживайте жест 700 мс"
        if now - self.since[1] + 1e-9 >= th.hold_s:
            self.latched = match
            self.since = None
            self.sequence += 1
            self.last_event = {"sequence": self.sequence, "gesture_id": match, "at": now}
            self.reason = "Жест сработал; для повтора выйдите из него на 350 мс"
            return self.last_event.copy()
        return None

    def snapshot(self, now: float) -> dict:
        return {"candidate": self.candidate, "hold_progress": round(self.progress, 3),
                "triggered": self.last_event, "latched": self.latched, "needs_exit": self.gated,
                "reason": self.reason, "thresholds": asdict(self.thresholds),
                "nearest_distance": self.distances[0][0] if self.distances else None,
                "second_distance": self.distances[1][0] if len(self.distances) > 1 else None,
                "training": ({"name": self.training["name"],
                    "progress": min(1, max(0, now - self.training["started"]) / self.thresholds.train_s)}
                    if self.training else None), "training_result": self.training_result}
