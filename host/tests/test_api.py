import time

from fastapi.testclient import TestClient

from flexglove.app import create_app


def test_demo_flow(tmp_path):
    app = create_app(tmp_path / "t.db", demo=True)
    with TestClient(app) as c:
        assert c.get("/api/meta").json()["actions"][0]["id"] == "flashlight"
        assert c.post("/api/bindings", json={"combo": "00", "action_id": "fan"}).status_code == 400
        assert c.post("/api/bindings", json={"combo": "11", "action_id": "fan"}).json() == {"11": "fan"}
        c.post("/api/mock", json={"bits": "11"})
        for _ in range(50):
            time.sleep(0.05)
            st = c.get("/api/state").json()
            if st["actions"]["fan"]:
                break
        assert st["source"]["connected"] and st["committed"] == "11" and st["actions"]["fan"]
        assert st["events"][0]["source"] == "gesture"
        with c.websocket_connect("/ws") as ws:
            assert "actions" in ws.receive_json()
