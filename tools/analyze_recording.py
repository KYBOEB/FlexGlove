"""Разбор записи со сгибами (этап 2): уровни покоя и сгиба, размах, шум, время нарастания, дрейф, графики PNG.

Протокол записи (кнопка «Запись по протоколу» на вкладке «Сигналы» подсказывает шаги):
    5 с покой → 5 × (сгиб 2 с / разгиб 2 с) → 5 с покой, отдельно для каждого сенсора (или всех вместе).

Запуск (из корня репозитория):
    python tools/analyze_recording.py data/recordings/2026-11-05_15-20-00.csv
    python tools/analyze_recording.py запись.csv --out docs/analysis/img --name prototype-2ch-s1

Понимает записи приложения и программы Захара (время; значения; колонки IMU пропускаются).
Графики нужны matplotlib (`pip install matplotlib`); без него печатается только отчёт.

Что считается по каждому каналу (единицы — отсчёты АЦП):
    покой в начале / в конце   — медиана первых и последних 4 с (минус 0,5 с с краёв);
    дрейф                      — покой в конце − покой в начале (и в % от размаха);
    шум                        — СКО в начальном покое после удаления линейного тренда; выбросы — отсчёты дальше 5 СКО;
    сгибы                      — участки дальше середины между покоем и сгибом дольше 0,5 с;
    уровень сгиба, размах      — медиана середины каждого сгиба и её отличие от покоя (среднее ± СКО по сгибам);
    время нарастания / спада   — от 10 % до 90 % размаха по сигналу после медианы 5;
    ползучесть                 — наклон сигнала на удержании сгиба, ед./с (у AgNW-сенсоров бывает релаксация);
    отношение сигнал/шум       — размах / СКО шума;
    перекрёстное влияние       — для канала без сгибов: максимальное отклонение от покоя в сигмах шума.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "host"))

from flexglove.processing import BEND_OFF, BEND_ON, EMA_ALPHA, MIN_AUTO_SPAN, MIN_CAL_SPAN  # noqa: E402
from flexglove.sources.replay import imu_columns, parse_recording  # noqa: E402

REST_WINDOW_S = 4.0
EDGE_S = 0.5
MIN_BEND_S = 0.5
SPIKE_SIGMA = 5.0
MIN_BEND_SIGMA = 5.0        # сгибом считаем отклонение больше 5 СКО шума…
MIN_BEND_UNITS = 8.0        # …и больше 8 единиц АЦП


@dataclass
class Bend:
    start: float
    end: float
    level: float
    amplitude: float
    rise_s: float | None
    fall_s: float | None
    creep_per_s: float


@dataclass
class ChannelReport:
    name: str
    rest_start: float
    rest_end: float
    drift: float
    noise_std: float
    spikes: int
    polarity: int                      # +1 — при сгибе значение растёт, −1 — падает, 0 — сгибов нет
    bends: list[Bend] = field(default_factory=list)
    crosstalk_sigma: float | None = None

    @property
    def amplitude(self) -> float:
        return float(np.mean([b.amplitude for b in self.bends])) if self.bends else 0.0

    @property
    def amplitude_std(self) -> float:
        return float(np.std([b.amplitude for b in self.bends])) if len(self.bends) > 1 else 0.0

    @property
    def snr(self) -> float:
        return abs(self.amplitude) / self.noise_std if self.noise_std > 0 else float("inf")

    def mean_of(self, attr: str) -> float | None:
        vals = [getattr(b, attr) for b in self.bends if getattr(b, attr) is not None]
        return float(np.mean(vals)) if vals else None


@dataclass
class Report:
    duration: float
    samples: int
    rate_hz: float
    interval_p95_ms: float
    channels: list[ChannelReport]


# ---------------------------------------------------------------- вычисления
def median5(x: np.ndarray) -> np.ndarray:
    pad = np.pad(x, 2, mode="edge")
    return np.median(np.lib.stride_tricks.sliding_window_view(pad, 5), axis=1)


def _window(t: np.ndarray, t0: float, t1: float) -> np.ndarray:
    return (t >= t0) & (t <= t1)


def _crossing(t: np.ndarray, y: np.ndarray, level: float, i0: int, i1: int, rising: bool) -> float | None:
    """Первое время, когда y пересекает level на участке [i0, i1) (с линейной интерполяцией)."""
    for i in range(max(i0, 1), min(i1, len(y))):
        a, b = y[i - 1], y[i]
        if (rising and a < level <= b) or (not rising and a > level >= b):
            frac = (level - a) / (b - a) if b != a else 0.0
            return float(t[i - 1] + frac * (t[i] - t[i - 1]))
    return None


def analyze_channel(name: str, t: np.ndarray, x: np.ndarray) -> ChannelReport:
    xf = median5(x)
    t0, t1 = t[0], t[-1]
    m_start = _window(t, t0 + EDGE_S, t0 + EDGE_S + REST_WINDOW_S)
    m_end = _window(t, t1 - EDGE_S - REST_WINDOW_S, t1 - EDGE_S)
    rest_start = float(np.median(x[m_start]))
    rest_end = float(np.median(x[m_end]))

    seg = x[m_start]
    trend = np.polyval(np.polyfit(t[m_start], seg, 1), t[m_start]) if seg.size > 2 else rest_start
    resid = seg - trend
    robust = 1.4826 * float(np.median(np.abs(resid - np.median(resid)))) if seg.size > 2 else 0.0
    clean = resid[np.abs(resid) <= SPIKE_SIGMA * max(robust, 1.0)] if seg.size > 2 else resid
    noise_std = float(np.std(clean)) if clean.size > 2 else 0.0   # шум без одиночных выбросов
    spikes = int(np.sum(np.abs(x - xf) > SPIKE_SIGMA * max(robust, 1.0)))

    rest = (rest_start + rest_end) / 2
    hi, lo = float(np.percentile(xf, 97)), float(np.percentile(xf, 3))
    polarity = 1 if hi - rest >= rest - lo else -1
    extreme = hi if polarity > 0 else lo
    noise_floor = max(noise_std, 1.0)
    rep = ChannelReport(name, rest_start, rest_end, rest_end - rest_start, noise_std, spikes, 0)

    if abs(extreme - rest) < max(MIN_BEND_SIGMA * noise_floor, MIN_BEND_UNITS):
        inner = _window(t, t0 + 2 * EDGE_S, t1 - EDGE_S)        # без переходного процесса в самом начале записи
        dev = np.abs(xf[inner] - rest) if inner.any() else np.abs(xf - rest)
        rep.crosstalk_sigma = float(dev.max() / noise_floor)
        return rep

    rep.polarity = polarity
    thr = rest + (extreme - rest) / 2
    above = (xf - thr) * polarity > 0
    edges = np.flatnonzero(np.diff(above.astype(int)))
    starts = [i + 1 for i in edges if not above[i]]
    ends = [i + 1 for i in edges if above[i]]
    if above[0]:
        starts = [0] + starts
    if above[-1]:
        ends = ends + [len(x)]
    for s, e in zip(starts, ends):
        if t[e - 1] - t[s] < MIN_BEND_S:
            continue
        span = t[e - 1] - t[s]
        mid = _window(t, t[s] + 0.2 * span, t[e - 1] - 0.2 * span)
        level = float(np.median(x[mid])) if mid.any() else float(xf[s:e].mean())
        amp = level - rest
        lv10, lv90 = rest + 0.1 * amp, rest + 0.9 * amp
        rising = amp > 0
        look = int(np.searchsorted(t, t[s] - 1.0))
        r10 = _crossing(t, xf, lv10, look, e, rising)
        r90 = _crossing(t, xf, lv90, look, e, rising)
        look_end = int(np.searchsorted(t, t[e - 1] + 1.5))
        f90 = _crossing(t, xf, lv90, max(e - 5, s), look_end, not rising)
        f10 = _crossing(t, xf, lv10, max(e - 5, s), look_end, not rising)
        creep = float(np.polyfit(t[mid], x[mid], 1)[0]) if mid.sum() > 3 else 0.0
        rep.bends.append(Bend(float(t[s]), float(t[e - 1]), level, amp,
                              r90 - r10 if r10 is not None and r90 is not None else None,
                              f10 - f90 if f10 is not None and f90 is not None else None, creep))
    return rep


def analyze(text: str) -> tuple[Report, np.ndarray, np.ndarray, list[str]]:
    header, rows = parse_recording(text)
    imu = set(imu_columns(header))
    t = np.array([r[0] for r in rows], dtype=float)
    vals = np.array([[v for i, v in enumerate(r[1]) if i not in imu] for r in rows], dtype=float)
    names = [h for i, h in enumerate(header[1:]) if i not in imu] if header else []
    names = [names[i] if i < len(names) else f"S{i + 1}" for i in range(vals.shape[1])]
    good = ~np.isnan(vals).any(axis=1)
    t, vals = t[good], vals[good]
    dt = np.diff(t)
    report = Report(
        duration=float(t[-1] - t[0]), samples=len(t),
        rate_hz=float((len(t) - 1) / (t[-1] - t[0])) if t[-1] > t[0] else 0.0,
        interval_p95_ms=float(np.percentile(dt, 95) * 1000) if dt.size else 0.0,
        channels=[analyze_channel(names[i], t, vals[:, i]) for i in range(vals.shape[1])],
    )
    return report, t, vals, names


# ---------------------------------------------------------------- вывод
def _f(v: float | None, digits: int = 1, unit: str = "") -> str:
    return "—" if v is None else f"{v:.{digits}f}".replace(".", ",") + unit


def recommendations(rep: Report) -> list[str]:
    out = []
    if rep.rate_hz < 45:
        out.append(f"Частота {rep.rate_hz:.0f} Гц ниже ожидаемых 50 Гц — проверить прошивку (непрерывная отправка) и BLE.")
    for ch in rep.channels:
        if not ch.bends:
            continue
        a, n = abs(ch.amplitude), max(ch.noise_std, 1e-9)
        if a < MIN_AUTO_SPAN:
            suggested = max(5 * n, 0.5 * a)
            fix = (f"можно снизить MIN_AUTO_SPAN до ≈ {suggested:.0f} (5 СКО шума и половина размаха)"
                   if suggested < MIN_AUTO_SPAN - 1 else
                   f"порог снижать некуда (шум близко) — только ручная калибровка (нужен размах ≥ {MIN_CAL_SPAN:.0f})")
            out.append(f"{ch.name}: размах {a:.0f} < порога автокалибровки {MIN_AUTO_SPAN:.0f} — автокалибровка не включится; "
                       f"{fix}; лучше поднять чувствительность (резистор делителя, Лиза).")
        elif a > 20 * MIN_AUTO_SPAN:
            out.append(f"{ch.name}: размах {a:.0f} намного больше порога автокалибровки {MIN_AUTO_SPAN:.0f} — порог можно оставить.")
        if ch.snr < 20:
            out.append(f"{ch.name}: сигнал/шум {ch.snr:.0f} — мало; состояние 0/1 будет дрожать у порогов {BEND_OFF}/{BEND_ON}. "
                       "Нужна ручная калибровка и/или сильнее сглаживание.")
        if a > 0 and abs(ch.drift) / a > 0.1:
            out.append(f"{ch.name}: дрейф покоя {ch.drift:+.0f} ({abs(ch.drift) / a:.0%} размаха) — за время работы «0 %» уплывёт; "
                       "нужна периодическая подстройка уровня покоя (этап 3).")
        if a > 0 and ch.bends and abs(ch.mean_of("creep_per_s") or 0) * 2 / a > 0.1:
            out.append(f"{ch.name}: на удержании сигнал ползёт на {ch.mean_of('creep_per_s'):+.1f} ед./с "
                       "(> 10 % размаха за 2 с) — сгиб в % будет «уплывать» при неподвижном пальце.")
        if a > 0 and ch.amplitude_std / a > 0.15:
            out.append(f"{ch.name}: повторяемость размаха ±{ch.amplitude_std / a:.0%} — калибровку «кулак» брать как среднее.")
        rise = ch.mean_of("rise_s")
        if rise is not None and rise > 0.4:
            out.append(f"{ch.name}: нарастание {rise:.2f} с — медленно (наш фильтр добавляет ≈ 0,06 с, α = {EMA_ALPHA}); "
                       "задержка в основном от сенсора или скорости сгиба.")
    if not any(ch.bends for ch in rep.channels):
        out.append("Сгибов не найдено ни в одном канале — запись без сгибов, сенсор не подключён или чувствительность "
                   "слишком мала (проверить делитель).")
    for ch in rep.channels:
        if not ch.bends and ch.crosstalk_sigma is not None and ch.crosstalk_sigma > MIN_BEND_SIGMA:
            out.append(f"{ch.name}: отклонения до {ch.crosstalk_sigma:.0f} σ без чётких сгибов — слабый сигнал или "
                       "наводка от соседнего сенсора; посмотреть график.")
    if not out:
        out.append("Пороги processing.py подходят: размах, шум и дрейф в пределах допущений этапа 1.")
    return out


def format_report(rep: Report, name: str = "") -> str:
    lines = [f"## Разбор записи {name}".rstrip(), "",
             f"Длительность {_f(rep.duration)} с, отсчётов {rep.samples}, средняя частота {_f(rep.rate_hz)} Гц, "
             f"95-й перцентиль интервала {_f(rep.interval_p95_ms, 0)} мс.", "",
             "| Канал | Покой нач. | Покой кон. | Дрейф | Шум СКО | Выбросы | Сгибов | Уровень сгиба | Размах (± СКО) | "
             "Сигнал/шум | Нарастание 10–90 % | Спад 90–10 % | Ползучесть |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for ch in rep.channels:
        if ch.bends:
            level = float(np.mean([b.level for b in ch.bends]))
            lines.append(
                f"| {ch.name} | {_f(ch.rest_start, 0)} | {_f(ch.rest_end, 0)} | {_f(ch.drift, 0)} | {_f(ch.noise_std)} | {ch.spikes} | "
                f"{len(ch.bends)} | {_f(level, 0)} | {_f(ch.amplitude, 0)} ± {_f(ch.amplitude_std, 0)} | {_f(ch.snr, 0)} | "
                f"{_f(ch.mean_of('rise_s'), 2, ' с')} | {_f(ch.mean_of('fall_s'), 2, ' с')} | {_f(ch.mean_of('creep_per_s'), 1, ' ед./с')} |")
        else:
            lines.append(
                f"| {ch.name} | {_f(ch.rest_start, 0)} | {_f(ch.rest_end, 0)} | {_f(ch.drift, 0)} | {_f(ch.noise_std)} | {ch.spikes} | "
                f"0 | — | — | — | — | — | перекрёстное влияние {_f(ch.crosstalk_sigma)} σ |")
    lines += ["", "**Выводы и рекомендации по порогам** (текущие: автокалибровка ≥ "
              f"{MIN_AUTO_SPAN:.0f}, ручная ≥ {MIN_CAL_SPAN:.0f}, гистерезис {BEND_OFF}/{BEND_ON}):", ""]
    lines += [f"- {r}" for r in recommendations(rep)]
    return "\n".join(lines) + "\n"


def plot(rep: Report, t: np.ndarray, vals: np.ndarray, out_dir: Path, stem: str) -> list[Path]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib не установлен — графики не построены (pip install matplotlib)", file=sys.stderr)
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    tt = t - t[0]
    for i, ch in enumerate(rep.channels):
        fig, ax = plt.subplots(figsize=(11, 4), dpi=110)
        ax.plot(tt, vals[:, i], color="#9aa9c4", lw=0.8, label="сырые")
        ax.plot(tt, median5(vals[:, i]), color="#263650", lw=1.4, label="медиана 5")
        ax.axhline(ch.rest_start, color="#1f9d55", ls="--", lw=1, label=f"покой {ch.rest_start:.0f}")
        if ch.bends:
            for b in ch.bends:
                ax.axvspan(b.start - t[0], b.end - t[0], color="#2f6fe4", alpha=0.08)
                ax.hlines(b.level, b.start - t[0], b.end - t[0], color="#d14343", lw=1.2)
            title = (f"{ch.name}: размах {ch.amplitude:.0f} ± {ch.amplitude_std:.0f}, шум СКО {ch.noise_std:.1f}, "
                     f"с/ш {ch.snr:.0f}, нарастание {_f(ch.mean_of('rise_s'), 2)} с, дрейф {ch.drift:+.0f}")
        else:
            title = f"{ch.name}: сгибов нет, шум СКО {ch.noise_std:.1f}, перекрёстное влияние {_f(ch.crosstalk_sigma)} σ"
        ax.set_title(title, fontsize=10, loc="left")
        ax.set_xlabel("время, с")
        ax.set_ylabel("АЦП")
        ax.grid(alpha=0.25)
        ax.legend(loc="upper right", fontsize=8, frameon=False)
        fig.tight_layout()
        path = out_dir / f"{stem}-ch{i + 1}.png"
        fig.savefig(path)
        plt.close(fig)
        paths.append(path)
    return paths


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Разбор записи со сгибами FlexGlove")
    ap.add_argument("csv", type=Path, help="файл записи CSV")
    ap.add_argument("--out", type=Path, default=None, help="папка для PNG и отчёта (по умолчанию — рядом с CSV)")
    ap.add_argument("--name", default=None, help="основа имён файлов (по умолчанию — имя CSV)")
    args = ap.parse_args(argv)
    text = args.csv.read_text(encoding="utf-8-sig")
    rep, t, vals, _ = analyze(text)
    stem = args.name or args.csv.stem
    out = args.out or args.csv.parent
    md = format_report(rep, args.csv.name)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{stem}-analysis.md").write_text(md, encoding="utf-8")
    print(md)
    for p in plot(rep, t, vals, out, stem):
        print(f"график: {p}")
    print(f"отчёт: {out / (stem + '-analysis.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
