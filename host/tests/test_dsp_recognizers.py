"""Распознаватели с общим интерфейсом «отсчёт → события» (dsp/recognizers)."""
import pytest

from flexglove.dsp.recognizers import (RECOGNIZERS, ComboRecognizer, DynamicGestureRecognizer, Hysteresis,
                                       KnnPoseRecognizer, Recognizer, Sample, ThresholdRecognizer, default_roles)


def test_hysteresis_no_chatter_between_thresholds():
    h = Hysteresis(0.6, 0.4)
    bits = [h.update(b) for b in (0.1, 0.55, 0.65, 0.5, 0.45, 0.39, 0.5)]
    assert bits == [0, 0, 1, 1, 1, 0, 0]
    with pytest.raises(ValueError):
        Hysteresis(0.4, 0.6)


def test_threshold_recognizer_emits_finger_events():
    r = ThresholdRecognizer()
    events = []
    for i, bends in enumerate([(0, 0), (0.7, 0), (0.7, 0.9), (0.5, 0.9), (0.2, 0.1)]):
        events += r.update(Sample(t=i * 0.02, bends=bends))
    assert [(e.data["channel"], e.data["state"]) for e in events] == [(0, 1), (1, 1), (0, 0), (1, 0)]
    assert r.bits == (0, 0)


def test_combo_recognizer_through_common_interface():
    r = ComboRecognizer()
    r.roles = default_roles(2)
    ctx = {"armed": False, "bindings": {"10": "light"}, "debounce_s": 0.2}
    events = []
    for i in range(20):
        events += r.update(Sample(t=i * 0.02, bits=(1, 0), context=ctx))
    assert [(e.kind, e.data["action"]) for e in events] == [("command", "light")]


def test_stubs_have_interface_but_no_implementation():
    for cls in (KnnPoseRecognizer, DynamicGestureRecognizer):
        rec = cls()
        assert isinstance(rec, Recognizer)
        with pytest.raises(NotImplementedError):
            rec.update(Sample(t=0.0))
    assert set(RECOGNIZERS) == {"threshold", "combo", "knn_pose", "dynamic"}
