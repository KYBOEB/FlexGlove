"""Проверки статических файлов интерфейса (без браузера)."""
import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "flexglove" / "web"


def test_hidden_attribute_beats_class_display():
    """Элемент с атрибутом hidden должен скрываться, даже если у его класса задан display (баг .demo-strip)."""
    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert re.search(r"\[hidden\]\s*\{\s*display:\s*none\s*!important;?\s*\}", css)


def test_elements_toggled_by_hidden_exist_in_html():
    """Все id, которым app.js меняет .hidden, есть в index.html (опечатка = элемент никогда не скроется)."""
    js = (WEB / "app.js").read_text(encoding="utf-8")
    html = (WEB / "index.html").read_text(encoding="utf-8")
    ids = set(re.findall(r'\$\("#([\w-]+)"\)\.hidden', js))
    assert ids, "в app.js не найдено переключений hidden"
    missing = [i for i in ids if f'id="{i}"' not in html]
    assert not missing, missing


def test_es_modules_served_with_javascript_mime(tmp_path):
    """3D-рука — ES-модули: браузер грузит их только с JavaScript MIME-типом (на Windows реестр может дать text/plain)."""
    from fastapi.testclient import TestClient

    from flexglove.app import create_app
    with TestClient(create_app(tmp_path / "t.db")) as c:
        for path in ("/static/hand3d.js", "/static/vendor/three/three.module.min.js"):
            r = c.get(path)
            assert r.status_code == 200 and "javascript" in r.headers["content-type"]
        html = c.get("/").text
        assert '<script type="module" src="/static/hand3d.js">' in html
    assert "three.module.min.js" in (WEB / "hand3d.js").read_text(encoding="utf-8")
    assert "MIT" in (WEB / "vendor" / "three" / "LICENSE").read_text(encoding="utf-8")
