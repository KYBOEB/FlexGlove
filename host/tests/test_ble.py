"""BLE-источник без железа: поддельный BleakClient проверяет подписку, переподключение и сообщения."""
import asyncio

from flexglove.sources.ble import BleSource, friendly_error


class FakeChar:
    def __init__(self, uuid, props, handle):
        self.uuid, self.properties, self.handle = uuid, props, handle


class FakeService:
    def __init__(self, uuid, chars):
        self.uuid, self.characteristics = uuid, chars


class World:
    """Состояние «радиоэфира»: сколько следующих подключений провалится."""

    def __init__(self, fail_next=0):
        self.fail_next = fail_next
        self.clients = []


def make_factory(world):
    class FakeClient:
        def __init__(self, address, disconnected_callback=None, timeout=15):
            self.cb = disconnected_callback
            self.is_connected = False
            self.notify = {}
            self.services = [FakeService("0000ffe0-0000-1000-8000-00805f9b34fb",
                                         [FakeChar("0000ffe1-0000-1000-8000-00805f9b34fb", ["notify"], 3)])]
            world.clients.append(self)

        async def connect(self):
            if world.fail_next > 0:
                world.fail_next -= 1
                raise TimeoutError("timed out")
            self.is_connected = True

        async def start_notify(self, char, cb):
            self.notify[char.uuid] = cb

        async def disconnect(self):
            self.is_connected = False

        def drop(self):                      # перчатку выключили
            self.is_connected = False
            self.cb(self)

        def send(self, data: bytes):
            for cb in self.notify.values():
                cb(None, bytearray(data))

    return FakeClient


def run_scenario(fail_after_drop):
    world = World()
    raw, status = [], []

    async def main():
        src = BleSource(lambda d, tag=None: raw.append(d), lambda **kw: status.append(kw), "AA:BB",
                        name="FlexGlove-01", client_factory=make_factory(world), reconnect_delays=(0.01, 0.01, 0.01))
        await src.start()
        world.clients[-1].send(b"213;300\n")
        world.fail_next = fail_after_drop
        world.clients[-1].drop()
        await asyncio.sleep(0.2)
        if world.clients[-1].is_connected:
            world.clients[-1].send(b"214;301\n")
        await src.stop()
        return src

    src = asyncio.run(main())
    return src, raw, status


def test_reconnects_after_drop():
    src, raw, status = run_scenario(fail_after_drop=1)       # 1-я попытка провалилась, 2-я удалась
    assert raw == [b"213;300\n", b"214;301\n"]
    msgs = [s.get("message", "") for s in status]
    assert any("потеряна" in m for m in msgs)
    assert any("Переподключение 2 из 3" in m for m in msgs)
    assert any(s.get("connected") and "переподключено" in s["message"] for s in status)
    assert src.reconnects == 1


def test_gives_up_after_three_attempts_with_clear_message():
    src, raw, status = run_scenario(fail_after_drop=10)
    final = [s for s in status if "Не удалось переподключиться" in s.get("message", "")]
    assert final and not final[0]["connected"] and "одно подключение" in final[0]["message"]
    assert src.reconnects == 0


def test_manual_stop_does_not_reconnect():
    world = World()
    status = []

    async def main():
        src = BleSource(lambda *a, **k: None, lambda **kw: status.append(kw), "AA:BB",
                        client_factory=make_factory(world), reconnect_delays=(0.01,))
        await src.start()
        await src.stop()
        await asyncio.sleep(0.05)

    asyncio.run(main())
    assert len(world.clients) == 1 and not any("Переподключение" in s.get("message", "") for s in status)


def test_friendly_errors():
    assert "одно подключение" in friendly_error(TimeoutError())
    assert "не найдена" in friendly_error(Exception("Device with address AA:BB was not found"))
