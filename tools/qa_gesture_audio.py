"""Браузерная проверка на отдельном демосервере и тестовой БД, без реальных MP3.

Запуск сервера: cd host; python -m flexglove --demo --ui none --port 8775 --db ../data/qa-gesture-audio.db
Затем: python tools/qa_gesture_audio.py [--browser путь-к-chrome.exe]
Требуется отдельно установленный playwright и его Chromium.
"""
import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "analysis" / "img" / "gesture-audio"
URL = "http://127.0.0.1:8775"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**({"executable_path": args.browser} if args.browser else {}))
        errors, results = [], []
        for width, height in [(1366, 768), (1920, 1080)]:
            page = browser.new_page(viewport={"width": width, "height": height})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(URL)
            page.wait_for_function("document.querySelector('#sideCal').textContent.includes('известные')")
            # Удаляются только результаты предыдущего запуска этой QA-базы.
            assert page.request.get(URL + "/api/ping").json()["version"] == "0.7.0"
            for template in page.request.get(URL + "/api/state").json()["gesture_system"]["templates"]:
                page.request.delete(URL + "/api/gestures/" + template["id"])
            prior_event = page.request.get(URL + "/api/state").json()["gesture_system"]["triggered"]
            page.request.post(URL + "/api/mock", data={"bends": [.1, .8, .8, .1, .1]})
            page.wait_for_function("!document.querySelector('#sideImu').textContent.includes('держите')")
            page.wait_for_timeout(1000)  # реальная анимация/фильтр, не тест временных порогов
            name = f"Поза {width}"
            page.once("dialog", lambda dialog: dialog.accept(name))
            page.locator("#createGesture").click()
            page.wait_for_function("name => document.querySelector('#gestureList strong')?.textContent === name", arg=name)
            page.wait_for_function("name => document.querySelector('#gestureWords').textContent === name", arg=name)
            page.screenshot(path=str(OUT / f"hand-{width}.png"))
            template = page.request.get(URL + "/api/state").json()["gesture_system"]["templates"][0]
            assert len(template["reference"]) == 5
            assert template["audio"] is None
            page.locator('a[href="#settings"]').first.click()
            page.wait_for_function("document.querySelector('#gestureList').textContent.includes('Аудио не назначено')")
            assert page.locator("#bindGrid").count() == 0
            assert page.locator("input[data-role]").count() == 0
            page.locator('[data-gesture-action="test"]').click()
            page.wait_for_function("document.querySelector('#audioResult').textContent.includes('недоступно')")
            page.once("dialog", lambda dialog: dialog.accept("Переименован <тест>"))
            page.locator('[data-gesture-action="rename"]').click()
            page.wait_for_function("document.querySelector('#gestureList strong').textContent === 'Переименован <тест>'")
            assert page.locator("#gestureList тест").count() == 0
            page.locator("#gestureSettings").evaluate("el => window.scrollTo(0, el.getBoundingClientRect().top + scrollY - 72)")
            page.screenshot(path=str(OUT / f"settings-{width}.png"))
            # После обучения уже удержанная поза не вызывает событие.
            assert page.request.get(URL + "/api/state").json()["gesture_system"]["triggered"] == prior_event
            page.request.post(URL + "/api/mock", data={"bends": [.1] * 5})
            page.wait_for_timeout(1100)
            page.request.post(URL + "/api/mock", data={"bends": [.1, .8, .8, .1, .1]})
            page.wait_for_function("document.querySelector('#mainTarget').textContent.includes('Сработал')")
            state = page.request.get(URL + "/api/state").json()
            assert state["gesture_system"]["triggered"]["gesture_id"] == template["id"]
            assert state["audio"]["results"][0]["status"] == "unavailable"
            page.request.post(URL + "/api/mock", data={"roll": 70, "pitch": -40, "yaw": 90})
            page.wait_for_timeout(1200)
            assert page.request.get(URL + "/api/state").json()["gesture_system"]["triggered"] == state["gesture_system"]["triggered"]
            page.locator("#stopAudio").click()
            results.append({"viewport": [width, height], "reference_channels": len(template["reference"]),
                            "recognized": True, "missing_audio": True, "orientation_no_repeat": True})
            page.request.delete(URL + "/api/gestures/" + template["id"])
            # История события сохраняется для наблюдения в API; следующий проход — новый источник,
            # но sequence не сбрасывается, поэтому проверяем отсутствие новых событий отдельно.
            page.close()
        browser.close()
        (OUT / "results.json").write_text(json.dumps({"results": results, "page_errors": errors},
                                                    ensure_ascii=False, indent=2), encoding="utf-8")
        assert not errors, errors
        print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
