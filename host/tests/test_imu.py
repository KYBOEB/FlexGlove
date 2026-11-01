"""Протокол v0.3: IMU разбирается, хранится в состоянии, пишется в CSV и воспроизводится."""
import asyncio
from pathlib import Path

from flexglove.engine import Engine
from flexglove.sources import ReplaySource, parse_recording
from flexglove.storage import Storage


def make(tmp_path=None):
    return Engine(Storage(":memory:"), Path(tmp_path) if tmp_path else None)


def test_engine_keeps_imu_and_two_channels():
    e = make()
    e.on_raw(b"213;300|12;-4;998;5;-12;3\n")
    assert len(e.channels) == 2                       # IMU не становится каналами
    snap = e.snapshot()
    assert snap["imu"]["az"] == 998 and snap["imu"]["gx"] == 5 and snap["imu"]["total"] == 1
    assert snap["stats"]["format"] == "text-values+imu"


def test_v02_stream_has_no_imu_in_state():
    e = make()
    e.on_raw(b"213;300\n")
    assert e.snapshot()["imu"] is None


def test_imu_forgotten_after_disconnect():
    e = make()
    e.on_raw(b"213;300|12;-4;998;5;-12;3\n")
    e.on_source_status(connected=False)
    assert e.snapshot()["imu"] is None


def test_recording_has_imu_columns(tmp_path):
    e = make(tmp_path)
    e.on_raw(b"213;300|12;-4;998;5;-12;3\n")
    e.start_recording()
    for i in range(3):
        e.on_raw(f"213;300|12;-4;998;5;-12;{i}\n".encode())
    name = e.stop_recording()
    text = (tmp_path / "recordings" / name).read_text(encoding="utf-8-sig")
    header, rows = parse_recording(text)
    assert header == ["Time (s)", "S1 (ADC)", "S2 (ADC)", "ax (mg)", "ay (mg)", "az (mg)",
                      "gx (0.1 dps)", "gy (0.1 dps)", "gz (0.1 dps)"]
    assert rows[2][1] == [213, 300, 12, -4, 998, 5, -12, 2]


def test_recording_without_imu_keeps_v02_columns(tmp_path):
    e = make(tmp_path)
    e.on_raw(b"213;300\n")
    e.start_recording()
    e.on_raw(b"213;300\n")
    text = (tmp_path / "recordings" / e.stop_recording()).read_text(encoding="utf-8-sig")
    assert text.splitlines()[0] == "Time (s);S1 (ADC);S2 (ADC)"


def test_replay_of_imu_recording_sends_v03_lines():
    csv = "Time (s);S1 (ADC);S2 (ADC);ax (mg);ay (mg);az (mg);gx (0.1 dps);gy (0.1 dps);gz (0.1 dps)\n" \
          "0,000;213;300;12;-4;998;5;-12;3\n0,020;214;301;11;-4;997;5;-12;3\n"
    got = []

    async def run():
        src = ReplaySource(lambda d, tag=None: got.append(d), lambda **kw: None, csv, loop=False)
        await src.start()
        await asyncio.sleep(0.1)
        await src.stop()

    asyncio.run(run())
    assert got[0] == "213;300|12;-4;998;5;-12;3\n"
    e = make()
    for line in got:
        e.on_raw(line)
    assert len(e.channels) == 2 and e.imu == (11, -4, 997, 5, -12, 3)
