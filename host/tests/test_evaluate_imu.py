"""tools/evaluate_imu.py: замеры ориентации на синтетике MPU6050 (короткий прогон)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
evaluate_imu = pytest.importorskip("evaluate_imu")


def test_short_run_orders_and_bounds(tmp_path):
    res = evaluate_imu.run(minutes=1.0, seed=2, out=None)
    none, cap, track = (abs(res["still"][k][2]) for k in ("none", "capture", "track"))
    assert none > 15 and cap < none and track < 0.5            # °/мин
    for method in ("madgwick", "complementary"):
        for worst, rms in res["angles"][method].values():
            assert worst < 2.0
        for est_lag, shown_lag, _ in res["latency"][method].values():
            assert abs(est_lag) < 20 and 20 < shown_lag < 60      # сглаживание 3D добавляет ≈ τ
    md = evaluate_imu.format_report(res, 1.0)
    assert "Дрейф рысканья" in md and "Madgwick" in md
