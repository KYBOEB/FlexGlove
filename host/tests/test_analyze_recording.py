"""tools/analyze_recording.py на синтетической записи по протоколу этапа 2."""
import math
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
analyze_recording = pytest.importorskip("analyze_recording")

TAU = 0.08


def protocol_csv(amp=450.0, noise=3.0, drift=6.0, imu=False, seed=7):
    """5 с покой → 5 × (сгиб 2 с / разгиб 2 с) → 5 с покой, 50 Гц. S1 сгибается, S2 — нет (лёгкая наводка)."""
    rnd = random.Random(seed)
    dur, dt = 30.0, 0.02
    head = "Time (s);S1 (ADC);S2 (ADC)" + (";ax (mg);ay (mg);az (mg);gx (0.1 dps);gy (0.1 dps);gz (0.1 dps)" if imu else "")
    lines, level, t = [head], 0.0, 0.0
    while t < dur:
        target = 1.0 if 5 <= t < 25 and ((t - 5) % 4) < 2 else 0.0
        level += (dt / (TAU + dt)) * (target - level)
        s1 = 1200 + drift * t / dur + amp * level + rnd.gauss(0, noise)
        if rnd.random() < 0.01:
            s1 += rnd.choice([-1, 1]) * 25
        s2 = 1500 + 2 * level + rnd.gauss(0, noise)
        row = [f"{t:.3f}".replace(".", ","), str(round(s1)), str(round(s2))]
        if imu:
            row += ["0", "0", "1000", "0", "0", "0"]
        lines.append(";".join(row))
        t += dt
    return "\n".join(lines) + "\n"


def test_detects_five_bends_and_measures_them():
    rep, *_ = analyze_recording.analyze(protocol_csv())
    s1, s2 = rep.channels
    assert len(s1.bends) == 5 and s1.polarity == 1
    assert abs(s1.amplitude - 450) < 15
    assert 2.0 < s1.noise_std < 4.0
    assert abs(s1.drift - 6) < 4
    rise = s1.mean_of("rise_s")
    assert abs(rise - 2.2 * TAU) < 0.08                 # 10→90 % для экспоненты = 2,2 τ
    assert abs(rep.rate_hz - 50) < 1
    assert not s2.bends and s2.crosstalk_sigma is not None and s2.crosstalk_sigma < 6


def test_negative_polarity_and_imu_columns_ignored():
    rep, _, vals, names = analyze_recording.analyze(protocol_csv(amp=-300, imu=True))
    assert names == ["S1 (ADC)", "S2 (ADC)"] and vals.shape[1] == 2
    assert rep.channels[0].polarity == -1 and len(rep.channels[0].bends) == 5


def test_report_and_png(tmp_path):
    csv = tmp_path / "rec.csv"
    csv.write_text(protocol_csv(amp=25), encoding="utf-8")      # размах меньше порога автокалибровки
    assert analyze_recording.main([str(csv), "--out", str(tmp_path / "out")]) == 0
    md = (tmp_path / "out" / "rec-analysis.md").read_text(encoding="utf-8")
    assert "| S1 (ADC) |" in md and "автокалибровк" in md
    pytest.importorskip("matplotlib")
    assert (tmp_path / "out" / "rec-ch1.png").stat().st_size > 10_000
