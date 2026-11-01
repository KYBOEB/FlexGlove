"""Совместимость: распознавание команд-жестов (D-21) переехало в dsp/recognizers/combo.py (этап 3)."""
from .dsp.recognizers.combo import (  # noqa: F401
    ACTION_PRIORITY,
    ARMED_HOLD_S,
    GUARD_BEFORE_S,
    GUARD_GRIP_SPAN,
    HISTORY_S,
    CommandRecognizer,
    ComboRecognizer,
    Role,
    armed_bindings,
    default_roles,
    normalize_roles,
    priority,
    project,
)
