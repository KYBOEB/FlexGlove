"""Замеры ориентации по IMU на синтетике с моделью MPU6050 (этап 4).

    python tools/evaluate_imu.py --out docs/analysis/img            # отчёт Markdown + графики PNG
    python tools/evaluate_imu.py --minutes 10 --seed 3

Модель датчика (типичные значения MPU6050 из документации и измерений любителей):
- гироскоп: шум 0,03 °/с (СКО при DLPF ≈ 44 Гц), постоянное смещение (0,5; −0,4; 0,3) °/с, медленный
  температурный уход смещения 0,1 °/с за 5 мин по Z и случайное блуждание смещения 0,002 °/с/√с;
- акселерометр: шум 3 мг; квантование протокола v0.3 — 1 мг и 0,1 °/с; 50 Гц.
Каждый отсчёт проходит тот же путь, что в приложении: ImuTracker (оси руки → смещение → Madgwick).

Что измеряется
1. Дрейф рысканья при неподвижности: без поправки смещения / только замер 2 с при подключении / замер + уточнение
   в покое (как в приложении).
2. Дрейф рысканья «в работе»: 10 с движения / 5 с покоя по кругу — смещение уточняется только в паузах.
3. Ошибка углов на известных поворотах (крен, тангаж, рысканье, три оси), Madgwick и комплементарный фильтр.
4. Задержка: синусоида крена 0,5 / 1 / 2 Гц — запаздывание оценки (взаимная корреляция) и сглаживание 3D (τ 50 мс).
"""
from __future__ import annotations

import argparse
import math
import random
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "host"))

from flexglove.dsp import synthetic as syn  # noqa: E402
from flexglove.dsp.orientation import q_conj, q_from_axis_angle, q_mul, q_normalize, q_rotate, q_to_euler  # noqa: E402
from flexglove.imu import ImuTracker, twist_deg  # noqa: E402

RATE = 50.0
DT = 1 / RATE
BIAS = (0.5, -0.4, 0.3)
GYRO_NOISE, ACC_NOISE = 0.03, 0.003
BIAS_RW = 0.002          # °/с/√с
TEMP_DRIFT = 0.1 / 300   # °/с за секунду (по Z)
SMOOTH_TAU = 0.05        # сглаживание 3D-руки в интерфейсе


class Mpu6050:
    """Синтетический MPU6050: истинная угловая скорость и поза → строка v0.3 (мг, 0,1 °/с)."""

    def __init__(self, seed: int = 1, bias=BIAS, rw: float = BIAS_RW, temp: float = TEMP_DRIFT):
        self.rnd = random.Random(seed)
        self.bias = list(bias)
        self.rw, self.temp = rw, temp

    def sample(self, q, omega_dps):
        for k in range(3):
            self.bias[k] += self.rnd.gauss(0, self.rw * math.sqrt(DT))
        self.bias[2] += self.temp * DT
        up = q_rotate(q_conj(q), (0.0, 0.0, 1.0))
        acc = [round(1000 * (u + self.rnd.gauss(0, ACC_NOISE))) for u in up]
        gyr = [round(10 * (w + b + self.rnd.gauss(0, GYRO_NOISE))) for w, b in zip(omega_dps, self.bias)]
        return tuple(acc + gyr)


def integrate(q, omega_dps):
    rate = math.sqrt(sum(w * w for w in omega_dps))
    if rate < 1e-12:
        return q
    return q_normalize(q_mul(q, q_from_axis_angle(omega_dps, math.radians(rate) * DT)))


def yaw_of(q):
    return q_to_euler(q)[2]


def unwrap_deg(a):
    return np.degrees(np.unwrap(np.radians(np.asarray(a))))


