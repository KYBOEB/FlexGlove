"""Статические жесты: все интервалы задаются виртуальными часами, без sleep."""
from copy import deepcopy
import random

import pytest

from flexglove.gestures import DEFAULT_THRESHOLDS, GestureSystem, distance
from flexglove.storage import Storage


CONTEXT = {"profile": "demo", "channels": [
    {"id": "slot:0", "finger": "thumb", "calibration": {"open": 200, "fist": 800}},
    {"id": "slot:1", "finger": "index", "calibration": {"open": 200, "fist": 800}}],
    "normalization": "fixed-linear-v1"}
A, B, C, UNKNOWN = [0.8, 0.1], [0.1, 0.8], [0.8, 0.8], [0.1, 0.1]


class Rig:
    def __init__(self, storage=None, with_templates=True):
        self.storage = storage or Storage(":memory:")
        if with_templates:
            for name, ref in zip("ABC", (A, B, C)):
                self.storage.save_gesture({"id": name, "name": name, "reference": ref,
                    "context": deepcopy(CONTEXT), "audio": None})
        self.system = GestureSystem(self.storage)
        self.context = deepcopy(CONTEXT)
        self.t = 10.0
        self.events = []

    def run(self, values, seconds, jitter=0):
        rng = random.Random(17)
        for _ in range(round(seconds / .02)):
            self.t = round(self.t + .02, 8)
            sample = [v + rng.uniform(-jitter, jitter) for v in values] if values is not None else None
            ev = self.system.update(sample, self.context, self.t, self.t if sample is not None else None)
            if ev:
                self.events.append(ev["gesture_id"])
        return self


def test_persistence_training_median_spread_and_legacy_data(tmp_path):
    path = tmp_path / "test.db"
    st = Storage(path)
    st.set_binding("11", "arm")
    st.put("calibration", {"open": [200, 200], "fist": [800, 800]})
    rig = Rig(st, False)
    rig.run(A, .3)
    rig.system.begin("Мой жест", rig.t, rig.context)
    rig.run(A, 1.04, .015)
    result = rig.system.training_result
    assert result["status"] == "saved"
    st.close()
    loaded = Storage(path)
    template, = loaded.gestures()
    assert template["context"] == CONTEXT
    assert template["reference"] == pytest.approx(A, abs=.01)
    assert len(template["reference"]) == 2  # отсутствующие пальцы не записаны нулями
    assert all(0 < x <= .03 for x in template["spread"])
    assert all(x > 0 for x in template["mad"])
    assert template["thresholds"]["hold_s"] == .7
    assert template["samples"] >= 20
    assert loaded.bindings() == {"11": "arm"}
    assert loaded.get("calibration")["fist"] == [800, 800]
    loaded.close()


def test_short_hold_sufficient_hold_long_hold_no_repeats():
    rig = Rig().run(UNKNOWN, .7).run(A, .68)
    assert rig.events == []
    assert rig.system.candidate == "A"
    assert 0 < rig.system.progress < 1
    rig.run(A, .4)
    assert rig.events == ["A"]
    assert rig.system.last_event["gesture_id"] == "A"
    rig.run(A, 12)
    assert rig.events == ["A"]


def test_noise_exit_true_exit_and_same_gesture_repeat():
    rig = Rig().run(UNKNOWN, .7).run(A, 1.2)
    rig.run(UNKNOWN, .2).run(A, 2)
    assert rig.events == ["A"]
    # Даже длительный неоднозначный/околопороговый уход не подтверждает выход.
    rig.run([.99, .1], 1).run(A, 2)
    assert rig.events == ["A"]
    rig.run(UNKNOWN, .7).run(A, 1.2)
    assert rig.events == ["A", "A"]


def test_a_b_c_without_neutral():
    rig = Rig().run(UNKNOWN, .7).run(A, 1.2).run(B, 1.2).run(C, 1.2)
    assert rig.events == ["A", "B", "C"]


def test_connect_or_restore_while_held_requires_exit():
    rig = Rig().run(A, 3)
    assert not rig.events
    rig.run(B, 1.4)
    assert rig.events == ["B"]
    rig.run(None, .3).run(B, 3)
    assert rig.events == ["B"]
    rig.run(UNKNOWN, .7).run(B, 1.2)
    assert rig.events == ["B", "B"]


