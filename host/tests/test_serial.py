import asyncio
import os
import sys

import pytest

from flexglove.sources import SerialSource

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="pty есть только на macOS/Linux")


def test_serial_reads_lines():
    async def run():
        master, slave = os.openpty()
        got, status = [], []
        src = SerialSource(got.append, lambda **kw: status.append(kw), os.ttyname(slave))
        await src.start()
        os.write(master, b"01\n10\n")
        for _ in range(50):
            await asyncio.sleep(0.02)
            if len(got) >= 2:
                break
        await src.stop()
        return got, status

    got, status = asyncio.run(run())
    assert got[:2] == [b"01\n", b"10\n"]
    assert any(s.get("connected") for s in status)