# ---------------------------------------------------------------- 1–2. дрейф рысканья
def drift_still(minutes: float, mode: str, seed: int):
    """mode: none — без поправки; capture — только замер 2 с; track — замер + уточнение в покое (приложение)."""
    tr = ImuTracker()
    dev = Mpu6050(seed)
    q = (1.0, 0.0, 0.0, 0.0)
    n = int(minutes * 60 * RATE)
    ts, yaws = [], []
    tr.start_capture(0.0, zero_after=True)
    for i in range(n):
        t = i * DT
        raw = dev.sample(q, (0.0, 0.0, 0.0))
        if mode == "none":
            tr.bias, tr.bias_ok, tr.capture = (0.0, 0.0, 0.0), True, None
            tr.rest.reset()
        tr.update(raw, t)
        if mode == "capture" and tr.bias_ok and tr.capture is None:
            tr.rest.reset()                               # запрет уточнения: детектор покоя никогда не «наполнится»
        if i == int(2.1 * RATE) and not tr.zeroed:
            tr.set_zero()
        if i % 25 == 0:
            ts.append(t)
            yaws.append(yaw_of(tr.q_rel))
    y = unwrap_deg(yaws)
    return np.array(ts), y


def drift_working(minutes: float, seed: int):
    """10 с движения (покачивания вокруг всех осей) / 5 с покоя; ошибка рысканья относительно правды."""
    tr = ImuTracker()
    dev = Mpu6050(seed)
    rnd = random.Random(seed + 7)
    q = (1.0, 0.0, 0.0, 0.0)
    n = int(minutes * 60 * RATE)
    ts, err = [], []
    tr.start_capture(0.0, zero_after=True)
    phase = [rnd.uniform(0, 6.28) for _ in range(3)]
    for i in range(n):
        t = i * DT
        moving = t > 3 and (t % 15) < 10
        omega = tuple(25 * math.sin(2 * math.pi * f * t + p) for f, p in zip((0.3, 0.45, 0.2), phase)) if moving else (0.0, 0.0, 0.0)
        q = integrate(q, omega)
        tr.update(dev.sample(q, omega), t)
        if i % 25 == 0 and t > 3:
            # ошибка позы, приведённая к рысканью: угол между оценкой и правдой вокруг вертикали
            est = q_mul(tr.q0, tr.q_rel)        # абсолютная оценка
            d = q_mul(est, q_conj(q))           # разница в осях Земли
            ts.append(t)
            err.append(math.degrees(2 * math.atan2(d[3], d[0])))
    return np.array(ts), unwrap_deg(err) - unwrap_deg(err)[0]


# ---------------------------------------------------------------- 3. ошибка углов
SCENARIOS = {
    "крен 60°": [syn.Rotation((1, 0, 0), 60, 1.0, 1.5)],
    "тангаж −45°": [syn.Rotation((0, 1, 0), -45, 1.0, 1.0)],
    "рысканье 90°": [syn.Rotation((0, 0, 1), 90, 1.0, 2.0)],
    "три оси подряд": [syn.Rotation((1, 0, 0), 30, 1.0, 1.0), syn.Rotation((0, 1, 0), -25, 2.5, 1.0),
                       syn.Rotation((0, 0, 1), 45, 4.0, 1.0)],
}


def angle_errors(method: str, seed: int):
    out = {}
    for name, rots in SCENARIOS.items():
        prof = syn.omega_profile(rots)
        tr = ImuTracker(method=method)
        dev = Mpu6050(seed, rw=0.0, temp=0.0)
        q = (1.0, 0.0, 0.0, 0.0)
        tr.bias, tr.bias_ok = BIAS, True                  # смещение уже откалибровано (как после замера 2 с)
        errs = []
        for i in range(int(7 * RATE)):
            t = i * DT
            w = prof(t)
            raw = dev.sample(q, w)
            tr.update(raw, t)
            if t > 0.5:
                for a, b in zip(q_to_euler(tr.q), q_to_euler(q)):
                    errs.append(abs((a - b + 180) % 360 - 180))
            q = integrate(q, w)
        out[name] = (max(errs), float(np.sqrt(np.mean(np.square(errs)))))
    return out


