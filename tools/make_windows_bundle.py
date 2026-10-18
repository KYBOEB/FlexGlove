"""Собирает FlexGlove-Windows.zip: исходники + папка wheels/ с пакетами под Windows x64
для Python 3.10–3.14 (чтобы первый запуск работал без интернета).

Запуск (на любой ОС, нужен интернет и пакет uv):  python tools/make_windows_bundle.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY_VERSIONS = ["3.10", "3.11", "3.12", "3.13", "3.14"]
EXCLUDE_DIRS = {".venv", "__pycache__", ".pytest_cache", "wheels", "data", "dist"}


def run(*cmd: str) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True)


def main() -> None:
    wheels = ROOT / "wheels"
    wheels.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for v in PY_VERSIONS:
            req = Path(tmp) / f"req-{v}.txt"
            # pip download не умеет вычислять маркеры для чужой ОС, поэтому сначала резолвим через uv
            run(sys.executable, "-m", "uv", "pip", "compile", str(ROOT / "requirements.txt"),
                "--python-platform", "x86_64-pc-windows-msvc", "--python-version", v,
                "--no-header", "--no-annotate", "-q", "-o", str(req))
            run(sys.executable, "-m", "pip", "download", "-q", "--no-deps", "--only-binary=:all:",
                "--platform", "win_amd64", "--python-version", v, "--implementation", "cp",
                "-d", str(wheels), "-r", str(req))

    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    out = dist / "FlexGlove-Windows.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(ROOT.rglob("*")):
            rel = path.relative_to(ROOT)
            if path.is_dir() or (set(rel.parts) & (EXCLUDE_DIRS - {"wheels"})):
                continue
            if rel.parts[0] == "data" and path.name != ".gitkeep":
                continue
            z.write(path, Path("FlexGlove") / rel)
    print(f"Готово: {out} ({out.stat().st_size // 1024 // 1024} МБ)")


if __name__ == "__main__":
    main()
