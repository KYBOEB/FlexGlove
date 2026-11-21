"""Поведение демо и изоляция пользовательской калибровки (этап 4г)."""
import pytest
from fastapi.testclient import TestClient

from flexglove.app import create_app
from flexglove.engine import Engine
from flexglove.storage import Storage
from flexglove.sources.mock import REST, BEND_DELTA, MockSource


def test_auto_fingers_do_not_change_imu_target():
    m = MockSource(lambda *_: None, lambda **_: None)
    m.set_angles(roll=35, pitch=-25, yaw=40)
    m.set_auto(True)
    for i in range(800):
        m.imu_sample(i * .027, .027)
    assert m.angles == pytest.approx([35, -25, 40])
    assert m.angle_target == [35, -25, 40]


def test_source_switch_keeps_real_calibration_and_resets_processing(tmp_path):
    e = Engine(Storage(':memory:'), tmp_path)
    real = {'open': [150] * 8, 'fist': [1200] * 8}
    e.storage.put('calibration', real)
    for kind in ['mock', 'serial', 'mock', 'ble']:
        e.on_source_status(kind=kind, connected=True)
        assert e.channels == [] and not e.bits and e.imu is None
        assert e.rate() == 0 and e.last_data_time == 0
        assert e.capture is None and e.wizard.step == 'idle'
        assert e.imu_tracker.count == 0
        for i in range(60):
            e.on_raw(';'.join(str(REST[j % 5]) for j in range(8)))
        assert len(e.channels) == 8
        if kind == 'mock':
            assert [c.cal.open for c in e.channels] == [REST[i % 5] for i in range(8)]
            assert all(c.bend < .01 for c in e.channels)
            for _ in range(100):
                e.on_raw(';'.join(str(REST[j % 5] + BEND_DELTA[j % 5]) for j in range(8)))
            assert all(c.bend > .99 for c in e.channels)
        else:
            assert [c.cal.open for c in e.channels] == real['open']
            e.start_calibration('open')
            e.wizard_start('open')
        assert e.storage.get('calibration') == real
        e.on_source_status(connected=False, kind=None)


def test_demo_calibration_api_cannot_write_real_profile(tmp_path):
    app = create_app(tmp_path / 'test.db', demo=True)
    e = app.state.flex.engine
    real = {'open': [100] * 5, 'fist': [1000] * 5}
    e.storage.put('calibration', real)
    with TestClient(app) as c:
        for url in ['/api/calibration/open', '/api/calibration/fist',
                    '/api/calibration/wizard/open', '/api/calibration/wizard-save']:
            assert c.post(url).status_code == 409
        assert c.delete('/api/calibration').status_code == 409
        assert e.storage.get('calibration') == real


def test_actual_model_palm_anchor_does_not_translate():
    import json
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which('node')
    if node is None:
        import os
        if os.environ.get('CI'):
            pytest.fail('В CI нужен Node.js')
        pytest.skip('Для проверки матриц three.js нужен Node.js')
    result = subprocess.run([node, '--experimental-vm-modules', str(Path(__file__).with_name('js') / 'hand3d_scene.mjs')],
                            capture_output=True, text=True, encoding='utf-8', timeout=40)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['count'] == 144


def test_idle_websocket_keeps_publishing_heartbeat(tmp_path):
    import asyncio
    async def check():
        e = Engine(Storage(':memory:'), tmp_path)
        q = e.subscribe()
        task = asyncio.create_task(e._broadcast_loop())
        try:
            first = await asyncio.wait_for(q.get(), 1.5)
            second = await asyncio.wait_for(q.get(), 1.5)
            assert first['mode'] == second['mode'] == 'none'
            assert not second['source']['connected']
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            e.storage.close()
    asyncio.run(check())
