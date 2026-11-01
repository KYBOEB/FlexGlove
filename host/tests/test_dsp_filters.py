"""Фильтры-блоки dsp/filters.py и конвейер из JSON."""
import math
import random
import statistics

import pytest

from flexglove.dsp import synthetic as syn
from flexglove.dsp.filters import (FILTERS, BiquadLowpass, Deadband, Ema, Filter, Hampel, Median, OneEuro, Pipeline,
                                   SampleClock, register, run)
from flexglove.dsp.presets import PRESETS, resolve

TS = [i * 0.02 for i in range(600)]


def noisy_rest(sigma=3.0, seed=1, n=600, level=213.0):
    rnd = random.Random(seed)
    return [level + rnd.gauss(0, sigma) for _ in range(n)]


def settle(f: Filter, value=500.0, n=300):
    return run(f, TS[:n], [value] * n)[-1]


# ---------------- общие свойства: коэффициент передачи на постоянном сигнале = 1 ----------------
@pytest.mark.parametrize("name", sorted(FILTERS))
def test_constant_input_passes_unchanged(name):
    f = FILTERS[name]()
    assert settle(f) == pytest.approx(500.0, abs=1e-6)


@pytest.mark.parametrize("name", sorted(FILTERS))
def test_reset_gives_identical_rerun(name):
    f = FILTERS[name]()
    sig = noisy_rest(seed=4)
    assert run(f, TS, sig) == run(f, TS, sig)            # run() сбрасывает состояние


@pytest.mark.parametrize("preset", sorted(PRESETS))
def test_presets_reduce_noise_and_build(preset):
    p = Pipeline.from_config(PRESETS[preset]["config"])
    out = run(p, TS, noisy_rest(seed=2))[50:]
    assert statistics.pstdev(out) < 2.0                  # вход СКО 3


# ---------------- отдельные блоки ----------------
def test_median_kills_single_spike():
    out = run(Median(n=5), TS[:20], [100.0] * 10 + [300.0] + [100.0] * 9)
    assert max(out) == 100.0


def test_hampel_replaces_outliers_but_keeps_normal_samples():
    sig = noisy_rest(seed=3)[:200]
    sig[100] += 40
    out = run(Hampel(n=7, k=3.0), TS[:200], sig)
    assert abs(out[100] - 213) < 10
    unchanged = sum(1 for a, b in zip(sig, out) if a == b)
    assert unchanged > 185                               # нормальные отсчёты почти все проходят как есть


def test_ema_alpha_and_tau_are_equivalent_at_50hz():
    a = Ema(alpha=0.35)
    tau = 0.02 * (1 - 0.35) / 0.35                       # α = dt/(τ+dt)
    b = Ema(tau_s=tau)
    sig = noisy_rest(seed=5)
    assert run(a, TS, sig)[-1] == pytest.approx(run(b, TS, sig)[-1], abs=1e-6)


def test_ema_rejects_both_parameters():
    assert Ema().params == {"alpha": 0.35}
    with pytest.raises(ValueError):
        Ema(alpha=0.3, tau_s=0.1)


def test_one_euro_smooths_rest_more_and_lags_motion_less_than_basic():
    ts = syn.times(4.0)
    sensor = syn.SensorModel(rest=213, span=600, noise=3, spike_prob=0, seed=8)
    sig, clean = sensor.generate(syn.step(2.0), ts)
    basic = run(Ema(alpha=0.35), ts, sig)
    euro = run(OneEuro(min_cutoff=1.0, beta=0.005), ts, sig)
    rest = slice(25, 95)
    assert statistics.pstdev(euro[rest]) < statistics.pstdev(basic[rest])

    def cross(y, level):
        return next(t for t, v in zip(ts, y) if t > 1.9 and v >= level)
    half = 213 + 300
    assert cross(euro, half) - cross(clean, half) < 0.05     # меньше 50 мс отставания на 50 % ступеньки


def test_biquad_matches_butterworth_attenuation():
    """−3 дБ на частоте среза; на 12 Гц (2 октавы выше) — не меньше 24 дБ (12 дБ/октаву у 2-го порядка,
    билинейное преобразование даёт даже больше)."""
    f = BiquadLowpass(cutoff_hz=3.0)

    def gain_db(freq):
        ts = [i / 50 for i in range(2000)]
        out = run(f, ts, [math.sin(2 * math.pi * freq * t) for t in ts])[1000:]
        return 20 * math.log10(max(abs(v) for v in out))
    assert gain_db(1.0) == pytest.approx(0.0, abs=0.3)
    assert gain_db(3.0) == pytest.approx(-3.0, abs=0.3)
    assert gain_db(12.0) < -24.0


def test_biquad_starts_without_transient():
    out = run(BiquadLowpass(cutoff_hz=3.0), TS[:5], [700.0] * 5)
    assert out == pytest.approx([700.0] * 5)


def test_deadband_holds_small_jitter_and_follows_big_moves():
    out = run(Deadband(width=2), TS[:6], [100, 101.5, 99, 100.5, 110, 109])
    assert out == [100, 100, 100, 100, 108, 108]


def test_sample_clock_ignores_ble_bursts():
    ts = syn.ble_jitter(syn.times(10.0))
    c = SampleClock()
    dts = [c.tick(t) for t in ts]
    assert min(dts[50:]) > 0.012 and max(dts[50:]) < 0.03     # реальные интервалы 0…120 мс


def test_one_euro_stable_with_bursty_timestamps():
    ts_true = syn.times(6.0)
    sig, _ = syn.SensorModel(rest=213, span=600, spike_prob=0, seed=9).generate(syn.step(3.0), ts_true)
    out = run(OneEuro(), syn.ble_jitter(ts_true), sig)
    assert all(150 < v < 900 for v in out)                     # без «взрывов» при dt ≈ 0


# ---------------- конвейер ----------------
def test_pipeline_from_config_roundtrip():
    cfg = [{"type": "median", "n": 5}, {"type": "one_euro", "min_cutoff": 1.0, "beta": 0.01}]
    p = Pipeline.from_config(cfg)
    assert [b["type"] for b in p.to_config()] == ["median", "one_euro"]
    assert p.to_config()[1]["beta"] == 0.01 and "d_cutoff" in p.to_config()[1]
    assert Pipeline.from_config(p.to_config()).to_config() == p.to_config()


@pytest.mark.parametrize("cfg, msg", [
    ({"type": "median"}, "список"),
    ([{"n": 5}], "type"),
    ([{"type": "kalman"}], "неизвестный фильтр"),
    ([{"type": "median", "size": 5}], "неверные параметры"),
    ([{"type": "biquad_lp", "cutoff_hz": 40}], "fs/2"),
])
def test_pipeline_config_errors_are_readable(cfg, msg):
    with pytest.raises(ValueError, match=msg):
        Pipeline.from_config(cfg)


def test_custom_filter_registration():
    @register("x2_test")
    class Double(Filter):
        def __init__(self):
            self._params = {}

        def reset(self):
            pass

        def process(self, x, t):
            return 2 * x

    try:
        assert Pipeline.from_config([{"type": "x2_test"}]).process(3.0, 0.0) == 6.0
    finally:
        FILTERS.pop("x2_test")


def test_resolve_presets():
    assert resolve("basic") == PRESETS["basic"]["config"]
    assert resolve("custom", [{"type": "median", "n": 3}]) == [{"type": "median", "n": 3}]
    with pytest.raises(ValueError):
        resolve("nope")
    with pytest.raises(ValueError):
        resolve("custom", [{"type": "nope"}])
