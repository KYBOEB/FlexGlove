"""Очередь с тестовым декодером; фиктивные .mp3 никогда не отправляются на устройство."""
import threading

import pytest
from fastapi.testclient import TestClient

from flexglove.app import create_app
from flexglove.audio import AudioPlayer, PygameDriver
from flexglove.engine import Engine
from flexglove.parser import Frame
from flexglove.storage import Storage


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FakeDriver:
    def __init__(self):
        self.played = []
        self.fail = False

    def play(self, path, cancelled):
        if self.fail:
            raise RuntimeError("Тестовая ошибка устройства")
        self.played.append(path.name)


def player(tmp_path, limit=4):
    folder = tmp_path / "audio"
    folder.mkdir(exist_ok=True)
    for name in ("a.mp3", "b.mp3", "c.mp3"):
        (folder / name).write_bytes(b"fake-decoder-only")
    return AudioPlayer(folder, FakeDriver(), limit)


def engine(tmp_path):
    clock = Clock()
    e = Engine(Storage(tmp_path / "test.db"), tmp_path, clock=clock, audio=player(tmp_path))
    e.on_source_status(kind="mock", connected=True)
    return e, clock


def run(e, clock, bends, seconds):
    for _ in range(round(seconds / .02)):
        clock.now = round(clock.now + .02, 8)
        e._on_frame(Frame(kind="values", values=tuple(213 + 620 * b if i == 0 else 228 + 760 * b
                    for i, b in enumerate(bends))), clock())
        e.tick(clock())


def train(e, clock, name, bends):
    run(e, clock, bends, .8)  # фильтры должны закончить переход до начала обучения
    e.train_gesture(name)
    run(e, clock, bends, 1.04)
    assert e.gestures.training_result["status"] == "saved"
    return e.gestures.training_result["id"]


def test_queue_order_limits_duplicate_completion_and_missing(tmp_path):
    p = player(tmp_path, 2)
    assert p.enqueue("A", None)["status"] == "unavailable"
    assert p.enqueue("A", "missing.mp3")["status"] == "unavailable"
    assert p.enqueue("A", "a.mp3")["status"] == "queued"
    assert p.enqueue("A", "a.mp3")["status"] == "duplicate"
    assert p.enqueue("B", "b.mp3")["status"] == "queued"
    assert p.enqueue("C", "c.mp3")["status"] == "full"
    p.process_next()
    p.process_next()
    assert p.driver.played == ["a.mp3", "b.mp3"]
    assert p.snapshot()["results"][0]["status"] == "finished"
    assert p.snapshot()["queue"] == []


def test_player_error_does_not_block_next_job(tmp_path):
    p = player(tmp_path)
    p.enqueue("A", "a.mp3")
    p.enqueue("B", "b.mp3")
    p.driver.fail = True
    p.process_next()
    assert p.snapshot()["results"][0]["status"] == "error"
    p.driver.fail = False
    p.process_next()
    assert p.driver.played == ["b.mp3"]
    assert p.snapshot()["results"][0]["status"] == "finished"


def test_file_removed_after_enqueue(tmp_path):
    p = player(tmp_path)
    p.enqueue("A", "a.mp3")
    (p.folder / "a.mp3").unlink()
    p.process_next()
    assert p.snapshot()["results"][0]["status"] == "error"
    assert not p.driver.played


@pytest.mark.parametrize("name", ["../a.mp3", "..\\a.mp3", "D:\\a.mp3", "/a.mp3", "x/a.mp3", "a.wav", "file:stream.mp3"])
def test_path_restriction(tmp_path, name):
    p = player(tmp_path)
    with pytest.raises(ValueError):
        p.resolve(name)
    assert p.enqueue("A", name)["status"] == "unavailable"


