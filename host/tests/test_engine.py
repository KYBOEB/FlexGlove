from flexglove.engine import Engine
from flexglove.parser import Frame
from flexglove.storage import Storage


def make():
    return Engine(Storage(":memory:"))


def test_debounce_and_toggle():
    e = make()
    e.set_binding("01", "flashlight")
    e.raw_since = 0
    e._on_frame(Frame(bits=(0, 1)), now=10.0)
    e.tick(10.05)                      # 50 мс < 150 мс — ещё не жест
    assert e.committed is None and not e.action_state["flashlight"]
    e.tick(10.2)                       # удержали — срабатывает
    assert e.committed == "01" and e.action_state["flashlight"]
    e.tick(10.5)                       # повторно не срабатывает, пока жест удерживается
    assert e.action_state["flashlight"]


def test_on_raw_counts_unparsed():
    e = make()
    e.on_raw(b"garbage")
    assert e.unparsed_total == 1 and e.raw is None
    e.on_raw(b"11\n")
    assert e.raw.combo == "11" and e.n_channels == 2


def test_one_action_one_combo():
    e = make()
    e.set_binding("01", "eye")
    e.set_binding("10", "eye")
    assert e.storage.bindings() == {"10": "eye"}


def test_comment_lines_are_skipped():
    e = make()
    e.on_raw(b"# ch1=1200mV ch2=900mV\n")
    assert e.unparsed_total == 0 and e.raw is None
