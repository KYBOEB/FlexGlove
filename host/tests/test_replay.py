from pathlib import Path

from flexglove.engine import Engine
from flexglove.sources import parse_recording
from flexglove.storage import Storage

DATA = Path(__file__).parent / "data" / "esp32_one_sensor_2026-10.csv"


def test_parse_zakhar_csv():
    header, rows = parse_recording(DATA.read_text(encoding="utf-8-sig"))
    assert header == ["Time (s)", "Sensor Value (ADC)"]
    assert len(rows) == 1815 and rows[0] == (0.024, [216.0])


def test_real_stream_at_rest_gives_no_bend():
    """Запись Захара — сенсор в покое: шум и выбросы не должны давать «сгиб»."""
    _, rows = parse_recording(DATA.read_text(encoding="utf-8-sig"))
    e = Engine(Storage(":memory:"))
    for _t, (v,) in rows:
        e.on_raw(f"{int(v)}\n".encode())
    ch = e.channels[0]
    assert e.mode == "values" and ch.mode == "uncalibrated" and ch.bit == 0
    assert abs(ch.filtered - 213) < 5