# ---------------------------------------------------------------- 4. задержка
def lag_ms(true, est):
    a, b = np.asarray(true) - np.mean(true), np.asarray(est) - np.mean(est)
    corr = np.correlate(b, a, mode="full")
    i = int(np.argmax(corr))
    frac = 0.0
    if 0 < i < len(corr) - 1:                       # уточнение пика параболой — точнее одного отсчёта
        y0, y1, y2 = corr[i - 1], corr[i], corr[i + 1]
        den = y0 - 2 * y1 + y2
        frac = 0.5 * (y0 - y2) / den if den else 0.0
    return (i - (len(a) - 1) + frac) * DT * 1000


def latency(method: str, freq: float, seed: int):
    tr = ImuTracker(method=method)
    tr.bias, tr.bias_ok = BIAS, True
    dev = Mpu6050(seed, rw=0.0, temp=0.0)
    q = (1.0, 0.0, 0.0, 0.0)
    amp = 30.0
    true_roll, est_roll, shown = [], [], []
    disp = 0.0
    for i in range(int(12 * RATE)):
        t = i * DT
        w = (amp * 2 * math.pi * freq * math.cos(2 * math.pi * freq * t), 0.0, 0.0)
        tr.update(dev.sample(q, w), t)
        q = integrate(q, w)
        if t > 2:
            tr_roll = twist_deg(q)
            er = q_to_euler(tr.q)[0]
            disp += (er - disp) * (1 - math.exp(-DT / SMOOTH_TAU))   # сглаживание 3D (60 к/с ≈ то же)
            true_roll.append(tr_roll)
            est_roll.append(er)
            shown.append(disp)
    return lag_ms(true_roll, est_roll), lag_ms(true_roll, shown), (true_roll, est_roll, shown)


# ---------------------------------------------------------------- отчёт
def _f(v, d=1):
    return f"{v:.{d}f}".replace(".", ",")


def run(minutes: float = 5.0, seed: int = 3, out: Path | None = None) -> dict:
    res = {"still": {}, "working": None, "angles": {}, "latency": {}}
    for mode in ("none", "capture", "track"):
        t, y = drift_still(minutes, mode, seed)
        rate = float(np.polyfit(t[t > 3], y[t > 3], 1)[0] * 60)
        res["still"][mode] = (t, y, rate, float(abs(y[-1])))
    t, e = drift_working(minutes, seed)
    res["working"] = (t, e, float(np.polyfit(t, e, 1)[0] * 60), float(np.max(np.abs(e))))
    for m in ("madgwick", "complementary"):
        res["angles"][m] = angle_errors(m, seed)
        res["latency"][m] = {f: latency(m, f, seed) for f in (0.5, 1.0, 2.0)}
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        res["plots"] = plot(res, out)
    return res


def format_report(res: dict, minutes: float) -> str:
    s = res["still"]
    names = {"none": "без поправки смещения", "capture": "замер 2 с при подключении", "track": "замер + уточнение в покое (приложение)"}
    lines = ["## Дрейф рысканья при неподвижности", "",
             f"Модель MPU6050, {_f(minutes, 0)} мин, смещение (0,5; −0,4; 0,3) °/с + температурный уход 0,1 °/с за 5 мин + блуждание.", "",
             "| Режим | Скорость дрейфа, °/мин | Уход за всё время, ° |", "|---|---|---|"]
    for k in ("none", "capture", "track"):
        lines.append(f"| {names[k]} | {_f(abs(s[k][2]), 2)} | {_f(s[k][3], 1)} |")
    w = res["working"]
    lines += ["", f"**В работе** (10 с движения / 5 с покоя): дрейф рысканья {_f(abs(w[2]), 2)} °/мин, "
              f"наибольшая ошибка за {_f(minutes, 0)} мин — {_f(w[3], 1)}°.", "",
              "## Ошибка углов на известных поворотах (смещение откалибровано)", "",
              "| Поворот | Madgwick: макс / СКО, ° | Комплементарный: макс / СКО, ° |", "|---|---|---|"]
    for name in SCENARIOS:
        a, b = res["angles"]["madgwick"][name], res["angles"]["complementary"][name]
        lines.append(f"| {name} | {_f(a[0], 2)} / {_f(a[1], 2)} | {_f(b[0], 2)} / {_f(b[1], 2)} |")
    lines += ["", "## Задержка (синусоида крена ±30°)", "",
              "| Частота | Madgwick: оценка / на экране, мс | Комплементарный: оценка / на экране, мс |", "|---|---|---|"]
    for f in (0.5, 1.0, 2.0):
        a, b = res["latency"]["madgwick"][f], res["latency"]["complementary"][f]
        lines.append(f"| {_f(f, 1)} Гц | {_f(a[0], 0)} / {_f(a[1], 0)} | {_f(b[0], 0)} / {_f(b[1], 0)} |")
    lines += ["", "«На экране» — с учётом сглаживания 3D-руки (τ = 50 мс). Задержка BLE и отрисовки сюда не входит."]
    return "\n".join(lines) + "\n"


