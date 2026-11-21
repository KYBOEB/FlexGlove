"""Браузерная регрессия четырёх экранов, только временная БД и тестовое аудио.

python tools/qa_ui_navigation.py --browser путь-к-chrome.exe
Playwright устанавливается отдельно; сценарий сам запускает и перезапускает сервер.
Потери связи и редкие состояния проверяются подменой снимков на границе WebSocket.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/analysis/img/ui-navigation"


def serve(db, port):
    sys.path.insert(0, str(ROOT / "host"))
    import uvicorn
    from flexglove.app import create_app
    from flexglove.audio import AudioPlayer

    class FailedDevice:
        def play(self, path, cancel):
            raise RuntimeError("Тестовая ошибка аудиоустройства")

    app = create_app(db)
    engine = app.state.flex.engine
    folder = Path(db).parent / "audio"
    folder.mkdir(exist_ok=True)
    (folder / "device-error.mp3").write_bytes(b"QA fixture; decoder is replaced")
    engine.audio = AudioPlayer(folder, driver=FailedDevice())
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")


def fingerprints():
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for folder in (ROOT / "data", ROOT / "audio")
            for p in folder.rglob("*") if p.is_file()}


def main(args):
    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    original = fingerprints()
    with tempfile.TemporaryDirectory(prefix="flexglove-ui-") as temp:
        db = Path(temp) / "test.db"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"

        def start():
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--serve", "--db", str(db), "--port", str(port)],
                cwd=ROOT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(100):
                try:
                    urllib.request.urlopen(url + "/api/ping", timeout=.5).close()
                    return process
                except OSError:
                    if process.poll() is not None:
                        raise RuntimeError("QA-сервер завершился до запуска")
                    time.sleep(.1)
            process.terminate()
            raise RuntimeError("QA-сервер не запустился")

        process = start()
        errors, results = [], []
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(**({"executable_path": args.browser} if args.browser else {}))
                for width, height in [(1366, 768), (1920, 1080)]:
                    print(f"viewport {width}x{height}", flush=True)
                    context = browser.new_context(viewport={"width": width, "height": height})
                    page = context.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    control = {"patch": None, "drop": False, "connections": 0}

                    def websocket(route):
                        control["connections"] += 1
                        server = route.connect_to_server()

                        def message(raw):
                            if control["drop"]:
                                return
                            state = json.loads(raw)
                            if control["patch"]:
                                control["patch"](state)
                            route.send(json.dumps(state))
                        server.on_message(message)

                    page.route_web_socket("**/ws", websocket)
                    page.add_init_script("window.WebSocket = class extends WebSocket { constructor(...args) { super(...args); window.__qaWS = this; } };")

                    def instrument(route):
                        response = route.fetch()
                        route.fulfill(response=response, body=response.text().replace(
                            "return new Hand3D(container);", "return (window.__qaHand = new Hand3D(container));").replace(
                            "return new Hand3D(container, options);", 'const h = new Hand3D(container, options); if (container.id === "hand3d") window.__qaHand = h; return h;'))
                    page.route("**/hand3d.js?*", instrument)

                    def api(path, data=None, method="post"):
                        response = getattr(page.request, method)(url + path, **({"data": data} if data is not None else {}))
                        assert response.ok, response.text()
                        return response.json()

                    def state():
                        return api("/api/state", method="get")

                    def tab(name):
                        page.locator(f'.tab[data-tab="{name}"]').click()
                        page.wait_for_function("name => !document.querySelector('#tab-'+name).hidden", arg=name)
                        page.evaluate("window.scrollTo(0,0)")

                    def shot(name, full=False):
                        if name.startswith("after-"):
                            page.wait_for_function("document.querySelector('#toast').hidden")
                        page.screenshot(path=str(OUT / f"{name}-{width}.png"), full_page=full)

                    def text(value):
                        page.wait_for_function("value => document.querySelector('#gestureWords').textContent === value", arg=value)

                    api("/api/disconnect")
                    # Эта БД создана самим сценарием и не связана с data/ пользователя.
                    for template in state()["gesture_system"]["templates"]:
                        api("/api/gestures/" + template["id"], method="delete")
                    page.goto(url)
                    text("Подключите источник")
                    assert page.locator("#demoStrip").is_hidden()
                    shot("state-no-source")
                    tab("settings")
                    page.locator('[data-kind="mock"]').click()
                    page.locator("#mockStart").click()
                    page.wait_for_function("document.querySelector('#demoStrip').hidden === false")
                    text("Создайте жест на экране «Жесты»")
                    assert page.locator("#gestureProgress").is_hidden()
                    shot("state-no-gestures")
                    api("/api/mock", {"bends": [.1, .8, .8, .1, .1]})
                    page.wait_for_timeout(1000)
                    tab("gestures")
                    if width == 1366:
                        assert page.locator("#audioResult").is_hidden()
                    name = "Указательный и средний"
                    page.locator("#createGesture").click()
                    page.locator("#gestureName").fill(name)
                    page.locator("#gestureTrain").click()
                    page.wait_for_function("document.querySelector('#cancelTraining').hidden === false")
                    shot("state-training")
                    page.wait_for_function("document.querySelector('#gestureList strong')?.textContent === 'Указательный и средний'")
                    page.locator("#gestureClose").click()
                    template = state()["gesture_system"]["templates"][0]
                    assert len(template["reference"]) == 5
                    page.once("dialog", lambda dialog: dialog.accept("Поза <тест>"))
                    page.locator('[data-gesture-action="rename"]').click()
                    page.wait_for_function("document.querySelector('#gestureList strong').textContent === 'Поза <тест>'")
                    assert page.locator("#gestureList тест").count() == 0
                    page.once("dialog", lambda dialog: dialog.accept(name))
                    page.locator('[data-gesture-action="rename"]').click()
                    page.wait_for_function("document.querySelector('#gestureList strong').textContent === 'Указательный и средний'")
                    # Обучение и удержанная после него поза не вызывают фразу.
                    before = state()["gesture_system"]["triggered"]
                    page.wait_for_timeout(800)
                    assert state()["gesture_system"]["triggered"] == before
                    tab("hand")
                    text(name)
                    shot("after-hand")
                    # Фиксированный центр ладони остаётся центром доступной сцены.
                    page.wait_for_function("window.__qaHand?.model?.kind === 'gltf'")
                    measure = page.evaluate("""() => {
                        const h=window.__qaHand, m=h.model;
                        h.scene.updateMatrixWorld(true);
                        const p=m.mesh.skeleton.bones.find(b=>b.name==='wrist').getWorldPosition(h.camera.position.clone())
                          .lerp(m.chains.middle.bones[1].getWorldPosition(h.camera.position.clone()),.5).project(h.camera);
                        return {error:Math.hypot((p.x+1)*h.container.clientWidth/2-h.container.clientWidth/2,
                          (1-p.y)*h.container.clientHeight/2-(h.insets.top+h.container.clientHeight-h.insets.bottom)/2),
                          damping:h.controls.enableDamping, autoRotate:h.controls.autoRotate};
                    }""")
                    assert measure["error"] < 1 and not measure["damping"] and not measure["autoRotate"], measure
                    api("/api/mock", {"bends": [.1] * 5})
                    text("Покажите жест")
                    assert page.locator("#gestureProgress").is_hidden()
                    shot("state-unknown")
                    page.wait_for_timeout(700)
                    api("/api/mock", {"bends": [.1, .8, .8, .1, .1]})
                    text(name)
                    page.wait_for_function("document.querySelector('#gestureProgress').value > 0 && document.querySelector('#gestureProgress').value < 1")
                    shot("state-candidate")
                    page.wait_for_function("document.querySelector('#gestureReason').textContent.includes('Для повтора')")
                    triggered = state()["gesture_system"]["triggered"]
                    assert triggered and triggered != before
                    shot("state-triggered")
                    api("/api/mock", {"bends": [.1] * 5})
                    text("Покажите жест")
                    assert state()["gesture_system"]["triggered"] == triggered
                    tab("gestures")
                    api("/api/gestures/" + template["id"], {"audio": "missing.mp3"}, method="patch")
                    page.wait_for_function("document.querySelector('#gestureList').textContent.includes('Файл MP3 отсутствует')")
                    page.locator('[data-gesture-action="test"]').click()
                    page.wait_for_function("document.querySelector('#audioResult').textContent.includes('недоступно')")
                    shot("state-missing-mp3")
                    page.locator("[data-audio]").select_option("device-error.mp3")
                    page.wait_for_function("document.querySelector('#gestureList').textContent.includes('MP3 доступен')")
                    page.locator('[data-gesture-action="test"]').click()
                    page.wait_for_function("document.querySelector('#audioResult').textContent.includes('Тестовая ошибка аудиоустройства')")
                    assert "job_id" not in page.locator("#audioResult").inner_text()
                    gap = page.locator("#audioResult").evaluate("el => el.getBoundingClientRect().top - document.querySelector('#stopAudio').getBoundingClientRect().bottom")
                    assert 12 <= gap <= 16, gap
                    shot("state-audio-error")
                    shot("after-gestures", True)
                    # Переключения экранов не создают новые WS или API-вызовы сами по себе.
                    audio_results = state()["audio"]["results"]
                    connections = control["connections"]
                    for name_tab in ["signals", "settings", "gestures", "hand"] * 3:
                        tab(name_tab)
                    assert control["connections"] == connections
                    assert state()["gesture_system"]["triggered"] == triggered
                    assert state()["audio"]["results"] == audio_results
                    tab("gestures")
                    requests = []
                    page.on("request", lambda request: requests.append((request.method, request.url)))
                    page.locator("#stopAudio").click()
                    page.wait_for_timeout(150)
                    assert len([r for r in requests if r == ("POST", url + "/api/audio/stop")]) == 1
                    assert not state()["audio"]["queue"]
                    tab("signals")
                    assert not page.locator(".advanced").evaluate("el => el.open")
                    shot("after-signals", True)
                    page.locator(".advanced summary").click()
                    pipeline = state()["pipeline"]
                    other = page.locator('#pipePreset option').evaluate_all("opts => opts.find(o => o.value !== 'custom' && !o.selected).value")
                    page.once("dialog", lambda dialog: dialog.dismiss())
                    page.locator("#pipePreset").select_option(other)
                    assert state()["pipeline"] == pipeline
                    assert page.locator("#pipePreset").input_value() == pipeline["preset"]
                    page.locator("#pipePreset").select_option("custom")
                    assert state()["pipeline"] == pipeline
                    assert pipeline["title"] in page.locator("#pipeCurrent").inner_text()
                    page.once("dialog", lambda dialog: dialog.dismiss())
                    page.locator("#pipeApply").click()
                    assert state()["pipeline"] == pipeline
                    page.locator("#pipePreset").select_option("custom")
                    page.locator("#pipeJson").fill("{")
                    page.locator("#pipeApply").click()
                    assert "Ошибка JSON" in page.locator("#toast").inner_text()
                    assert state()["pipeline"] == pipeline
                    page.locator("#pipeJson").fill(json.dumps(pipeline["config"]))
                    page.once("dialog", lambda dialog: dialog.accept())
                    page.locator("#pipeApply").click()
                    page.wait_for_function("document.querySelector('#pipePreset').value === 'custom' && !document.querySelector('#pipeJson').classList.contains('bad')")
                    assert state()["pipeline"]["preset"] == "custom"
                    page.once("dialog", lambda dialog: dialog.accept())
                    page.locator("#pipePreset").select_option(pipeline["preset"])
                    page.wait_for_timeout(150)
                    page.once("dialog", lambda dialog: dialog.accept())
                    page.locator("#pipePreset").select_option(other)
                    page.wait_for_function("value => document.querySelector('#pipePreset').value === value", arg=other)
                    assert state()["pipeline"]["preset"] == other
                    tab("hand")
                    text("Переобучите жесты для текущих настроек")
                    shot("state-incompatible")
                    # Возвращаем обработку только на тестовой БД, через то же явное подтверждение.
                    tab("signals")
                    page.once("dialog", lambda dialog: dialog.accept())
                    page.locator("#pipePreset").select_option(pipeline["preset"])
                    page.wait_for_timeout(200)
                    tab("settings")
                    shot("after-settings", True)
                    # CSV-запись создаётся рядом с временной БД.
                    tab("signals")
                    page.locator("#recBtn").click()
                    page.wait_for_function("document.querySelector('#recBtn').classList.contains('rec-on')")
                    page.wait_for_timeout(250)
                    page.locator("#recBtn").click()
                    page.wait_for_function("!document.querySelector('#recBtn').classList.contains('rec-on')")
                    page.wait_for_timeout(200)
                    page.locator("#recList").select_option(index=1)
                    page.locator("#recPlay").click()
                    page.wait_for_function("document.querySelector('#srcName').textContent.includes('.csv')")
                    assert state()["source"]["kind"] == "replay"
                    api("/api/connect", {"kind": "mock", "n_channels": 5})
                    page.wait_for_timeout(2300)
                    tab("hand")

                    def uncalibrated(s):
                        for ch in s["channels"]:
                            ch["mode"] = "auto"
                        s["gesture_system"].update(candidate=None, reason="Нужна сохранённая калибровка всех каналов")
                    control["patch"] = uncalibrated
                    text("Откалибруйте все каналы в настройках")
                    shot("state-calibration-blocked")

                    def stale(s):
                        for ch in s["channels"]:
                            ch["stale"] = True
                    control["patch"] = stale
                    text("Нет свежих данных")
                    assert "ok" not in page.locator("#sideDot").get_attribute("class")
                    shot("state-stale")
                    control["patch"] = None
                    api("/api/disconnect")
                    text("Подключите источник")
                    shot("state-disconnected")
                    api("/api/connect", {"kind": "mock", "n_channels": 5})
                    page.wait_for_timeout(2300)
                    control["drop"] = True
                    text("Нет связи с приложением")
                    assert page.locator("#demoStrip").is_hidden()
                    shot("state-websocket-stalled")
                    control["drop"] = False
                    page.wait_for_function("!document.querySelector('#demoStrip').hidden")
                    page.evaluate("window.__qaWS.close()")
                    text("Нет связи с приложением")
                    shot("state-websocket-disconnected")
                    page.wait_for_function("!document.querySelector('#demoStrip').hidden")
                    # Контроль подписей и уникальности id на всех экранах.
                    accessibility = page.evaluate("""() => ({
                      duplicateIds: [...document.querySelectorAll('[id]')].map(e=>e.id).filter((id,i,a)=>a.indexOf(id)!==i),
                      unlabeled: [...document.querySelectorAll('input,select,textarea')].filter(e=>!e.labels?.length).map(e=>e.id)
                    })""")
                    assert not accessibility["duplicateIds"] and not accessibility["unlabeled"], accessibility
                    for name_tab in ["hand", "gestures", "signals", "settings"]:
                        tab(name_tab)
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), name_tab
                    results.append({"viewport": [width, height], "palm_anchor": measure, "audio_result_gap_px": gap,
                                    "single_stop_request": True, "tab_switch_no_events_or_audio": True,
                                    "processing_confirm_cancel": True, "custom_processing_apply_cancel_and_invalid_json": True,
                                    "csv_record_replay": True,
                                    "unique_ids_and_labels": accessibility,
                                    "fixture_states": ["calibration-blocked", "stale", "websocket-stalled", "websocket-disconnected"],
                                    "live_states": ["no-source", "demo", "no-gestures", "training", "candidate", "triggered", "unknown", "missing-mp3", "audio-error-with-stub-device", "disconnected", "incompatible"]})
                    context.close()
                # Реальный перезапуск процесса, та же временная база.
                expected = template["id"]
                process.terminate()
                process.wait(timeout=10)
                process = start()
                page = browser.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(url + "/#gestures")
                page.wait_for_function("document.querySelector('#gestureList strong')?.textContent === 'Указательный и средний'")
                saved = page.request.get(url + "/api/state").json()["gesture_system"]["templates"][0]
                assert saved["id"] == expected and saved["reference"] == template["reference"]
                browser.close()
        finally:
            process.terminate()
            process.wait(timeout=10)
        assert fingerprints() == original, "Содержимое пользовательских data/ или audio/ изменилось"
        report = {"results": results, "restart_preserves_template": True,
                  "user_data_files_unchanged": len(original), "page_errors": errors}
        (OUT / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        assert not errors, errors
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--db")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if args.serve:
        serve(args.db, args.port)
    else:
        main(args)
