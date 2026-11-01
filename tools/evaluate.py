"""Офлайн-оценка конвейеров фильтров на записи CSV (этап 3, решение D-18).

    python tools/evaluate.py data/recordings/запись.csv --pipeline basic --compare one_euro smooth
    python tools/evaluate.py запись.csv --pipeline мой_конвейер.json
    python tools/evaluate.py запись.csv --pipeline '[{"type":"median","n":3},{"type":"one_euro","beta":0.01}]'
    python tools/evaluate.py --synthetic --save data/recordings/synthetic_protocol.csv --compare one_euro smooth

--pipeline / --compare: имя пресета (basic, one_euro, smooth), путь к .json или JSON прямо в строке.
Отчёт (Markdown) и графики PNG пишутся в папку --out (по умолчанию — рядом с записью).

Метрики по каждому каналу и конвейеру
- Шум в покое — СКО выхода на участках покоя (вне сгибов, без первой секунды разгона), после вычитания
  среднего каждого участка; «макс. отклонение» — самый большой выброс, просочившийся через фильтр.
- Задержка — на сколько мс выход позже сырого сигнала пересекает 50 % размаха; отдельно для сгиба и разгиба.
  Это собственная задержка фильтра (инерция самого сенсора есть и в сыром сигнале).
- Переключения 0/1 — смены бита после гистерезиса 0,4/0,6 при калибровке «покой → уровень сгиба» из записи;
  ожидается 2 на каждый сгиб, всё сверх — ложные. Если сгибов нет (запись покоя), калибровка условная:
  размах --span (по умолчанию 60 ед. — заведомо маленький сенсор), чтобы проверить запас по шуму.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "host"))
sys.path.insert(0, str(ROOT / "tools"))

from analyze_recording import analyze_channel, median5  # noqa: E402
from flexglove.dsp import synthetic as syn  # noqa: E402
from flexglove.dsp.calibration import Calibration  # noqa: E402
from flexglove.dsp.filters import Pipeline, run  # noqa: E402
from flexglove.dsp.normalize import bend as bend_of  # noqa: E402
from flexglove.dsp.presets import PRESETS  # noqa: E402
from flexglove.dsp.recognizers.threshold import Hysteresis  # noqa: E402
from flexglove.sources.replay import imu_columns, parse_recording  # noqa: E402

WARMUP_S = 1.0
GUARD_BEFORE_S, GUARD_AFTER_S = 0.6, 0.8      # вокруг сгиба — переходные участки, не покой


@dataclass
class Metrics:
    pipeline: str
    channel: str
    noise_std: float
    max_dev: float
    rise_ms: float | None
    fall_ms: float | None
    toggles: int
    expected: int
    calibration: str

    @property
    def false_toggles(self) -> int:
        return max(0, self.toggles - self.expected)


def load_pipeline(spec: str) -> tuple[str, list[dict]]:
    if spec in PRESETS:
        return PRESETS[spec]["title"], PRESETS[spec]["config"]
    if spec.lstrip().startswith("["):
        cfg = json.loads(spec)
        return "свой", cfg
    path = Path(spec)
    cfg = json.loads(path.read_text(encoding="utf-8"))
    return path.stem, cfg


def _cross(t: np.ndarray, y: np.ndarray, level: float, rising: bool, t_from: float, t_to: float, confirm: int = 2):
    idx = np.flatnonzero((t >= t_from) & (t <= t_to))
    for i in idx:
        seg = y[i:i + confirm + 1]
        if len(seg) == confirm + 1 and all((v >= level) if rising else (v <= level) for v in seg) and i > 0:
            a, b = y[i - 1], y[i]
            frac = (level - a) / (b - a) if b != a else 0.0
            return float(t[i - 1] + frac * (t[i] - t[i - 1]))
    return None


def rest_mask(t: np.ndarray, bends) -> np.ndarray:
    m = t >= t[0] + WARMUP_S
    for b in bends:
        m &= ~((t >= b.start - GUARD_BEFORE_S) & (t <= b.end + GUARD_AFTER_S))
    return m


def noise_metrics(y: np.ndarray, mask: np.ndarray) -> tuple[float, float]:
    """СКО и макс. отклонение по участкам покоя (у каждого участка вычитается своё среднее)."""
    resid = []
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return float("nan"), float("nan")
    runs = np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1)
    for r in runs:
        if r.size > 5:
            resid.append(y[r] - y[r].mean())
    if not resid:
        return float("nan"), float("nan")
    allr = np.concatenate(resid)
    return float(allr.std()), float(np.abs(allr).max())


def evaluate_channel(name: str, pipe_title: str, cfg: list[dict], t: np.ndarray, x: np.ndarray, span: float) -> tuple[Metrics, np.ndarray]:
    ref = analyze_channel(name, t, x)                       # сгибы и уровни — по сырому сигналу, одинаково для всех конвейеров
    y = np.array(run(Pipeline.from_config(cfg), list(t), list(x)))
    noise, max_dev = noise_metrics(y, rest_mask(t, ref.bends))

    rest = (ref.rest_start + ref.rest_end) / 2
    rises, falls = [], []
    for b in ref.bends:
        half = rest + b.amplitude / 2
        up = b.amplitude > 0
        tr = _cross(t, x, half, up, b.start - 0.5, b.end)
        if tr is not None:
            to = _cross(t, y, half, up, tr - 0.2, b.end + 0.5)
            if to is not None:
                rises.append(to - tr)
        tf = _cross(t, x, half, not up, b.end - 0.3, b.end + 1.0)
        if tf is not None:
            to = _cross(t, y, half, not up, tf - 0.2, tf + 1.0)
            if to is not None:
                falls.append(to - tf)

    if ref.bends:
        amp = float(np.mean([b.amplitude for b in ref.bends]))
        cal = Calibration(open=rest, fist=rest + amp)
        cal_text = f"по записи: покой {rest:.0f}, сгиб {rest + amp:.0f}"
    else:
        cal = Calibration(open=rest, fist=rest + span)
        cal_text = f"условная: покой {rest:.0f}, размах {span:.0f}"
    h = Hysteresis()
    bits = [h.update(bend_of(float(v), cal)) for v in y]
    toggles = int(sum(1 for a, b in zip(bits, bits[1:]) if a != b))
    m = Metrics(pipe_title, name, noise, max_dev,
                float(np.mean(rises) * 1000) if rises else None, float(np.mean(falls) * 1000) if falls else None,
                toggles, 2 * len(ref.bends), cal_text)
    return m, y


def load_recording(text: str) -> tuple[np.ndarray, np.ndarray, list[str]]:
    header, rows = parse_recording(text)
    imu = set(imu_columns(header))
    t = np.array([r[0] for r in rows], dtype=float)
    vals = np.array([[v for i, v in enumerate(r[1]) if i not in imu] for r in rows], dtype=float)
    names = [h for i, h in enumerate(header[1:]) if i not in imu] if header else []
    names = [names[i] if i < len(names) else f"S{i + 1}" for i in range(vals.shape[1])]
    good = ~np.isnan(vals).any(axis=1)
    return t[good], vals[good], names


def evaluate(text: str, pipelines: list[tuple[str, list[dict]]], span: float = 60.0):
    t, vals, names = load_recording(text)
    results, outputs = [], {}
    for title, cfg in pipelines:
        for i, name in enumerate(names):
            m, y = evaluate_channel(name, title, cfg, t, vals[:, i], span)
            results.append(m)
            outputs[(title, i)] = y
    return results, t, vals, names, outputs


def _f(v: float | None, d: int = 1) -> str:
    return "—" if v is None or v != v else f"{v:.{d}f}".replace(".", ",")


def format_report(results: list[Metrics], t: np.ndarray, title: str, pipelines: list[tuple[str, list[dict]]]) -> str:
    dt = np.diff(t)
    lines = [f"## Оценка конвейеров: {title}", "",
             f"Длительность {_f(float(t[-1] - t[0]))} с, отсчётов {len(t)}, средняя частота {_f((len(t) - 1) / (t[-1] - t[0]))} Гц, "
             f"интервалы приёма от {_f(float(dt.min() * 1000), 0)} до {_f(float(dt.max() * 1000), 0)} мс.", "",
             "Конвейеры:", ""]
    lines += [f"- **{name}**: `{json.dumps(cfg, ensure_ascii=False)}`" for name, cfg in pipelines]
    lines += ["", "| Канал | Конвейер | Шум в покое, СКО | Макс. отклонение в покое | Задержка сгиб, мс | Задержка разгиб, мс | "
              "Переключений 0/1 (ожидалось) | Ложных | Калибровка для подсчёта |",
              "|---|---|---|---|---|---|---|---|---|"]
    for m in sorted(results, key=lambda r: r.channel):
        lines.append(f"| {m.channel} | {m.pipeline} | {_f(m.noise_std, 2)} | {_f(m.max_dev, 1)} | {_f(m.rise_ms, 0)} | "
                     f"{_f(m.fall_ms, 0)} | {m.toggles} ({m.expected}) | {m.false_toggles} | {m.calibration} |")
    return "\n".join(lines) + "\n"


def plot(t, vals, names, outputs, pipelines, results, out_dir: Path, stem: str) -> list[Path]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib не установлен — графики не построены (pip install matplotlib)", file=sys.stderr)
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    colors = ["#263650", "#2f6fe4", "#d14343", "#1f9d55", "#a36c00"]
    paths = []
    tt = t - t[0]
    for i, name in enumerate(names):
        ref = analyze_channel(name, t, vals[:, i])
        fig, axes = plt.subplots(1, 2, figsize=(13, 4), dpi=110, gridspec_kw={"width_ratios": [2, 1]})
        for ax in axes:
            ax.plot(tt, vals[:, i], color="#c6cdd9", lw=0.8, label="сырые")
            for k, (title, _cfg) in enumerate(pipelines):
                ax.plot(tt, outputs[(title, i)], color=colors[k % len(colors)], lw=1.3, label=title)
            ax.grid(alpha=0.25)
            ax.set_xlabel("время, с")
        axes[0].set_ylabel("АЦП")
        axes[0].set_title(f"{name}: вся запись", fontsize=10, loc="left")
        if ref.bends:                                       # крупно — фронт первого сгиба
            b = ref.bends[0]
            axes[1].set_xlim(b.start - t[0] - 0.4, b.start - t[0] + 0.6)
            lo, hi = sorted((ref.rest_start, b.level))
            pad = (hi - lo) * 0.1
            axes[1].set_ylim(lo - pad, hi + pad)
            axes[1].set_title("крупно: фронт сгиба (задержка)", fontsize=10, loc="left")
        else:                                               # крупно — 3 с покоя (шум)
            mid = tt[len(tt) // 2]
            axes[1].set_xlim(mid - 1.5, mid + 1.5)
            seg = vals[(tt > mid - 1.5) & (tt < mid + 1.5), i]
            axes[1].set_ylim(seg.min() - 3, seg.max() + 3)
            axes[1].set_title("крупно: 3 с покоя (шум)", fontsize=10, loc="left")
        axes[0].legend(loc="upper right", fontsize=8, frameon=False)
        fig.tight_layout()
        path = out_dir / f"{stem}-ch{i + 1}.png"
        fig.savefig(path)
        plt.close(fig)
        paths.append(path)
    return paths


def synthetic_csv(seed: int = 3) -> str:
    ts, noisy, _clean = syn.ProtocolRecording(seed=seed).build()
    return syn.to_csv(ts, noisy)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Офлайн-оценка конвейеров фильтров FlexGlove")
    ap.add_argument("csv", nargs="?", type=Path, help="запись CSV")
    ap.add_argument("--pipeline", default="basic", help="пресет, путь к .json или JSON-строка")
    ap.add_argument("--compare", nargs="*", default=[], help="ещё конвейеры для сравнения")
    ap.add_argument("--span", type=float, default=60.0, help="условный размах для записи без сгибов, ед. АЦП")
    ap.add_argument("--out", type=Path, default=None, help="папка для отчёта и PNG")
    ap.add_argument("--name", default=None, help="основа имён файлов")
    ap.add_argument("--synthetic", action="store_true", help="оценить на синтетической записи по протоколу этапа 2")
    ap.add_argument("--save", type=Path, default=None, help="с --synthetic: сохранить синтетическую запись в CSV")
    args = ap.parse_args(argv)

    if args.synthetic:
        text = synthetic_csv()
        if args.save:
            args.save.parent.mkdir(parents=True, exist_ok=True)
            args.save.write_text(text, encoding="utf-8")
        src_name, default_dir = "синтетика по протоколу этапа 2", (args.save.parent if args.save else Path("."))
        stem = args.name or (args.save.stem if args.save else "synthetic")
    elif args.csv:
        text = args.csv.read_text(encoding="utf-8-sig")
        src_name, default_dir, stem = args.csv.name, args.csv.parent, args.name or args.csv.stem
    else:
        ap.error("укажите запись CSV или --synthetic")

    try:
        pipelines = [load_pipeline(s) for s in [args.pipeline, *args.compare]]
        for _title, cfg in pipelines:
            Pipeline.from_config(cfg)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"Ошибка в описании конвейера: {exc}", file=sys.stderr)
        return 2

    results, t, vals, names, outputs = evaluate(text, pipelines, args.span)
    out = args.out or default_dir
    out.mkdir(parents=True, exist_ok=True)
    md = format_report(results, t, src_name, pipelines)
    (out / f"{stem}-evaluate.md").write_text(md, encoding="utf-8")
    print(md)
    for p in plot(t, vals, names, outputs, pipelines, results, out, f"{stem}-evaluate"):
        print(f"график: {p}")
    print(f"отчёт: {out / (stem + '-evaluate.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
