"""Сборка портативной Windows-версии MannBot (работает и на Linux, и на Windows):

    python packaging/build_windows_portable.py

Результат: dist/MannBot-Windows-x64-portable.zip — распаковать и запустить «MannBot.bat».
Внутри: официальный CPython для Windows (пакет python с nuget.org), зависимости (win_amd64-колёса
с PyPI), код приложения и ярлыки запуска. Установка Python у пользователя не нужна.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "win-portable"
OUT_DIR = BUILD / "MannBot"
DIST = ROOT / "dist"
PY_MINOR = "3.12"
REQUIREMENTS = ["aiohttp>=3.9", "beautifulsoup4>=4.12", "lxml>=5.0", "PyYAML>=6.0", "Jinja2>=3.1",
                "cryptography>=42"]

BAT = r"""@echo off
chcp 65001 >nul
title MannBot
cd /d "%~dp0"
set "MANNBOT_HOME=%~dp0data"
"%~dp0runtime\python.exe" "%~dp0app\MannBot.py" %*
if errorlevel 1 (
  echo.
  echo MannBot завершился с ошибкой. Журнал: data\data\mannbot.log
  pause
)
"""

BAT_DEMO = r"""@echo off
chcp 65001 >nul
title MannBot DEMO
cd /d "%~dp0"
set "MANNBOT_HOME=%~dp0data"
"%~dp0runtime\python.exe" "%~dp0app\MannBot.py" --demo %*
"""

VBS_HIDDEN = r"""' Запуск MannBot без окна консоли. Закрыть программу — кнопка ⏻ в интерфейсе.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
d = fso.GetParentFolderName(WScript.ScriptFullName)
sh.Environment("PROCESS")("MANNBOT_HOME") = d & "\data"
sh.CurrentDirectory = d
q = Chr(34)
sh.Run q & d & "\runtime\pythonw.exe" & q & " " & q & d & "\app\MannBot.py" & q, 0, False
"""

README = """MannBot — автопродажа ключей Mann Co. Supply Crate Key на FunPay
===================================================================

ЗАПУСК
  1. Распакуйте архив в любую папку (лучше без кириллицы и пробелов, например C:\\MannBot).
  2. Дважды щёлкните «MannBot.bat». Откроется браузер с интерфейсом программы.
     Окно консоли — это сама программа: пока оно открыто, бот работает.
     Вариант без консоли: «MannBot (без окна).vbs», закрыть — кнопка ⏻ в интерфейсе.
  3. Пройдите «Мастер настройки»: golden_key FunPay, ID лота, maFile Steam.
  4. Нажмите «Запустить бота». Первое время держите «Тестовый режим» — бот ничего не отправляет.
  5. Когда всё проверите — «Включить боевой режим» в жёлтой полосе сверху.

  Посмотреть программу без настройки: «MannBot DEMO.bat» (эмуляция FunPay и Steam).

ГДЕ ДАННЫЕ
  Папка data\\ — настройки (config.yaml), база заказов (data\\mannbot.sqlite3), журнал.
  Для переноса на другой ПК скопируйте всю папку MannBot целиком.
  Резервная копия = копия папки data\\.

ВАЖНО
  • Компьютер должен быть включён и программа запущена, пока бот продаёт.
    Для работы 24/7 используйте VPS (см. Docker-вариант в README проекта).
  • Windows Defender/SmartScreen может предупредить о неизвестном издателе — это
    нормально для самостоятельно собранных программ.
  • Интерфейс доступен только с этого компьютера (127.0.0.1).
"""


def run(cmd: list[str]) -> None:
    print("$", " ".join(cmd))
    subprocess.run(cmd, check=True)


def fetch(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    print("↓", url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    return dest


def latest_python() -> str:
    with urllib.request.urlopen("https://api.nuget.org/v3-flatcontainer/python/index.json", timeout=60) as r:
        versions = json.load(r)["versions"]
    stable = [v for v in versions if v.startswith(PY_MINOR + ".") and v.replace(".", "").isdigit()]
    return sorted(stable, key=lambda v: tuple(map(int, v.split("."))))[-1]


def main() -> None:
    shutil.rmtree(OUT_DIR, ignore_errors=True)
    OUT_DIR.mkdir(parents=True)
    cache = BUILD / "cache"

    # 1. Python для Windows (официальная сборка CPython, пакет nuget «python»)
    ver = latest_python()
    nupkg = fetch(f"https://api.nuget.org/v3-flatcontainer/python/{ver}/python.{ver}.nupkg", cache / f"python.{ver}.nupkg")
    runtime = OUT_DIR / "runtime"
    with zipfile.ZipFile(nupkg) as z:
        for name in z.namelist():
            if name.startswith("tools/") and not name.endswith("/"):
                target = runtime / name[len("tools/"):]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(name))
    for junk in ("Lib/test", "Lib/idlelib", "Lib/tkinter", "Lib/turtledemo", "Lib/ensurepip"):
        shutil.rmtree(runtime / junk, ignore_errors=True)
    assert (runtime / "python.exe").exists(), "в пакете nuget нет python.exe"

    # 2. Зависимости: готовые колёса win_amd64 для этой версии Python
    wheels = cache / f"wheels-{PY_MINOR}"
    wheels.mkdir(parents=True, exist_ok=True)
    run([sys.executable, "-m", "pip", "download", "--only-binary=:all:", "--platform", "win_amd64",
         "--python-version", PY_MINOR, "--implementation", "cp", "-d", str(wheels), *REQUIREMENTS])
    site = runtime / "Lib" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    for whl in sorted(wheels.glob("*.whl")):
        with zipfile.ZipFile(whl) as z:
            for name in z.namelist():
                if ".data/" in name.split("/", 1)[0] + "/":
                    continue
                z.extract(name, site)

    # 3. Код приложения
    app = OUT_DIR / "app"
    shutil.copytree(ROOT / "mannbot", app / "mannbot", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(ROOT / "MannBot.py", app / "MannBot.py")
    shutil.copy2(ROOT / "config.example.yaml", app / "config.example.yaml")
    (OUT_DIR / "data").mkdir()

    # 4. Ярлыки запуска и инструкция (CRLF для Windows)
    def write(name: str, text: str, encoding: str = "utf-8") -> None:
        (OUT_DIR / name).write_bytes(text.replace("\r\n", "\n").replace("\n", "\r\n").encode(encoding))
    write("MannBot.bat", BAT)
    write("MannBot DEMO.bat", BAT_DEMO)
    write("MannBot (без окна).vbs", VBS_HIDDEN, "utf-16")
    write("ПРОЧТИ МЕНЯ.txt", README, "utf-8-sig")

    # 5. Архив
    DIST.mkdir(exist_ok=True)
    out = DIST / "MannBot-Windows-x64-portable.zip"
    out.unlink(missing_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(OUT_DIR.rglob("*")):
            if f.is_file():
                z.write(f, Path("MannBot") / f.relative_to(OUT_DIR))
    print(f"\nГотово: {out} ({out.stat().st_size / 1e6:.1f} МБ), Python {ver}")


if __name__ == "__main__":
    main()
