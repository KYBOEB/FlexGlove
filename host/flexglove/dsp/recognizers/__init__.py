"""Распознаватели: общий интерфейс «отсчёт → события» (base.py) и реализации."""
from .base import Event, Recognizer, Sample
from .combo import CommandRecognizer, ComboRecognizer, Role, armed_bindings, default_roles, normalize_roles
from .dynamic import DynamicGestureRecognizer
from .knn_pose import KnnPoseRecognizer
from .threshold import BEND_OFF, BEND_ON, Hysteresis, ThresholdRecognizer

RECOGNIZERS = {r.name: r for r in (ThresholdRecognizer, ComboRecognizer, KnnPoseRecognizer, DynamicGestureRecognizer)}

__all__ = ["Event", "Recognizer", "Sample", "CommandRecognizer", "ComboRecognizer", "Role", "armed_bindings",
           "default_roles", "normalize_roles", "DynamicGestureRecognizer", "KnnPoseRecognizer", "BEND_OFF", "BEND_ON",
           "Hysteresis", "ThresholdRecognizer", "RECOGNIZERS"]
