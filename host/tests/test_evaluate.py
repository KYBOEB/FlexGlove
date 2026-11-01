"""tools/evaluate.py: офлайн-оценка конвейеров на записи Захара и синтетике по протоколу."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
evaluate = pytest.importorskip("evaluate")
DATA = Path(__file__).parent / "data" / "esp32_one_sensor_2026-10.csv"


def by(results, pipeline, channel_prefix="S1"):
    return next(r for r in results if r.pipeline == pipeline and r.channel.startswith(channel_prefix))


def test_synthetic_protocol_metrics():
    pipes = [evaluate.load_pipeline(p) for p in ("basic", "one_euro", "smooth")]
    results, *_ = evaluate.evaluate(evaluate.synthetic_csv(), pipes)
    basic, euro, smooth = (by(results, n) for n in ("Базовый", "One-Euro", "Сильное сглаживание"))
    for m in (basic, euro, smooth):
        assert m.expected == 10 and m.false_toggles == 0
    assert euro.rise_ms < basic.rise_ms - 30                 # One-Euro заметно быстрее на фронте
    assert euro.noise_std < basic.noise_std                  # и при этом тише в покое
    assert smooth.noise_std < 0.7 * basic.noise_std          # «Сильное сглаживание» — заметно тише


def test_zakhar_rest_recording_no_false_toggles():
    pipes = [evaluate.load_pipeline(p) for p in ("basic", "one_euro", "smooth")]
    results, *_ = evaluate.evaluate(DATA.read_text(encoding="utf-8-sig"), pipes, span=60)
    for m in results:
        assert m.expected == 0 and m.toggles == 0 and m.rise_ms is None
        assert 0.3 < m.noise_std < 1.5


def test_pipeline_specs(tmp_path):
    assert evaluate.load_pipeline("basic")[0] == "Базовый"
    assert evaluate.load_pipeline('[{"type":"median","n":3}]') == ("свой", [{"type": "median", "n": 3}])
    f = tmp_path / "my.json"
    f.write_text(json.dumps([{"type": "ema", "alpha": 0.2}]), encoding="utf-8")
    assert evaluate.load_pipeline(str(f)) == ("my", [{"type": "ema", "alpha": 0.2}])


def test_cli_writes_report_and_rejects_bad_pipeline(tmp_path):
    rc = evaluate.main(["--synthetic", "--save", str(tmp_path / "syn.csv"), "--compare", "one_euro", "--out", str(tmp_path)])
    assert rc == 0 and (tmp_path / "syn.csv").exists()
    md = (tmp_path / "syn-evaluate.md").read_text(encoding="utf-8")
    assert "| S1 (ADC) | Базовый |" in md and "One-Euro" in md
    assert evaluate.main([str(DATA), "--pipeline", '[{"type":"nope"}]', "--out", str(tmp_path)]) == 2
    pytest.importorskip("matplotlib")
    assert (tmp_path / "syn-evaluate-ch1.png").stat().st_size > 10_000
