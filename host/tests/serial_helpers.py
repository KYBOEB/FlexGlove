"""Переносимый транспорт с семантикой read(size)/timeout pyserial и управляемым временем."""
from collections import deque


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class ScheduledSerial:
    timeout = .2

    def __init__(self, clock, arrivals, on_end):
        self.clock = clock
        self.arrivals = deque(arrivals)
        self.buffer = bytearray()
        self.on_end = on_end
        self.closed = False
        self.reads = []

    @property
    def in_waiting(self):
        while self.arrivals and self.arrivals[0][0] <= self.clock():
            _, data = self.arrivals.popleft()
            self.buffer.extend(data)
        return len(self.buffer)

    def read(self, size):
        self.reads.append(size)
        deadline = self.clock() + self.timeout
        while self.in_waiting < size and self.arrivals and self.arrivals[0][0] <= deadline:
            self.clock.now = self.arrivals[0][0]
        if len(self.buffer) < size:
            self.clock.now = deadline
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        if not data and not self.arrivals:
            self.on_end()
        return data

    def close(self):
        self.closed = True


class InlineLoop:
    # Тест выполняет reader и ядро в одном потоке, без планировщика ОС.
    def call_soon_threadsafe(self, callback, *args):
        callback(*args)
