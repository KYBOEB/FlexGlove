"""Перенос обработки в dsp/ не меняет поведение: новый ChannelProcessor (пресет «Базовый») даёт ровно те же
отфильтрованные значения, сгибы и биты, что эталонная реализация версии 0.3.0 (переписана здесь дословно)."""
import random
import statistics
from collections import deque
from pathlib import Path

import pytest

from flexglove.processing import ChannelProcessor
from flexglove.sources import parse_recording

DATA = Path(__file__).parent / "data" / "esp32_one_sensor_2026-10.csv"


class LegacyChannel:
    """processing.ChannelProcessor из v0.3.0 (без калибровки «ладонь/кулак» — она проверяется отдельно)."""

    def __init__(self, cal_open=None, cal_fist=None):
        self.w, self.filtered, self.rest_samples, self.auto_rest, self.max_dev = deque(maxlen=5), None, [], None, 0.0
        self.cal_open, self.cal_fist, self.bend, self.bit = cal_open, cal_fist, 0.0, 0

    def update(self, v):
        self.w.append(v)
        med = statistics.median(self.w)
        self.filtered = med if self.filtered is None else self.filtered + 0.35 * (med - self.filtered)
        if self.auto_rest is None:
            self.rest_samples.append(self.filtered)
            if len(self.rest_samples) >= 25:
                self.auto_rest = statistics.median(self.rest_samples)
        else:
            self.max_dev = max(self.max_dev, abs(self.filtered - self.auto_rest))
        f = self.filtered
        if self.cal_open is not None and abs(self.cal_fist - self.cal_open) >= 15:
            b = (f - self.cal_open) / (self.cal_fist - self.cal_open)
        elif self.auto_rest is not None and self.max_dev >= 30:
            b = abs(f - self.auto_rest) / self.max_dev
        else:
            b = 0.0
        self.bend = min(1.0, max(0.0, b))
        if self.bend > 0.6:
            self.bit = 1
        elif self.bend < 0.4:
            self.bit = 0


def signals():
    _, rows = parse_recording(DATA.read_text(encoding="utf-8-sig"))
    yield "zakhar", [r[1][0] for r in rows]
    rnd = random.Random(11)
    sig = []
    for i in range(1500):                                  # покой → сгибы разной глубины → покой, шум и выбросы
        level = [0, 1, 0, 0.5, 0, 0.7, 0][min(6, i // 200)]
        v = 213 + 600 * level + rnd.gauss(0, 3) + (rnd.choice([-25, 25]) if rnd.random() < 0.01 else 0)
        sig.append(float(round(v)))
    yield "synthetic", sig


@pytest.mark.parametrize("cal", [(None, None), (200.0, 800.0)])
@pytest.mark.parametrize("name, sig", list(signals()))
def test_new_processor_matches_v030_exactly(name, sig, cal):
    new, old = ChannelProcessor(), LegacyChannel(*cal)
    new.cal.open, new.cal.fist = cal
    for i, v in enumerate(sig):
        new.update(v, i * 0.02)
        old.update(v)
        assert new.filtered == pytest.approx(old.filtered, abs=1e-9)
        assert new.bend == pytest.approx(old.bend, abs=1e-12) and new.bit == old.bit
    assert new.auto_rest == old.auto_rest and new.auto_max_dev == pytest.approx(old.max_dev)