def test_stop_cancels_active_and_queue_and_dedup_while_playing(tmp_path):
    p = player(tmp_path)
    entered, completed = threading.Event(), threading.Event()

    class BlockingDriver:
        def play(self, path, cancelled):
            entered.set()
            assert cancelled.wait(2), "Проигрыватель не получил остановку"
            completed.set()

    p.driver = BlockingDriver()
    p.enqueue("A", "a.mp3")
    p.start()
    assert entered.wait(2)
    assert p.enqueue("A", "a.mp3")["status"] == "duplicate"
    assert p.enqueue("B", "b.mp3")["status"] == "queued"
    p.stop()
    assert completed.wait(2)
    p.close()
    assert not p.thread.is_alive()
    assert not p.snapshot()["queue"]
    assert sum(r["status"] == "cancelled" for r in p.snapshot()["results"]) == 2


def test_end_of_audio_does_not_rearm_gesture_and_disconnect_clears_queue(tmp_path):
    e, clock = engine(tmp_path)
    gesture_id = train(e, clock, "A", [.8, .1])
    e.edit_gesture(gesture_id, audio="a.mp3", assign_audio=True)
    run(e, clock, [.8, .1], 2)
    assert e.gestures.sequence == 0  # после обучения удержанная поза не срабатывает
    run(e, clock, [.1, .1], 1)
    run(e, clock, [.8, .1], 1.6)
    assert e.gestures.sequence == 1
    assert len(e.audio.snapshot()["queue"]) == 1
    assert any(ev["action"] == "gesture_recognized" for ev in e.events)
    assert any(ev["action"] == "audio_queue" for ev in e.events)
    e.audio.process_next()
    assert e.audio.driver.played == ["a.mp3"]
    run(e, clock, [.8, .1], 4)
    assert e.gestures.sequence == 1 and not e.audio.pending
    e.audio.enqueue(gesture_id, "a.mp3", "test")
    e.on_source_status(kind="mock", connected=False)
    assert not e.audio.pending
    e.on_source_status(kind="mock", connected=True)
    run(e, clock, [.8, .1], 3)
    assert e.gestures.sequence == 1
    run(e, clock, [.1, .1], 1)
    run(e, clock, [.8, .1], 1.6)
    assert e.gestures.sequence == 2


def test_no_auto_calibration_training_and_missing_channel(tmp_path):
    e, clock = engine(tmp_path)
    gesture_id = train(e, clock, "A", [.8, .1])
    run(e, clock, [.8], 1)
    assert not e.gestures.compatible(e.gestures.templates[0])
    assert len(e.storage.gestures()) == 1
    e.on_source_status(kind="ble", connected=True)
    run(e, clock, [.8, .1], 1)
    with pytest.raises(ValueError, match="автокалибровка"):
        e.train_gesture("Fail")
    assert e.storage.gestures()[0]["id"] == gesture_id


def test_tagged_positional_switch_resets_channel_identity(tmp_path):
    e, clock = engine(tmp_path)
    e.on_raw("S1:213")
    e.on_raw("S2:228")
    tagged = e.gesture_input(clock())[1]
    e.on_raw("213")
    positional = e.gesture_input(clock())[1]
    assert e.tag_order == [] and len(e.channels) == 1
    assert tagged["channels"][0]["id"] != positional["channels"][0]["id"]
    e.on_raw("S2:228")
    assert e.tag_order == [2] and len(e.channels) == 1
    assert e.channels[0].raw == 228


def test_tagged_channel_stale_then_restore_requires_new_exit(tmp_path):
    e, clock = engine(tmp_path)
    # Подменяем только доставку кадра: реальные метки, фиксированные диапазоны демо.
    for _ in range(50):
        clock.now += .02
        e.on_raw("S1:709")
        e.on_raw("S2:304")
        e.tick(clock())
    e.train_gesture("tagged")
    for _ in range(52):
        clock.now += .02
        e.on_raw("S1:709")
        e.on_raw("S2:304")
        e.tick(clock())
    assert e.gestures.training_result["status"] == "saved"
    for _ in range(30):
        clock.now += .02
        e.on_raw("S1:709")  # S2 пропал, S1 продолжает обновляться
        e.tick(clock())
    assert "свежих" in e.gestures.reason
    for _ in range(100):
        clock.now += .02
        e.on_raw("S1:709")
        e.on_raw("S2:304")
        e.tick(clock())
    assert e.gestures.sequence == 0


