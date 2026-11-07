"""Этап 4б: сгиб пальца 0..1 → углы суставов 3D-руки (web/handpose.js) — в анатомических пределах.

Пределы проверяются всегда (по тексту модуля); сами функции — через Node.js, если он установлен
(на компьютере без Node проверка функций пропускается, на сервере CI Node есть)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "flexglove" / "web"
SRC = (WEB / "handpose.js").read_text(encoding="utf-8")


def limits():
    m = re.search(r"export const LIMITS = (\{.*?\n\});", SRC, re.S)
    js = m.group(1)
    js = re.sub(r"(\w+):", r'"\1":', js)
    return json.loads(re.sub(r",\s*}", "}", js))


def test_limits_match_anatomy():
    lim = limits()
    f, t = lim["finger"], lim["thumb"]
    assert 80 <= f["proximal"] <= 90
    assert f["intermediate"] <= 100
    assert f["distal"] == pytest.approx(f["intermediate"] * 2 / 3, abs=2)
    assert 0 < t["proximal"] < f["proximal"] and 0 < t["distal"] <= 80        # у большого пальца свои пределы
    assert 0 < t["metacarpalAcross"] <= 45 and 0 < t["metacarpalOppose"] <= 45


@pytest.mark.skipif(shutil.which("node") is None, reason="нет Node.js — функции handpose.js не проверить")
def test_joint_angles_monotonic_and_bounded():
    script = f"""
      import {{ jointAngles, LIMITS, bendWord }} from {json.dumps((WEB / 'handpose.js').as_uri())};
      const out = {{}};
      for (const f of ["thumb", "index", "pinky"]) {{
        out[f] = [];
        for (let i = -2; i <= 22; i++) out[f].push(jointAngles(f, i / 20));
      }}
      out.nan = jointAngles("index", NaN);
      out.limits = LIMITS;
      out.words = [0, 0.3, 0.9].map(bendWord);
      console.log(JSON.stringify(out));
    """
    res = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    lim = data["limits"]
    for finger in ("thumb", "index", "pinky"):
        rows = data[finger]
        table = lim["thumb"] if finger == "thumb" else lim["finger"]
        for key, top in table.items():
            seq = [r[key] for r in rows]
            assert seq[0] == 0 and seq[2] == 0                        # сгиб < 0 → как 0
            assert seq[-1] == pytest.approx(top) and seq[-3] == pytest.approx(top)   # сгиб > 1 → как 1
            assert all(b >= a - 1e-9 for a, b in zip(seq, seq[1:]))   # монотонно
            assert all(0 <= v <= top + 1e-9 for v in seq)
    assert all(v == 0 for v in data["nan"].values())
    assert data["words"] == ["разогнут", "полусогнут", "согнут"]
