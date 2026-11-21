"""Замер доставки SerialSource через программный pyserial loop://, без COM/аудио/БД.

python tools/measure_serial_loopback.py --samples 100
python tools/measure_serial_loopback.py --samples 100 --source-ref origin/main
Порогов точного планирования ОС нет: выводим реальные задержки и интервалы.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time

import serial

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from flexglove.sources.serial_src import SerialSource


async def measure(samples, interval):
    port = serial.serial_for_url("loop://", timeout=.2)
    received, sent = [], []
    complete = asyncio.Event()
    src = SerialSource(lambda data: receive(data), lambda **kw: None, "loop://")

    def receive(data):
        received.append((time.perf_counter(), data))
        if len(received) == samples:
            complete.set()

    # Открыт программный транспорт; start() для настоящих COM остаётся прежним.
    src._ser = port
    src._loop = asyncio.get_running_loop()
    src._thread = threading.Thread(target=src._reader, daemon=True)
    src._thread.start()
    try:
        for i in range(samples):
            line = f"{i:04d};228\n".encode()
            sent.append((time.perf_counter(), line))
            port.write(line)
            await asyncio.sleep(interval)
        await asyncio.wait_for(complete.wait(), 5)
    finally:
        await src.stop()
    assert [data for _, data in received] == [data for _, data in sent]
    delays = sorted((r[0] - s[0]) * 1000 for r, s in zip(received, sent))
    gaps = [(b[0] - a[0]) * 1000 for a, b in zip(received, received[1:])]
    return {"samples": samples, "requested_interval_ms": interval * 1000,
            "latency_ms": {"min": min(delays), "median": statistics.median(delays),
                           "p95": delays[int((len(delays) - 1) * .95)], "max": max(delays)},
            "delivery_gap_ms": {"median": statistics.median(gaps), "max": max(gaps)}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--interval", type=float, default=.02)
    parser.add_argument("--source-ref", help="Сравнить с reader из указанного git ref, без изменения checkout")
    args = parser.parse_args()
    if args.samples < 2 or args.interval <= 0:
        parser.error("Нужны samples >= 2 и interval > 0")
    if args.source_ref:
        source = subprocess.check_output(["git", "show", f"{args.source_ref}:host/flexglove/sources/serial_src.py"],
                                         cwd=Path(__file__).resolve().parents[1]).decode("utf-8")
        namespace = {"__name__": "flexglove.sources.serial_baseline", "__package__": "flexglove.sources"}
        exec(compile(source, "serial_baseline.py", "exec"), namespace)
        SerialSource._reader = namespace["SerialSource"]._reader
    print(json.dumps({"source": args.source_ref or "working-tree", **asyncio.run(measure(args.samples, args.interval))},
                     indent=2))


if __name__ == "__main__":
    main()
