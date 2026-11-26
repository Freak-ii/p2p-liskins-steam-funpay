"""Орбита: команды для deploy/orbita — говорят с работающим центром на этом сервере.

    python -m center.serverctl status              состояние центра, релиз, боты (JSON)
    python -m center.serverctl handover            центр уходит на новый релиз, боты работают (код 3 — не умеет)
    python -m center.serverctl wait-up [сек]        дождаться ответа нового центра
    python -m center.serverctl wait-rollout [сек]   дождаться конца обновления ботов (код 1 — откат)
    python -m center.serverctl snapshot <папка>    согласованные копии всех баз SQLite из папки данных

Папка данных — $CENTER_HOME. Только стандартная библиотека и PyYAML: работает и со старым центром.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml


def _home() -> Path:
    return Path(os.environ.get("CENTER_HOME") or "/var/lib/orbita")


def _center() -> tuple[str, str]:
    try:
        raw = yaml.safe_load((_home() / "center.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        raw = {}
    c = raw.get("center") or {}
    return f"http://127.0.0.1:{int(c.get('port') or 8800)}", str(c.get("token") or "")


def call(method: str, path: str, body: dict | None = None, timeout: float = 8) -> tuple[int, dict]:
    base, token = _center()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, OSError, ValueError):
        return 0, {}


def up() -> bool:
    code, d = call("GET", "/api/ping", timeout=3)
    return code == 200 and d.get("app") == "center"


def snapshot(dest: Path) -> int:
    """Копии баз через backup API SQLite: согласованы, даже пока боты пишут."""
    home, n = _home(), 0
    for src in sorted(home.rglob("*.sqlite*")):
        if src.suffix in (".sqlite3-wal", ".sqlite3-shm", ".sqlite-wal", ".sqlite-shm") or "backups" in src.parts:
            continue
        if src.name.endswith(("-wal", "-shm", "-journal")):
            continue
        out = dest / src.relative_to(home)
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=10)
            d = sqlite3.connect(out)
            with d:
                s.backup(d)
            s.close(), d.close()
            n += 1
        except sqlite3.Error as e:
            print(f"пропущено {src}: {e}", file=sys.stderr)
    return n


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    arg = argv[1] if len(argv) > 1 else ""
    if cmd == "status":
        code, d = call("GET", "/api/server/status")
        print(json.dumps(d if code == 200 else {"up": up(), "http": code}, ensure_ascii=False, indent=1))
        return 0 if code == 200 else 1
    if cmd == "handover":
        if not up():
            print("центр не запущен")
            return 2
        code, d = call("POST", "/api/server/handover", {})
        if code == 404:
            print("этот центр не умеет передавать эстафету (старая версия)")
            return 3
        print(d.get("message") or d.get("error") or code)
        if code != 200:
            return 1
        deadline = time.time() + 40               # дождаться, пока старый центр освободит порт
        while time.time() < deadline and up():
            time.sleep(0.3)
        return 0
    if cmd == "wait-up":
        deadline = time.time() + float(arg or 90)
        while time.time() < deadline:
            if up():
                return 0
            time.sleep(0.5)
        return 1
    if cmd == "wait-rollout":
        deadline = time.time() + float(arg or 900)
        st: dict = {}
        while time.time() < deadline:
            code, d = call("GET", "/api/server/status")
            st = d.get("rollout") or {}
            pending = (_home() / "server" / "rollout-pending.json").exists()   # новый центр ещё не взялся
            if code == 200 and not pending and not d.get("busy") and st.get("state") in ("done", "failed"):
                print(json.dumps(st, ensure_ascii=False, indent=1))
                return 0 if st["state"] == "done" else 1
            time.sleep(2)
        print("не дождались конца обновления: " + json.dumps(st, ensure_ascii=False))
        return 1
    if cmd == "snapshot":
        n = snapshot(Path(arg))
        print(f"копий баз: {n}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
