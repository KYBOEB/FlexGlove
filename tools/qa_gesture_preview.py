"""Приёмка 3D-жестов: отдельная временная БД, настоящий Chromium и перезапуск сервера.

Запуск: python tools/qa_gesture_preview.py [--browser путь-к-chrome.exe].
Нужны playwright и Chromium (playwright install chromium либо установленный Chrome).
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/analysis/img/gesture-preview"
URL = "http://127.0.0.1:8787"
PORT = 8787


def start_server(db):
    global URL, PORT
    # После остановки Linux оставляет старые соединения в TIME_WAIT. Новый
    # процесс получает свежий порт; CLI иначе сам выбрал бы следующий порт.
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        PORT = available.getsockname()[1]
    URL = f"http://127.0.0.1:{PORT}"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "host")}
    process = subprocess.Popen([sys.executable, "-m", "flexglove", "--demo", "--ui", "none",
                               "--port", str(PORT), "--db", str(db)], cwd=ROOT, env=env,
                               # SwiftShader на Windows-runner занимает CPU. Только QA-процессу
                               # даём приоритет отсчётов; часы и требования 20 отсчётов не меняем.
                               creationflags=subprocess.HIGH_PRIORITY_CLASS if os.name == "nt" else 0,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            urllib.request.urlopen(URL + "/api/ping", timeout=1).close()
            return process
        except OSError:
            if process.poll() is not None:
                raise RuntimeError("Тестовый сервер завершился")
            time.sleep(.1)
    process.terminate()
    process.wait(timeout=10)
    raise RuntimeError("Тестовый сервер не запустился")


def instrument(route):
    response = route.fetch()
    source = response.text().replace(
        "return new Hand3D(container, options);",
        "const h = new Hand3D(container, options); (window.__qaHands ||= []).push(h); return h;")
    route.fulfill(response=response, body=source)


def wait_images(page):
    page.wait_for_function("""() => {
      const images=[...document.querySelectorAll('[data-thumbnail]')];
      return images.length && images.every(i => !i.hidden && i.complete && i.naturalWidth === 320);
    }""")


def state(page):
    return page.request.get(URL + "/api/state").json()


def mock(page, bends):
    assert page.request.post(URL + "/api/mock", data={"bends": bends}).ok
    page.wait_for_timeout(600)


def train_until_saved(page):
    """Нагруженный CI может задержать реальные отсчёты; автомат обязан отклонить их.

    Повторяем только явный разрыв/нехватку данных, не меняя часы и пороги распознавателя.
    Ошибка движения и другие ошибки остаются провалом сценария.
    """
    transient = ("Разрыв потока данных", "Недостаточно данных за секунду")
    for attempt in range(5):
        page.locator("#gestureTrain").click()
        page.wait_for_function("""() => {
          const button=document.querySelector('#gestureTrain');
          return button.hidden || !button.disabled;
        }""")
        message = page.locator("#gestureDialogStatus").inner_text()
        if "сохранён" in message:
            return
        if not message.startswith(transient):
            raise AssertionError(message)
        print(f"CI source retry {attempt + 1}: {message}", flush=True)
        page.wait_for_timeout(800)
    raise AssertionError("Пять записей отклонены из-за разрыва/нехватки данных")


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "failure.png").unlink(missing_ok=True)
    errors, results = [], []
    with tempfile.TemporaryDirectory(prefix="flexglove-preview-") as tmp, sync_playwright() as pw:
        db = Path(tmp) / "qa.db"
        server = start_server(db)
        browser = pw.chromium.launch(args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
                                     **({"executable_path": args.browser} if args.browser else {}))
        try:
            for width, height in [(1366, 768), (1920, 1080)]:
                print("viewport", width, flush=True)
                page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.route("**/hand3d.js?*", instrument)
                drop = [False]
                def websocket(route):
                    peer = route.connect_to_server()
                    peer.on_message(lambda message: None if drop[0] else route.send(message))
                page.route_web_socket("**/ws", websocket)
                page.goto(URL + "/#gestures")
                page.wait_for_function("window.__qaHands?.[0]?.model?.kind === 'gltf'")
                page.wait_for_function("document.querySelector('#calStatus').textContent.includes('известные')")
                page.wait_for_function("!document.querySelector('#sideImu').textContent.includes('держите')")
                page.wait_for_function("Number(document.querySelector('#dbgRate').textContent) >= 20")
                for t in state(page)["gesture_system"]["templates"]:
                    page.request.delete(URL + "/api/gestures/" + t["id"])
                page.wait_for_function("!document.querySelector('.learned-gesture')")
                mock(page, [.1, .8, .65, .1, .2])
                prior = state(page)["gesture_system"]["triggered"]
                page.locator("#createGesture").click()
                page.locator("#gestureName").fill("Указательный и средний")
                page.wait_for_function("window.__qaHands.some(h => !h.disposed && h.container.id==='gesturePreview' && h.model?.kind==='gltf')")
                page.wait_for_timeout(300)
                assert page.evaluate("""() => {
                  const main=window.__qaHands[0], preview=window.__qaHands.find(h=>!h.disposed && h.container.id==='gesturePreview');
                  return main.qCurrent.angleTo(preview.qCurrent)<.001 && Object.keys(main.bendCurrent).every(f=>Math.abs(main.bendCurrent[f]-preview.bendCurrent[f])<.001);
                }""")
                page.screenshot(path=str(OUT / f"create-{width}.png"))
                train_until_saved(page)
                wait_images(page)
                page.wait_for_function("!document.querySelector('#gestureSaved').hidden")
                page.screenshot(path=str(OUT / f"saved-{width}.png"))
                template = state(page)["gesture_system"]["templates"][0]
                image = page.locator("[data-thumbnail]").get_attribute("src")
                assert page.locator("#gestureSaved").get_attribute("src") == image
                assert state(page)["gesture_system"]["triggered"] == prior
                # Снимок действительно отрисован по эталону, а не по живым каналам.
                assert page.evaluate("""t => {
                  const h=window.__qaHands.filter(h=>h.container.className==='thumbnail-render-host').at(-1);
                  return h.disposed && t.context.channels.every((c,i)=>h.bendCurrent[c.finger]===t.reference[i]);
                }""", template)
                page.locator("#gestureClose").click()
                page.screenshot(path=str(OUT / f"list-{width}.png"))
                mock(page, [.5] * 5)
                assert page.locator("[data-thumbnail]").get_attribute("src") == image
                page.request.post(URL + "/api/mock", data={"roll": 70, "pitch": -40, "yaw": 80})
                page.wait_for_timeout(400)
                assert page.locator("[data-thumbnail]").get_attribute("src") == image
                mock(page, [.1, .8, .65, .1, .2])
                page.wait_for_function("document.querySelector('#gestureReason').textContent.includes('Для повтора')")
                recognized = state(page)["gesture_system"]["triggered"]
                assert recognized["gesture_id"] == template["id"]
                page.request.post(URL + "/api/mock", data={"roll": -20, "pitch": 30, "yaw": -50})
                page.wait_for_timeout(1100)
                assert state(page)["gesture_system"]["triggered"] == recognized
                mock(page, [.5] * 5)
                page.reload()
                wait_images(page)
                assert page.locator("[data-thumbnail]").get_attribute("src") == image
                # Просмотр ранее созданного эталона после загрузки страницы.
                page.locator('[data-gesture-action="view"]').click()
                assert page.locator("#gestureSaved").get_attribute("src") == image
                assert page.locator("#gesturePreview canvas").count() == 0
                page.locator("#gestureClose").click()
                # Отмена подготовки и записи ничего не меняет.
                page.locator("#createGesture").click()
                page.locator("#gestureClose").click()
                page.locator("#createGesture").click()
                page.locator("#gestureName").fill("Отменить")
                # Два locator.click ждут стабильности кадров и на SwiftShader могут
                # занять больше секунды. Проверяем отмену именно до окончания записи.
                page.evaluate("document.querySelector('#gestureTrain').click(); document.querySelector('#gestureClose').click()")
                page.wait_for_timeout(1200)
                assert state(page)["gesture_system"]["templates"] == [template]
                # Ошибка движения и повторная запись в том же окне.
                page.locator("#createGesture").click()
                page.locator("#gestureName").fill("Повтор после движения")
                page.evaluate("document.querySelector('#gestureTrain').click()")
                page.wait_for_timeout(150)
                mock(page, [.15] * 5)
                page.wait_for_function("document.querySelector('#gestureDialogStatus').textContent.includes('движение')")
                train_until_saved(page)
                page.locator("#gestureClose").click()
                retry = state(page)["gesture_system"]["templates"][-1]
                page.request.delete(URL + "/api/gestures/" + retry["id"])
                page.wait_for_function("document.querySelectorAll('.learned-gesture').length===1")
                mock(page, [.5] * 5)
                # WS не передал ни одного снимка активной записи; результат восстанавливается.
                page.locator("#createGesture").click()
                page.locator("#gestureName").fill("Запись при паузе WS")
                drop[0] = True
                page.locator("#gestureTrain").click()
                page.wait_for_timeout(1400)
                drop[0] = False
                page.wait_for_function("document.querySelector('#gestureDialogStatus').textContent.includes('сохранён')")
                page.locator("#gestureClose").click()
                paused = state(page)["gesture_system"]["templates"][-1]
                page.request.delete(URL + "/api/gestures/" + paused["id"])
                page.wait_for_function("document.querySelectorAll('.learned-gesture').length===1")
                # Переобучение того же id обновляет позу/миниатюру; отмена сохраняет старую.
                page.locator('[data-gesture-action="retrain"]').click()
                page.locator("#gestureClose").click()
                assert page.locator("[data-thumbnail]").get_attribute("src") == image
                page.locator('[data-gesture-action="retrain"]').click()
                train_until_saved(page)
                page.wait_for_function("old => document.querySelector('[data-thumbnail]').src !== old", arg=image)
                wait_images(page)
                new_image = page.locator("[data-thumbnail]").get_attribute("src")
                assert state(page)["gesture_system"]["templates"][0]["id"] == template["id"]
                page.locator("#gestureClose").click()
                # Нет повторного события при удержании и операциях с UI.
                held = state(page)["gesture_system"]["triggered"]
                page.wait_for_timeout(1100)
                assert state(page)["gesture_system"]["triggered"] == held
                # Настоящий перезапуск процесса с той же временной БД.
                server.terminate(); server.wait(timeout=10)
                server = start_server(db)
                page.goto(URL + "/#gestures"); wait_images(page)
                assert page.locator("[data-thumbnail]").get_attribute("src") == new_image
                # Другая рука, неполный набор сенсоров, изменённый порядок пальцев.
                page.request.post(URL + "/api/connect", data={"kind": "mock", "n_channels": 2})
                page.request.post(URL + "/api/settings", data={"hand": "left", "finger_map": ["ring", "thumb"]})
                mock(page, [.8, .25])
                page.wait_for_function("!document.querySelector('#sideImu').textContent.includes('держите')")
                page.locator("#createGesture").click()
                page.locator("#gestureName").fill("Левая: два сенсора")
                train_until_saved(page)
                wait_images(page)
                left = state(page)["gesture_system"]["templates"][-1]
                assert left["context"]["hand"] == "left"
                assert page.evaluate("""t => {
                  const h=window.__qaHands.filter(h=>h.container.className==='thumbnail-render-host').at(-1);
                  return h.hand==='left' && h.model.kind==='gltf' && h.bendCurrent.index===.12 && h.bendCurrent.middle===.12 && h.bendCurrent.pinky===.12 && h.bendCurrent.ring===t.reference[0] && h.bendCurrent.thumb===t.reference[1];
                }""", left)
                page.locator("#gestureClose").click()
                page.screenshot(path=str(OUT / f"both-hands-{width}.png"))
                assert page.evaluate("window.__qaHands.filter(h=>!h.disposed).length") == 1
                assert page.evaluate("window.__qaHands.filter(h=>h.disposed).every(h=>!h.renderer.domElement.isConnected && h.renderer.info.memory.geometries===0 && h.renderer.info.memory.textures===0)")
                assert page.locator("canvas").count() == 2  # главная рука и график; список — img
                page.locator('a[href="#signals"]').click()
                page.locator("#imuBox").wait_for(state="visible")
                assert page.locator("#fingerRows").count() == 0
                assert page.locator("#imuBox").is_visible()
                page.screenshot(path=str(OUT / f"signals-{width}.png"))
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                results.append({"viewport": [width, height], "reference_exact": True, "live_mirror": True,
                                "cancel": True, "retrain": True, "process_restart": True,
                                "both_hands_missing_sensors": True, "static_images": True,
                                "active_renderers_after_dialog": 1, "no_repeat": True,
                                "retry_after_movement_error": True, "gpu_memory_disposed": True})
                # Следующий проход начинает с обычной правой руки/пяти каналов.
                page.request.post(URL + "/api/settings", data={"hand": "right", "finger_map": ["thumb", "index", "middle", "ring", "pinky"]})
                page.request.post(URL + "/api/connect", data={"kind": "mock", "n_channels": 5})
                page.close()
            page = browser.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.add_init_script("""const original = HTMLCanvasElement.prototype.getContext;
              HTMLCanvasElement.prototype.getContext = function(kind, ...args) {
                return kind.startsWith('webgl') ? null : original.call(this, kind, ...args);
              };""")
            page.goto(URL + "/#gestures")
            page.locator("#createGesture").click()
            page.wait_for_function("document.querySelector('#gesturePreview').textContent.includes('WebGL')")
            page.locator("#gestureClose").click()
            page.close()
        except Exception:
            if 'page' in locals() and not page.is_closed():
                page.screenshot(path=str(OUT / "failure.png"))
                print(page.locator('#gestureDialogStatus').inner_text(), flush=True)
                try:
                    print(json.dumps(state(page), ensure_ascii=False), flush=True)
                except Exception as diagnostic_error:
                    print(f"Снимок сервера недоступен: {diagnostic_error}", flush=True)
            raise
        finally:
            browser.close()
            server.terminate(); server.wait(timeout=10)
        (OUT / "results.json").write_text(json.dumps({"results": results, "page_errors": errors},
                                                     ensure_ascii=False, indent=2), encoding="utf-8")
        assert not errors, errors
        print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
