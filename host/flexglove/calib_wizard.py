"""Мастер калибровки (этап 4б, P1): «ладонь раскрыта — 3 с» → «кулак — 3 с» → итог по каждому каналу.

Мастер ничего не сохраняет сам: после двух замеров он показывает по каждому каналу уровни «ладонь» и «кулак»,
размах, шум и оценку, а сохраняет только по кнопке «Сохранить» (Engine.wizard_save). «Повторить» начинает заново.

Что считается:
- уровень позы — медиана отфильтрованных значений за время замера (медиана не боится редких выбросов);
- шум — робастный разброс сырых значений: 1,4826 · медиана |x − медиана| (для нормального шума это СКО,
  выбросы до ±30 ед. его почти не меняют); берётся больший из двух замеров;
- размах = кулак − ладонь (может быть отрицательным: у части сенсоров при сгибе АЦП падает).

Оценка канала:
- «не меняется — проверьте сенсор» (flat): |размах| < MIN_CAL_SPAN (15 ед. ≈ 5 СКО шума) — калибровка
  такого канала недействительна, сгиб будет считаться по автокалибровке;
- «слабый сигнал» (weak): |размах| < max(WEAK_SPAN, WEAK_SNR · шум) — работает, но сгиб будет шумным;
- «хорошо» (good) — иначе.
"""
from __future__ import annotations

import statistics

from .dsp.calibration import MIN_CAL_SPAN

CAPTURE_S = 3.0          # длительность замера каждой позы
WEAK_SPAN = 60.0         # ед. АЦП: меньше — «слабый сигнал» (у прототипа размах сотни единиц)
WEAK_SNR = 10.0          # размах меньше 10 шумов — тоже «слабый сигнал»
MIN_SAMPLES = 10         # меньше отсчётов за замер — канал «нет данных»

GRADE_TEXT = {
    "good": "хорошо",
    "weak": "слабый сигнал",
    "flat": "не меняется — проверьте сенсор",
    "no_data": "нет данных — проверьте связь",
}


def robust_std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    med = statistics.median(xs)
    return 1.4826 * statistics.median(abs(x - med) for x in xs)


def grade(span: float | None, noise: float) -> str:
    if span is None:
        return "no_data"
    if abs(span) < MIN_CAL_SPAN:
        return "flat"
    if abs(span) < max(WEAK_SPAN, WEAK_SNR * noise):
        return "weak"
    return "good"


class _Capture:
    def __init__(self, target: str, n: int, started: float, duration: float):
        self.target, self.started, self.duration = target, started, duration
        self.raw: list[list[float]] = [[] for _ in range(n)]
        self.filtered: list[list[float]] = [[] for _ in range(n)]

    def add(self, i: int, raw: float | None, filtered: float | None) -> None:
        if i < len(self.raw):
            if raw is not None:
                self.raw[i].append(raw)
            if filtered is not None:
                self.filtered[i].append(filtered)

    def progress(self, now: float) -> float:
        return min(1.0, max(0.0, (now - self.started) / self.duration))

    def result(self) -> list[dict]:
        out = []
        for raw, flt in zip(self.raw, self.filtered):
            ok = len(flt) >= MIN_SAMPLES
            out.append({"level": statistics.median(flt) if ok else None, "noise": robust_std(raw), "n": len(flt)})
        return out


class CalibrationWizard:
    """Состояния: idle → (замер open) → open_done → (замер fist) → review → сохранение или повтор."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.step = "idle"                  # idle | open_done | review
        self.capture: _Capture | None = None
        self.results: dict[str, list[dict]] = {}

    # ---------- замер ----------
    def start(self, target: str, n_channels: int, now: float, duration: float = CAPTURE_S) -> None:
        if target not in ("open", "fist"):
            raise ValueError("Поза должна быть open (ладонь) или fist (кулак)")
        if target == "fist" and "open" not in self.results:
            raise RuntimeError("Сначала шаг 1 — ладонь раскрыта")
        if n_channels < 1:
            raise RuntimeError("Калибровка возможна только при потоке значений АЦП")
        if target == "open":
            self.results = {}
        self.capture = _Capture(target, n_channels, now, duration)

    @property
    def capturing(self) -> str | None:
        return self.capture.target if self.capture else None

    def add(self, i: int, raw: float | None, filtered: float | None) -> None:
        if self.capture is not None:
            self.capture.add(i, raw, filtered)

    def update(self, now: float) -> bool:
        """Закончить замер, если вышло время. True — состояние изменилось."""
        cap = self.capture
        if cap is None or now - cap.started < cap.duration:
            return False
        self.capture = None
        self.results[cap.target] = cap.result()
        self.step = "review" if cap.target == "fist" else "open_done"
        return True

    # ---------- итог ----------
    def summary(self) -> list[dict]:
        if self.step != "review":
            return []
        rows = []
        for o, f in zip(self.results["open"], self.results["fist"]):
            span = f["level"] - o["level"] if o["level"] is not None and f["level"] is not None else None
            noise = max(o["noise"], f["noise"])
            g = grade(span, noise)
            rows.append({
                "open": None if o["level"] is None else round(o["level"], 1),
                "fist": None if f["level"] is None else round(f["level"], 1),
                "span": None if span is None else round(span, 1),
                "noise": round(noise, 1),
                "grade": g, "grade_text": GRADE_TEXT[g],
            })
        return rows

    def levels(self) -> tuple[list[float | None], list[float | None]]:
        rows = self.summary()
        if not rows:
            raise RuntimeError("Калибровка ещё не завершена: нужны оба замера")
        return [r["open"] for r in rows], [r["fist"] for r in rows]

    def snapshot(self, now: float) -> dict:
        return {
            "step": self.step,
            "capturing": self.capturing,
            "progress": round(self.capture.progress(now), 2) if self.capture else None,
            "duration": self.capture.duration if self.capture else CAPTURE_S,
            "summary": self.summary(),
        }
