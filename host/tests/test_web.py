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
