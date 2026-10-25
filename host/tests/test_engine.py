from pathlib import Path

from flexglove.engine import Engine
from flexglove.parser import Frame
from flexglove.storage import Storage


def make(tmp_path=None):
    return Engine(Storage(":memory:"), Path(tmp_path) if tmp_path else None)


def test_bits_debounce_and_toggle():
    e = make()
    e.set_binding("01", "light")
    e._on_frame(Frame(kind="bits", bits=(0, 1)), now=10.0)
    e.tick(10.05)
    assert e.committed is None and not e.light
    e.tick(10.3)
    assert e.committed == "01" and e.light
    e.tick(10.6)
    assert e.light


def test_five_value_streams_to_combo():
    e = make()
    e.source_info["connected"] = True
    t = 0.0
    for _ in range(40):                                   # покой — автокалибровка запоминает уровень
        e.on_raw(b"213;228;205;220;210\n"); t += 0.02
    for _ in range(40):                                   # согнули указательный и средний
        e.on_raw(b"213;900;905;220;210\n")
    assert e.mode == "values" and len(e.channels) == 5
    assert "".join(str(c.bit) for c in e.channels) == "01100"


def test_tagged_streams_are_ordered_by_tag():
    e = make()
    for tag in (5, 3, 1, 4, 2):
        e.on_raw(f"S{tag}:{200 + tag}".encode())
    assert len(e.channels) == 5
    assert [c.raw for c in e.channels] == [201, 202, 203, 204, 205]


def test_separate_characteristics_become_channels():
    e = make()
    e.on_raw(b"213", tag=0)
    e.on_raw(b"300", tag=1)
    assert [c.raw for c in e.channels] == [213, 300]


def test_comment_and_garbage():
    e = make()
    e.on_raw(b"# ch1=1200mV\n")
    assert e.unparsed_total == 0
    e.on_raw(b"garbage")
    assert e.unparsed_total == 1


def test_arm_requires_link():
    e = make()
    e.toggle_action("arm")
    assert not e.controller.armed
    e.controller.link_ok = True
    e.toggle_action("arm")
    assert e.controller.armed


def test_one_action_one_combo():
    e = make()
    e.set_binding("01", "hold")
    e.set_binding("10", "hold")
    assert e.storage.bindings() == {"10": "hold"}


def test_recording_roundtrip(tmp_path):
    from flexglove.sources import parse_recording
    e = make(tmp_path)
    e.on_raw(b"213;214\n")
    e.start_recording()
    for _ in range(5):
        e.on_raw(b"213;214\n")
    name = e.stop_recording()
    text = (tmp_path / "recordings" / name).read_text(encoding="utf-8-sig")
    header, rows = parse_recording(text)
    assert header[:2] == ["Time (s)", "S1 (ADC)"] and len(rows) == 5 and rows[0][1] == [213, 214]


def test_desktop_module_imports_and_detects_no_instance():
    from flexglove import desktop
    assert desktop.is_flexglove("http://127.0.0.1:9") is False
