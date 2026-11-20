"""Портативная сборка центра для Windows x64 (собирается и на Linux, и на Windows):

    python packaging/build_center_windows.py

Результат: dist/Center-Windows-x64-portable.zip — распаковать и запустить «Центр.vbs».
Внутри: официальный CPython для Windows (пакет python с nuget.org) и Node.js для Windows (nodejs.org),
Python-колёса win_amd64, код центра и обоих ботов, зависимости LisBot (better-sqlite3 с готовым
win32-x64 модулем). Устанавливать Python и Node.js у пользователя не нужно.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "center-win"
OUT_DIR = BUILD / "Center"
DIST = ROOT / "dist"
PY_MINOR = "3.12"
NODE_MAJOR = "22"
# Центр + MannBot (как requirements.txt, без pytest) и значок в трее
REQUIREMENTS = ["aiohttp>=3.9", "beautifulsoup4>=4.12", "lxml>=5.0", "PyYAML>=6.0", "Jinja2>=3.1",
                "cryptography>=42", "pystray>=0.19", "Pillow>=10"]
for extra in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines() + \
        (ROOT / "bots" / "mannbot" / "requirements.txt").read_text(encoding="utf-8").splitlines():
    # пакеты, добавленные в requirements другими задачами (например, anthropic для ИИ-агента), тоже попадут в сборку
    name = extra.split("#")[0].strip()
    base = name.split(">")[0].split("=")[0].split("<")[0].strip().lower()
    if name and not name.startswith("-") and base != "pytest" and \
            base not in {r.split(">")[0].lower() for r in REQUIREMENTS}:
        REQUIREMENTS.append(name)

ENV = r"""set "CENTER_HOME=%~dp0data"
set "CENTER_NODE=%~dp0runtime\node\node.exe"
set "PYTHONPATH=%~dp0app"
"""

BAT_CONSOLE = r"""@echo off
chcp 65001 >nul
title Центр перепродажи
cd /d "%~dp0app"
""" + ENV + r"""
"%~dp0runtime\python\python.exe" -m center %*
if errorlevel 1 pause
"""

BAT_DEMO = r"""@echo off
chcp 65001 >nul
title Центр перепродажи DEMO
cd /d "%~dp0app"
""" + ENV + r"""
"%~dp0runtime\python\python.exe" -m center --demo %*
"""

# Основной запуск: без окна консоли, значок в трее (меню: панель, папка данных, выход)
VBS_TRAY = r"""' Центр перепродажи: работает в фоне, значок у часов. Выход — правый клик по значку.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
d = fso.GetParentFolderName(WScript.ScriptFullName)
Set env = sh.Environment("PROCESS")
env("CENTER_HOME") = d & "\data"
env("CENTER_NODE") = d & "\runtime\node\node.exe"
env("PYTHONPATH") = d & "\app"
sh.CurrentDirectory = d & "\app"
q = Chr(34)
sh.Run q & d & "\runtime\python\pythonw.exe" & q & " -m center --tray", 0, False
"""

README = """Центр перепродажи: LisBot (LIS-SKINS -> Steam) + MannBot (FunPay)
====================================================================

ЗАПУСК
  1. Распакуйте архив в папку без кириллицы и пробелов, например C:\\Center.
  2. Дважды щёлкните «Центр.vbs». У часов появится значок центра, откроется панель в браузере.
     Правый клик по значку: «Открыть панель», «Папка данных», «Выход (остановить ботов)».
     Жёлтый значок — одному из ботов нужна настройка или он упал.
  3. В панели центра: «Подключения» у LisBot и «Настройки» у MannBot.
  4. Оба бота стартуют в тестовом режиме и ничего не покупают и не продают.

  Вариант с окном журнала: «Центр (консоль).bat». Посмотреть без аккаунтов: «Центр DEMO.bat».

ГДЕ ДАННЫЕ
  Папка data\\ — настройки, базы ботов, журналы. Ключи и пароли лежат в data\\secrets.dat
  в зашифрованном виде (Windows DPAPI): расшифровать их может только этот пользователь Windows
  на этом компьютере. При переносе на другой ПК ключи придётся ввести заново.

ВАЖНО
  • Выход через значок или кнопку «Выход» в панели: центр сначала просит ботов завершиться
    самим (они дописывают базы), и только потом закрывает процессы.
  • Windows Defender/SmartScreen может предупредить о неизвестном издателе — это нормально
    для самостоятельно собранных программ.
  • Панель доступна только с этого компьютера (127.0.0.1). Для работы 24/7 — Docker на VPS.
