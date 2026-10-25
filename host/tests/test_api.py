import time

from fastapi.testclient import TestClient

from flexglove.app import create_app


def wait(c, cond, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = c.get("/api/state").json()
        if cond(st):
            return st
        time.sleep(0.05)
    return st


def test_demo_flow(tmp_path):
    app = create_app(tmp_path / "t.db", demo=True)
    with TestClient(app) as c:
        assert c.get("/api/ping").json()["app"] == "flexglove"
        assert [a["id"] for a in c.get("/api/meta").json()["actions"]] == ["arm", "hold", "light"]
        assert c.post("/api/bindings", json={"combo": "00000", "action_id": "arm"}).status_code == 400
        c.post("/api/bindings", json={"combo": "01100", "action_id": "light"})
        st = wait(c, lambda s: s["n_channels"] == 5 and s["mode"] == "values")
        assert st["n_channels"] == 5
        time.sleep(0.8)                                   # автокалибровка: покой
        c.post("/api/mock", json={"bits": "01100"})
        st = wait(c, lambda s: s["actions"]["light"])
        assert st["combo"] == "01100" and st["actions"]["light"]
        # управление захватом: разрешить и сжать кисть
        c.post("/api/mock", json={"bits": "00000"})
        wait(c, lambda s: s["combo"] == "00000")
        c.post("/api/actions/arm/toggle")
        c.post("/api/mock", json={"bends": [1, 1, 1, 1, 1]})
        st = wait(c, lambda s: s["controller"]["command"] > 0.8)
        assert st["controller"]["state"] == "active" and st["controller"]["command"] <= 0.9 + 1e-6
        with c.websocket_connect("/ws") as ws:
            assert "controller" in ws.receive_json()


def test_replay_and_recording(tmp_path):
    app = create_app(tmp_path / "t.db")
    csv_text = "Time (s);S1;S2\n" + "".join(f"{i * 0.02:.3f}".replace(".", ",") + ";213;300\n" for i in range(100))
    with TestClient(app) as c:
        r = c.post("/api/connect", json={"kind": "replay", "text": csv_text, "name": "t.csv"})
        assert r.status_code == 200
        st = wait(c, lambda s: s["n_channels"] == 2)
        assert st["channels"][1]["raw"] == 300
        c.post("/api/recording/start")
        time.sleep(0.3)
        name = c.post("/api/recording/stop").json()["file"]
        assert any(f["name"] == name for f in c.get("/api/recordings").json())
        assert c.get(f"/api/recordings/{name}").text.startswith("﻿Time (s);S1 (ADC);S2 (ADC)")
        assert c.post("/api/connect", json={"kind": "replay", "text": "nothing"}).status_code == 400
