"""Проверка собранной Windows-версии: центр в демо-режиме стартует, Python-боты сборки переходят
в состояние running, центр корректно выключается кнопкой «Выключить» (/api/quit) вместе с ботами.

    python packaging/windows/smoke_test.py <папка программы> [--python <интерпретатор>] [--port 8899]

По умолчанию берётся <папка>/runtime/python.exe — то есть ровно тот Python, что поедет к пользователю.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


def get(url: str, token: str = "", timeout: float = 3) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("base", type=Path)
    ap.add_argument("--python")
    ap.add_argument("--windowed", action="store_true", help="запуск через pythonw.exe, как из ярлыка установщика")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--timeout", type=float, default=120)
    args = ap.parse_args()

    base = args.base.resolve()
    py = args.python or str(base / "runtime" / ("pythonw.exe" if args.windowed else "python.exe"))
    info = json.loads((base / "build-info.json").read_text(encoding="utf-8"))
    expected = [b for b in info["bots"] if b != "lisbot"]          # LisBot в демо ждёт настройки — это норма
    home = Path(tempfile.mkdtemp(prefix="center-smoke-"))
    env = {**os.environ, "CENTER_HOME": str(home)}
    log = open(home / "launcher.log", "w", encoding="utf-8")
    proc = subprocess.Popen([py, str(base / "app" / "launch.pyw"), "--demo", "--no-browser",
                             "--port", str(args.port)], cwd=base / "app", env=env, stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{args.port}"
    deadline = time.time() + args.timeout
    states: dict = {}
    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                raise SystemExit(f"центр завершился с кодом {proc.returncode}")
            try:
                get(f"{url}/api/ping")
                token = (home / "demo" / "center.yaml").read_text(encoding="utf-8").split("token:")[1].split()[0]
                ov = get(f"{url}/api/overview", token)
                states = {b.get("name"): b.get("state") for b in ov.get("bots", [])}
                if expected and all(states.get(b) == "running" for b in expected):
                    break
            except (OSError, IndexError, ValueError):
                pass
            time.sleep(2)
        else:
            raise SystemExit(f"боты не запустились за {args.timeout:.0f} с: {states or 'центр не ответил'}")
        print("OK: центр отвечает, боты:", states, flush=True)
        req = urllib.request.Request(f"{url}/api/quit", method="POST", headers={"Authorization": f"Bearer {token}"})
        urllib.request.urlopen(req, timeout=5).read()
        proc.wait(60)
        print("OK: центр выключился с кодом", proc.returncode, flush=True)
    finally:
        if proc.poll() is None:
            proc.kill()
        log.close()
        print("---- журнал запуска ----")
        print((home / "launcher.log").read_text(encoding="utf-8", errors="replace")[-4000:])
        for f in sorted((home / "demo" / "logs").glob("*.log")) if (home / "demo" / "logs").exists() else []:
            print(f"---- {f.name} ----")
            print(f.read_text(encoding="utf-8", errors="replace")[-2000:])


if __name__ == "__main__":
    sys.exit(main())