"""


def run(cmd: list[str], **kw) -> None:
    print("$", " ".join(cmd))
    subprocess.run(cmd, check=True, **kw)


def fetch(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    print("↓", url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    with urllib.request.urlopen(url, timeout=300) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    os.replace(tmp, dest)
    return dest


def latest_python() -> str:
    with urllib.request.urlopen("https://api.nuget.org/v3-flatcontainer/python/index.json", timeout=60) as r:
        versions = json.load(r)["versions"]
    stable = [v for v in versions if v.startswith(PY_MINOR + ".") and v.replace(".", "").isdigit()]
    return sorted(stable, key=lambda v: tuple(map(int, v.split("."))))[-1]


def latest_node() -> str:
    with urllib.request.urlopen("https://nodejs.org/dist/index.json", timeout=60) as r:
        for rel in json.load(r):                          # список отсортирован от новых к старым
            if rel["version"].startswith(f"v{NODE_MAJOR}.") and "win-x64-zip" in rel["files"]:
                return rel["version"]
    raise RuntimeError(f"не найден Node.js {NODE_MAJOR} для win-x64")


def copy_tree(src: Path, dst: Path, *ignore: str) -> None:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", *ignore))


def main() -> None:
    shutil.rmtree(OUT_DIR, ignore_errors=True)
    OUT_DIR.mkdir(parents=True)
    cache = BUILD / "cache"

    # 1. Python для Windows
    ver = latest_python()
    nupkg = fetch(f"https://api.nuget.org/v3-flatcontainer/python/{ver}/python.{ver}.nupkg", cache / f"python.{ver}.nupkg")
    py = OUT_DIR / "runtime" / "python"
    with zipfile.ZipFile(nupkg) as z:
        for name in z.namelist():
            if name.startswith("tools/") and not name.endswith("/"):
                target = py / name[len("tools/"):]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(name))
    for junk in ("Lib/test", "Lib/idlelib", "Lib/turtledemo", "Lib/ensurepip"):
        shutil.rmtree(py / junk, ignore_errors=True)
    assert (py / "python.exe").exists() and (py / "pythonw.exe").exists(), "в пакете nuget нет python.exe/pythonw.exe"

    # 2. Python-зависимости: готовые колёса win_amd64
    wheels = cache / f"wheels-{PY_MINOR}"
    shutil.rmtree(wheels, ignore_errors=True)
    wheels.mkdir(parents=True)
    run([sys.executable, "-m", "pip", "download", "--only-binary=:all:", "--platform", "win_amd64",
         "--python-version", PY_MINOR, "--implementation", "cp", "-d", str(wheels), *REQUIREMENTS])
    site = py / "Lib" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    for whl in sorted(wheels.glob("*.whl")):
        with zipfile.ZipFile(whl) as z:
            for name in z.namelist():
                if ".data/" in name.split("/", 1)[0] + "/":
                    continue
                z.extract(name, site)

    # 3. Node.js для Windows: нужен только node.exe (LisBot запускается центром, npm не нужен)
    nver = latest_node()
    nzip = fetch(f"https://nodejs.org/dist/{nver}/node-{nver}-win-x64.zip", cache / f"node-{nver}-win-x64.zip")
    node_dir = OUT_DIR / "runtime" / "node"
    node_dir.mkdir(parents=True)
    with zipfile.ZipFile(nzip) as z:
        for name in z.namelist():
            base = name.split("/", 1)[-1]
            if base in ("node.exe", "LICENSE"):
                (node_dir / base).write_bytes(z.read(name))
    assert (node_dir / "node.exe").exists(), "в архиве Node.js нет node.exe"

    # 4. Код центра и ботов
    app = OUT_DIR / "app"
    copy_tree(ROOT / "center", app / "center")
    shutil.copy2(ROOT / "requirements.txt", app / "requirements.txt")
    mb = ROOT / "bots" / "mannbot"
    copy_tree(mb / "mannbot", app / "bots" / "mannbot" / "mannbot")
    for f in ("MannBot.py", "config.example.yaml", "requirements.txt", "README.md"):
        shutil.copy2(mb / f, app / "bots" / "mannbot" / f)
    lb = ROOT / "bots" / "lisbot"
    lis = app / "bots" / "lisbot"
    lis.mkdir(parents=True)
    copy_tree(lb / "src", lis / "src")
    for f in ("package.json", "package-lock.json", "config.example.json", "README.md"):
        shutil.copy2(lb / f, lis / f)
    # зависимости LisBot: без скриптов установки, better-sqlite3 — только готовый модуль win32-x64 (N-API)
    run(["npm", "ci", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=lis,
        shell=sys.platform == "win32")
    bs = lis / "node_modules" / "better-sqlite3"
    for d in ("deps", "src", "docs", "build"):
        shutil.rmtree(bs / d, ignore_errors=True)
    for f in (bs / "prebuilds").iterdir():
        if f.name != "win32-x64.node":
            f.unlink()
    assert (bs / "prebuilds" / "win32-x64.node").exists(), "нет prebuilds/win32-x64.node у better-sqlite3"
    (OUT_DIR / "data").mkdir()

    # 5. Ярлыки запуска, значок и инструкция (CRLF для Windows)
    def write(name: str, text: str, encoding: str = "utf-8") -> None:
        (OUT_DIR / name).write_bytes(text.replace("\r\n", "\n").replace("\n", "\r\n").encode(encoding))
    write("Центр.vbs", VBS_TRAY, "utf-16")
    write("Центр (консоль).bat", BAT_CONSOLE)
    write("Центр DEMO.bat", BAT_DEMO)
    write("ПРОЧТИ МЕНЯ.txt", README, "utf-8-sig")
    sys.path.insert(0, str(ROOT))
    from center.tray import make_icon_image
    make_icon_image(256).save(OUT_DIR / "app" / "center.ico", sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])

    # 6. Архив
    DIST.mkdir(exist_ok=True)
    out = DIST / "Center-Windows-x64-portable.zip"
    out.unlink(missing_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(OUT_DIR.rglob("*")):
            if f.is_file():
                z.write(f, Path("Center") / f.relative_to(OUT_DIR))
    print(f"\nГотово: {out} ({out.stat().st_size / 1e6:.1f} МБ), Python {ver}, Node.js {nver}")


if __name__ == "__main__":
    main()
