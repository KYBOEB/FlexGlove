"""FlexGlove — запуск двойным щелчком (Windows; на macOS/Linux: python3 FlexGlove.pyw).

Первый запуск (1–2 минуты): создаёт окружение .venv, ставит пакеты из папки wheels/
(без интернета; если папки нет — из интернета), создаёт ярлыки «FlexGlove» на рабочем столе
и в меню «Пуск». Дальше приложение запускается ярлыком: без консоли, интерфейс открывается
в браузере по умолчанию, значок — в области уведомлений рядом с часами.

Файл использует только стандартную библиотеку Python 3.10+.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOST = ROOT / "host"
VENV = ROOT / ".venv"
REQ = ROOT / "requirements.txt"
WHEELS = ROOT / "wheels"
MARK = VENV / "flexglove_ready.txt"
ICON = HOST / "flexglove" / "web" / "icon.ico"
LOG = ROOT / "data" / "setup.log"
WIN = sys.platform == "win32"
NO_WINDOW = 0x08000000 if WIN else 0  # CREATE_NO_WINDOW: дочерние процессы без чёрных окон


def venv_python(gui: bool = False) -> Path:
    if WIN:
        return VENV / "Scripts" / ("pythonw.exe" if gui else "python.exe")
    return VENV / "bin" / "python"


def base_python() -> str:
    exe = Path(sys.executable)
    if WIN and exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return str(exe)


def req_hash() -> str:
    return hashlib.sha256(REQ.read_bytes()).hexdigest()[:16]


def needs_setup() -> bool:
    return not (MARK.exists() and venv_python().exists() and MARK.read_text().strip() == req_hash())


def run(cmd: list[str], log) -> int:
    log.write("\n> " + " ".join(cmd) + "\n")
    log.flush()
    return subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, creationflags=NO_WINDOW).returncode


def setup(status) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("w", encoding="utf-8") as log:
        if not venv_python().exists():
            status("Создаю окружение Python…")
            if run([base_python(), "-m", "venv", str(VENV)], log):
                raise RuntimeError("не удалось создать окружение .venv")
        status("Устанавливаю пакеты (без интернета)…")
        pip = [str(venv_python()), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(REQ)]
        ok = WHEELS.exists() and run(pip + ["--no-index", "--find-links", str(WHEELS)], log) == 0
        if not ok:
            status("Скачиваю пакеты из интернета…")
            if run(pip, log):
                raise RuntimeError("не удалось установить пакеты")
        if WIN:
            status("Создаю ярлыки…")
            create_shortcuts(log)
    MARK.write_text(req_hash())


def create_shortcuts(log) -> None:
    """Ярлык «FlexGlove» на рабочем столе и в меню «Пуск» (через PowerShell, без доп. пакетов)."""
    q = lambda p: str(p).replace("'", "''")  # noqa: E731 - экранирование для строки PowerShell
    target, args, workdir, icon = q(venv_python(gui=True)), "-m flexglove.desktop", q(HOST), q(ICON)
    ps = (
        "$w = New-Object -ComObject WScript.Shell;"
        "foreach ($d in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {"
        "  $s = $w.CreateShortcut((Join-Path $d 'FlexGlove.lnk'));"
        f"  $s.TargetPath = '{target}'; $s.Arguments = '{args}'; $s.WorkingDirectory = '{workdir}';"
        f"  $s.IconLocation = '{icon}'; $s.Description = 'FlexGlove — пульт перчатки'; $s.Save() }}"
    )
    run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], log)


def launch() -> None:
    subprocess.Popen([str(venv_python(gui=True)), "-m", "flexglove.desktop", *sys.argv[1:]],
                     cwd=HOST, creationflags=NO_WINDOW)


def setup_with_window() -> None:
    """Окно с прогрессом на время первой установки. Если tkinter нет — ставим молча."""
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError:
        setup(lambda _msg: None)
        return

    try:
        root = tk.Tk()
    except tk.TclError:  # нет графического окружения
        setup(lambda _msg: None)
        return
    root.title("FlexGlove — первый запуск")
    root.geometry("440x150")
    root.resizable(False, False)
    tk.Label(root, text="FlexGlove готовится к первому запуску", font=("Segoe UI", 12, "bold")).pack(pady=(18, 6))
    msg = tk.StringVar(value="Подготовка…")
    tk.Label(root, textvariable=msg, font=("Segoe UI", 10)).pack()
    bar = ttk.Progressbar(root, mode="indeterminate", length=360)
    bar.pack(pady=14)
    bar.start(12)
    result: dict[str, Exception | None] = {"error": None}

    def work():
        try:
            setup(lambda m: root.after(0, msg.set, m))
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc
        root.after(0, root.destroy)

    threading.Thread(target=work, daemon=True).start()
    root.mainloop()
    if result["error"]:
        err = tk.Tk()
        err.withdraw()
        messagebox.showerror("FlexGlove", f"Установка не удалась: {result['error']}.\n\nПодробности: {LOG}\n"
                             "Проверьте интернет или удалите папку .venv и запустите снова.")
        err.destroy()
        raise SystemExit(1)


def main() -> None:
    if sys.version_info < (3, 10):
        try:
            import tkinter.messagebox as mb
            mb.showerror("FlexGlove", "Нужен Python 3.10 или новее: https://www.python.org/downloads/")
        finally:
            raise SystemExit(1)
    if needs_setup():
        setup_with_window()
    launch()


if __name__ == "__main__":
    main()
