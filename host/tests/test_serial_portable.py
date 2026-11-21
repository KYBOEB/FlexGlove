"""SerialSource без физического COM, pty и аудиоустройства; работает на Windows."""
import asyncio

import pytest
import serial

from flexglove.sources import SerialSource
from serial_helpers import Clock, InlineLoop, ScheduledSerial


def run_reader(arrivals):
    clock = Clock()
    got = []
    src = SerialSource(lambda data: got.append((clock(), data)), lambda **kw: None, "test")
    transport = ScheduledSerial(clock, arrivals, src._stop.set)
    src._ser, src._loop = transport, InlineLoop()
    src._reader()
    return got, transport


def test_short_lines_are_delivered_at_arrival_not_read_timeout():
    arrivals = [(100 + i * .02, b"213;228\n") for i in range(1, 31)]
    got, transport = run_reader(arrivals)
    assert [data for _, data in got] == [data for _, data in arrivals]
    assert [t for t, _ in got] == pytest.approx([t for t, _ in arrivals])
    assert transport.closed


def test_split_batch_crlf_lf_and_unfinished_tail():
    got, _ = run_reader([(100.02, b"21"), (100.04, b"3;228\r"),
                         (100.06, b"\n300;400\n500;600\r\nunfinished")])
    assert [data for _, data in got] == [b"213;228\r\n", b"300;400\n", b"500;600\r\n"]
    assert [t for t, _ in got] == pytest.approx([100.06] * 3)


def test_buffer_is_bounded_and_recovers():
    got, _ = run_reader([(100.02, b"x" * 10000), (100.04, b"\n213;228\n")])
    assert b"".join(data for _, data in got) == b"x" * 10000 + b"\n213;228\n"
    assert max(len(data) for _, data in got) <= 4096 + 256
    assert got[-1][1] == b"213;228\n"


def test_no_data_blocks_for_timeout():
    got, transport = run_reader([])
    assert got == []
    assert transport.reads == [1]
    assert transport.clock() == pytest.approx(100.2)


@pytest.mark.parametrize("operation", ["read", "in_waiting"])
@pytest.mark.parametrize("error", [serial.SerialException("unplugged"), OSError("disconnected")])
def test_port_error_is_delivered_after_except_and_port_closes(operation, error):
    callbacks, statuses = [], []

    class Loop:
        def call_soon_threadsafe(self, callback, *args):
            callbacks.append((callback, args))

    class BrokenPort:
        closed = False

        @property
        def in_waiting(self):
            if operation == "in_waiting":
                raise error
            return 0

        def read(self, size):
            raise error

        def close(self):
            self.closed = True

    src = SerialSource(lambda data: pytest.fail("unexpected data"), lambda **kw: statuses.append(kw), "test")
    src._ser, src._loop = BrokenPort(), Loop()
    src._reader()
    assert src._ser.closed
    assert statuses == []
    for callback, args in callbacks:
        callback(*args)
    assert statuses == [dict(kind="serial", name="test", connected=False, message=f"Порт закрыт: {error}")]


def test_start_idle_stop_restart_and_discard_queued_callbacks(monkeypatch):
    ports = []

    def open_port(port, baud, timeout):
        assert timeout > 0
        transport = serial.serial_for_url("loop://", baudrate=baud, timeout=timeout)
        ports.append(transport)
        return transport

    monkeypatch.setattr("flexglove.sources.serial_src.serial.Serial", open_port)

    async def run():
        got, statuses = [], []
        src = SerialSource(got.append, lambda **kw: statuses.append(kw), "test")
        for _ in range(2):
            await src.start()
            assert src._thread.is_alive()
            await asyncio.sleep(.25)  # пустой порт должен пережить timeout и продолжить чтение
            ports[-1].write(b"213;228\r\n300;400\n")
            for _ in range(100):
                if len(got) == 2:
                    break
                await asyncio.sleep(.01)
            assert got == [b"213;228\r\n", b"300;400\n"]
            await asyncio.wait_for(src.stop(), 3)
            assert not src._thread.is_alive()
            assert not ports[-1].is_open
            src._deliver(b"late\n")
            src._report_error("late error")
            assert got == [b"213;228\r\n", b"300;400\n"]
            assert statuses[-1]["message"] == "Не подключено"
            got.clear()
        await src.stop()

    asyncio.run(run())


def test_open_error_propagates(monkeypatch):
    def fail(*args, **kwargs):
        raise serial.SerialException("cannot open")

    monkeypatch.setattr("flexglove.sources.serial_src.serial.Serial", fail)
    src = SerialSource(lambda data: None, lambda **kw: None, "test")
    with pytest.raises(serial.SerialException, match="cannot open"):
        asyncio.run(src.start())
    asyncio.run(src.stop())
