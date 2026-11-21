"""Последовательные байты → настоящий parser/Engine → обучение/событие → аудиозаглушка."""
import pytest
import serial

from flexglove.engine import Engine
from flexglove.sources import SerialSource
from flexglove.storage import Storage
from serial_helpers import Clock, InlineLoop, ScheduledSerial


class AudioStub:
    def __init__(self):
        self.jobs = []
        self.pending = []

    def stop(self):
        self.pending.clear()

    def resolve(self, name):
        return name

    def enqueue(self, gesture_id, audio):
        self.jobs.append((gesture_id, audio))
        self.pending.append((gesture_id, audio))
        return {"status": "queued", "message": "тест без устройства"}


class Rig:
    def __init__(self, tmp_path):
        self.clock = Clock()
        self.storage = Storage(tmp_path / "serial-test.db")
        self.storage.put("calibration", {"open": [200, 300], "fist": [1200, 1300]})
        self.audio = AudioStub()
        self.engine = Engine(self.storage, tmp_path, clock=self.clock, audio=self.audio)
        self.engine.on_source_status(kind="serial", connected=True)
        self.source = SerialSource(self.on_raw, self.engine.on_source_status, "test")
        self.source._loop = InlineLoop()

    def on_raw(self, data):
        self.engine.on_raw(data)
        self.engine.tick(self.clock())
        self.remaining -= 1
        if self.remaining == 0:
            self.source._stop.set()

    def pose(self, values, seconds):
        start = self.clock()
        line = (";".join(map(str, values)) + "\n").encode()
        arrivals = [(round(start + i * .02, 8), line) for i in range(1, round(seconds / .02) + 1)]
        self.remaining = len(arrivals)
        self.source._stop.clear()
        self.source._ser = ScheduledSerial(self.clock, arrivals, self.source._stop.set)
        self.source._reader()
        assert self.clock() == pytest.approx(arrivals[-1][0], abs=1e-8, rel=0)

    def train(self):
        self.pose([1000, 400], .8)
        self.engine.train_gesture("Тестовая поза")
        self.pose([1000, 400], 1.04)
        assert self.engine.gestures.training_result["status"] == "saved"
        gesture_id = self.engine.gestures.training_result["id"]
        self.engine.edit_gesture(gesture_id, audio="stub.mp3", assign_audio=True)
        assert self.engine.gestures.templates[0]["context"]["profile"] == "glove"
        return gesture_id


@pytest.mark.parametrize("gap,tick_during_gap", [(.16, False), (.6, True)])
def test_serial_training_hold_gap_recovery_and_rearm(tmp_path, gap, tick_during_gap):
    rig = Rig(tmp_path)
    try:
        gesture_id = rig.train()
        rig.pose([1000, 400], 2)
        assert rig.audio.jobs == []  # обучение в удержанной позе не запускает фразу
        rig.pose([300, 400], 1)
        rig.pose([1000, 400], .5)
        assert rig.audio.jobs == []  # короткого удержания недостаточно
        rig.pose([1000, 400], 1.2)
        assert rig.audio.jobs == [(gesture_id, "stub.mp3")]
        rig.pose([1000, 400], 2)
        assert rig.engine.gestures.sequence == 1
        assert len(rig.audio.jobs) == 1

        # Настоящий разрыв во время незавершённого удержания сбрасывает прогресс.
        rig.pose([300, 400], 1)
        rig.pose([1000, 400], .5)
        rig.clock.now += gap
        if tick_during_gap:
            rig.engine.tick(rig.clock())
            assert not rig.audio.pending
        rig.pose([1000, 400], 2)
        assert rig.engine.gestures.gated
        assert rig.engine.gestures.sequence == 1
        assert len(rig.audio.jobs) == 1  # восстановление в позе не запускает аудио
        rig.pose([300, 400], 1)
        rig.pose([1000, 400], 1.7)
        assert rig.engine.gestures.sequence == 2
        assert len(rig.audio.jobs) == 2
    finally:
        rig.storage.close()


def test_serial_read_error_reaches_engine_and_cancels_audio(tmp_path):
    rig = Rig(tmp_path)
    callbacks = []

    class DeferredLoop:
        def call_soon_threadsafe(self, callback, *args):
            callbacks.append((callback, args))

    class Unplugged:
        in_waiting = 0
        closed = False

        def read(self, size):
            raise serial.SerialException("test unplugged")

        def close(self):
            self.closed = True

    try:
        rig.train()
        rig.pose([300, 400], 1)
        rig.pose([1000, 400], 1.7)
        assert rig.audio.pending
        rig.source._stop.clear()
        rig.source._ser, rig.source._loop = Unplugged(), DeferredLoop()
        rig.source._reader()
        for callback, args in callbacks:
            callback(*args)
        assert rig.source._ser.closed
        assert not rig.engine.source_info["connected"]
        assert "test unplugged" in rig.engine.source_info["message"]
        assert rig.engine.gestures.gated
        assert not rig.audio.pending
    finally:
        rig.storage.close()