@pytest.mark.parametrize("change", ["missing", "mapping", "calibration", "profile"])
def test_incompatible_templates_require_retraining(change):
    rig = Rig().run(UNKNOWN, .7)
    if change == "missing":
        rig.context["channels"].pop()
        values = A[:1]
    elif change == "mapping":
        rig.context["channels"][0]["finger"] = "middle"
        values = A
    elif change == "calibration":
        rig.context["channels"][0]["calibration"]["fist"] = 900
        values = A
    else:
        rig.context["profile"] = "glove"
        values = A
    rig.run(UNKNOWN[:len(values)], .7).run(values, 3)
    assert not rig.events
    assert not any(rig.system.compatible(t) for t in rig.system.templates)
    assert len(rig.storage.gestures()) == 3  # несовместимость не удаляет данные


def test_unknown_and_ambiguous_pose_do_not_fire():
    rig = Rig().run(UNKNOWN, 2).run([.45, .45], 2)
    assert not rig.events
    assert rig.system.candidate is None
    rig.storage.save_gesture({"id": "similar", "name": "similar", "reference": [.84, .1], "context": CONTEXT})
    rig.system.reload()
    rig.run(UNKNOWN, .7).run(A, 2)
    assert not rig.events
    assert rig.system.candidate is None
    assert rig.system.distances[1][0] - rig.system.distances[0][0] < DEFAULT_THRESHOLDS.margin


@pytest.mark.parametrize("problem", ["motion", "stale", "gap", "few", "calibration"])
def test_training_rejects_bad_data(problem):
    rig = Rig(with_templates=False).run(A, .2)
    rig.system.begin("new", rig.t, rig.context)
    rig.run(A, .2)
    if problem == "motion":
        rig.run(B, .5)
    elif problem == "stale":
        rig.t += .5
        rig.system.update(A, rig.context, rig.t, rig.t - .5)
    elif problem == "gap":
        rig.t += .14
        rig.run(A, 1)
    elif problem == "few":
        # 10 Гц: паузы допустимы, но отсчётов меньше минимальных 20.
        for _ in range(10):
            rig.t += .1
            rig.system.update(A, rig.context, rig.t, rig.t)
    else:
        rig.context["channels"][0]["calibration"]["open"] = 100
        rig.run(A, 1)
    assert rig.system.training is None
    assert rig.system.training_result["status"] == "error"
    assert not rig.storage.gestures()


def test_similar_reference_warning_and_retrain_keeps_audio():
    rig = Rig().run(A, .2)
    rig.system.begin("Similar", rig.t, rig.context)
    rig.run(A, 1.04)
    assert "A" in rig.system.training_result["similar"]
    t = rig.storage.gestures()[0]
    rig.storage.save_gesture({**t, "audio": "local.mp3"})
    rig.system.reload()
    rig.system.begin("A renamed", rig.t, rig.context, "A")
    rig.run(C, 1.04)
    updated = next(t for t in rig.storage.gestures() if t["id"] == "A")
    assert updated["audio"] == "local.mp3" and updated["name"] == "A renamed"
    assert updated["reference"] == C


def test_tick_does_not_count_repeated_samples_or_hold_across_gaps():
    rig = Rig().run(UNKNOWN, .7).run(A, .5)
    sample_time = rig.t
    for _ in range(30):
        rig.t += .02
        rig.system.update(A, rig.context, rig.t, sample_time)
    assert rig.system.candidate is None
    rig.run(A, 3)
    assert not rig.events  # после разрыва удержанная поза блокирована


def test_retraining_does_not_overwrite_another_profile():
    rig = Rig().run(A, .2)
    other = deepcopy(CONTEXT)
    other["profile"] = "glove"
    with pytest.raises(ValueError, match="другого профиля"):
        rig.system.begin("glove", rig.t, other, "A")
    assert rig.storage.gestures()[0]["context"]["profile"] == "demo"


def test_threshold_dataset():
    """Воспроизводимая синтетическая выборка: 3 центра, шум до ±0.04, 100 повторов.

    Данные демонстрируют выбранные границы, а не точность настоящей перчатки.
    """
    rng = random.Random(20261121)
    references = [A, B, C]
    correct = 0
    for label, center in enumerate(references):
        for _ in range(100):
            values = [v + rng.uniform(-.04, .04) for v in center]
            ranked = sorted((distance(values, ref), i) for i, ref in enumerate(references))
            assert ranked[0][0] <= .04
            assert ranked[1][0] - ranked[0][0] > .4
            if ranked[0][0] <= .12 and ranked[1][0] - ranked[0][0] >= .06:
                correct += ranked[0][1] == label
    assert correct == 300
    assert min(distance(UNKNOWN, ref) for ref in references) > .49
    assert distance(A, [.84, .1]) < .06  # пара не должна уверенно разделяться