def plot(res: dict, out: Path) -> list[Path]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib не установлен — графики не построены", file=sys.stderr)
        return []
    paths = []
    fig, ax = plt.subplots(1, 2, figsize=(12, 3.8), dpi=110)
    labels = {"none": "без поправки", "capture": "замер 2 с", "track": "замер + уточнение в покое"}
    colors = {"none": "#d14343", "capture": "#a36c00", "track": "#2f6fe4"}
    for k in ("none", "capture", "track"):
        t, y, *_ = res["still"][k]
        ax[0].plot(t / 60, y, color=colors[k], label=labels[k])
    ax[0].set_title("Рысканье при неподвижности", fontsize=10, loc="left")
    ax[0].set_xlabel("время, мин"); ax[0].set_ylabel("°"); ax[0].legend(fontsize=8, frameon=False); ax[0].grid(alpha=0.25)
    for k in ("capture", "track"):
        t, y, *_ = res["still"][k]
        ax[1].plot(t / 60, y, color=colors[k], label=labels[k])
    t, e, *_ = res["working"]
    ax[1].plot(t / 60, e, color="#263650", label="в работе (движение/покой)")
    ax[1].set_title("Крупно: с поправкой смещения", fontsize=10, loc="left")
    ax[1].set_xlabel("время, мин"); ax[1].legend(fontsize=8, frameon=False); ax[1].grid(alpha=0.25)
    fig.tight_layout()
    p = out / "2026-11-01-imu-yaw-drift.png"
    fig.savefig(p); plt.close(fig); paths.append(p)

    fig, ax = plt.subplots(figsize=(10, 3.2), dpi=110)
    tr, est, shown = res["latency"]["madgwick"][1.0][2]
    tt = np.arange(len(tr)) * DT
    sel = tt < 3
    ax.plot(tt[sel], np.asarray(tr)[sel], color="#c6cdd9", lw=3, label="истинный крен")
    ax.plot(tt[sel], np.asarray(est)[sel], color="#2f6fe4", label="оценка Madgwick")
    ax.plot(tt[sel], np.asarray(shown)[sel], color="#263650", ls="--", label="на экране (сглаживание 50 мс)")
    ax.set_title("Задержка: синусоида крена 1 Гц", fontsize=10, loc="left")
    ax.set_xlabel("время, с"); ax.set_ylabel("°"); ax.legend(fontsize=8, frameon=False); ax.grid(alpha=0.25)
    fig.tight_layout()
    p = out / "2026-11-01-imu-latency.png"
    fig.savefig(p); plt.close(fig); paths.append(p)
    return paths


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Замеры ориентации по IMU на синтетике MPU6050")
    ap.add_argument("--minutes", type=float, default=5.0)
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--out", type=Path, default=None, help="папка для графиков PNG и отчёта")
    args = ap.parse_args(argv)
    res = run(args.minutes, args.seed, args.out)
    md = format_report(res, args.minutes)
    print(md)
    if args.out:
        (args.out / "imu-evaluate.md").write_text(md, encoding="utf-8")
        for p in res.get("plots", []):
            print(f"график: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