@pytest.mark.parametrize("blocking", ["training", "calibration", "wizard", "imu", "reset"])
def test_learning_and_calibration_block_events_and_cancel_queue(tmp_path, blocking):
    e, clock = engine(tmp_path)
    e.storage.put("calibration", {"open": [213, 228], "fist": [833, 988]})
    e.on_source_status(kind="ble", connected=True)
    train(e, clock, "A", [.8, .1])
    e.audio.enqueue("old", "a.mp3")
    if blocking == "training":
        e.train_gesture("Another")
    elif blocking == "calibration":
        e.start_calibration("open")
    elif blocking == "wizard":
        e.wizard_start("open")
    elif blocking == "imu":
        e.imu_tracker.start_capture(clock())
    else:
        e.reset_calibration()
    run(e, clock, [.8, .1], .6)
    assert e.gestures.sequence == 0 and not e.audio.pending


def test_rename_delete_and_multiple_api_clients_do_not_recognize_twice(tmp_path):
    app = create_app(tmp_path / "api.db")
    e = app.state.flex.engine
    clock = Clock()
    e.clock = clock
    e.audio = player(tmp_path)
    e.on_source_status(kind="mock", connected=True)
    gesture_id = train(e, clock, "A", [.8, .1])
    # Источником и временем управляет стенд, звук — FakeDriver.
    with TestClient(app) as client:
        # tick_loop использует clock; источник не запускается.
        assert client.patch(f"/api/gestures/{gesture_id}", json={"name": "Новое имя"}).status_code == 200
        assert client.patch(f"/api/gestures/{gesture_id}", json={"audio": "../outside.mp3"}).status_code == 400
        assert client.patch(f"/api/gestures/{gesture_id}", json={"audio": "missing.mp3"}).status_code == 200
        assert client.post(f"/api/audio/test/{gesture_id}").json()["status"] == "unavailable"
        # Подписчики состояния не вызывают ни распознавание, ни постановку аудио.
        q1, q2 = e.subscribe(), e.subscribe()
        with client.websocket_connect("/ws") as ws1, client.websocket_connect("/ws") as ws2:
            assert ws1.receive_json()["gesture_system"]["templates"][0]["name"] == "Новое имя"
            assert ws2.receive_json()["gesture_system"]["templates"][0]["name"] == "Новое имя"
            for _ in range(10):
                client.get("/api/state")
            assert e.gestures.sequence == 0
            assert client.patch(f"/api/gestures/{gesture_id}", json={"audio": "a.mp3"}).status_code == 200
            client.portal.call(run, e, clock, [.1, .1], 1)
            client.portal.call(run, e, clock, [.8, .1], 1.6)
            for _ in range(10):
                client.get("/api/state")
            assert e.gestures.sequence == 1
            assert sum(r["status"] == "queued" for r in e.audio.snapshot()["results"]) == 1
        e.unsubscribe(q1)
        e.unsubscribe(q2)
        assert client.post("/api/audio/stop").status_code == 200
        assert client.delete(f"/api/gestures/{gesture_id}").status_code == 200
        assert e.storage.gestures() == []


def test_pygame_adapter_contract_without_device(monkeypatch, tmp_path):
    calls = []
    cancel = threading.Event()

    class Music:
        def load(self, path): calls.append(("load", path))
        def play(self, loops): calls.append(("play", loops))
        def get_busy(self): return False
        def stop(self): calls.append("stop")
        def unload(self): calls.append("unload")

    class Mixer:
        music = Music()
        def init(self): calls.append("init")
        def get_init(self): return True
        def quit(self): calls.append("quit")

    monkeypatch.setattr("flexglove.audio.importlib.import_module", lambda name: Mixer())
    PygameDriver().play(tmp_path / "a.mp3", cancel)
    assert calls == ["init", ("load", str(tmp_path / "a.mp3")), ("play", 0), "stop", "unload", "quit"]
