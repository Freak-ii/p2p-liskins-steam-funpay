"""Сборка Windows-версии Центра (Windows 10/11 x64): портативная папка, zip и установщик.

    python packaging/windows/build.py [--lisbot] [--installer] [--version 1.0.0]

Что получается (в dist/windows/):
    StarsCenter/                    готовая папка программы
      runtime/                      официальный CPython для Windows (пакет python с nuget.org) + зависимости
      node/                         Node.js для LisBot (только с --lisbot)
      app/                          center/ и bots/ (без тестов), лаунчер launch.pyw и desktop/ (трей, обновления)
      Центр.bat, Центр (демо).bat   запуск с окном консоли (видно журнал)
    StarsCenter-<версия>-win64-portable.zip
    StarsCenter-<версия>-setup.exe  (с --installer, нужен Inno Setup 6: ISCC.exe)

Почему не один PyInstaller-exe: центр запускает каждого бота отдельным процессом Python
(`python -m mannbot...`, `python -m starsbot...`), поэтому программе нужен настоящий интерпретатор.
Официальный CPython из nuget работает как обычный Python, ничего в коде центра и ботов менять не нужно,
и антивирусы реже ругаются на него, чем на распаковывающийся PyInstaller-onefile.

Python-зависимости берутся из requirements.txt в корне и из bots/*/requirements.txt (готовые колёса
win_amd64), поэтому новый Python-бот (например StarsBot) попадает в сборку без правок этого скрипта.
Собирать можно и на Linux, включая LisBot (его единственный нативный модуль better-sqlite3 несёт готовую
сборку под Windows). Установщик на Linux собирается через Wine: --iscc "wine /путь/ISCC.exe".
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import urllib.request
import zipfile
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BUILD = ROOT / "build" / "windows"
CACHE = BUILD / "cache"
DIST = ROOT / "dist" / "windows"
OUT = DIST / "StarsCenter"
PY_MINOR = "3.12"
NODE_MAJOR = "22"
APP_NAME = "StarsCenter"
SKIP_REQS = re.compile(r"^(pytest|pytest-\w+|pyinstaller|ruff|mypy|black)\b", re.I)
PY_JUNK = ("Lib/test", "Lib/idlelib", "Lib/tkinter", "Lib/turtledemo", "Lib/ensurepip", "tcl", "Tools",
           "libs", "include")                            # .lib и заголовки нужны только для сборки C-расширений
IGNORE_ANYWHERE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "node_modules", "tests", "test",
                                         ".venv*", "*.log", "*.sqlite3")
IGNORE_AT_TOP = {"dist", "build", "packaging", "center-data", "data", "Dockerfile", "docker-compose.yml",
                 "pytest.ini", ".dockerignore", ".gitignore"}


def code_ignore(top: Path):
    """Тесты и мусор не копируются нигде, сборочные папки и данные — только на верхнем уровне бота."""
    def ignore(directory: str, names: list[str]) -> set[str]:
        out = set(IGNORE_ANYWHERE(directory, names))
        if Path(directory) == top:
            out |= IGNORE_AT_TOP & set(names)
        return out
    return ignore


def run(cmd: list[str], **kw) -> None:
    print("$", " ".join(map(str, cmd)), flush=True)
    subprocess.run(cmd, check=True, **kw)


def fetch(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    print("↓", url, flush=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=180) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    os.replace(tmp, dest)
    return dest


def get_json(url: str):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)


# ---------------------------------------------------------------- Python
def latest_python() -> str:
    versions = get_json("https://api.nuget.org/v3-flatcontainer/python/index.json")["versions"]
    stable = [v for v in versions if v.startswith(PY_MINOR + ".") and v.replace(".", "").isdigit()]
    return max(stable, key=lambda v: tuple(map(int, v.split("."))))


def install_python(runtime: Path) -> str:
    ver = latest_python()
    nupkg = fetch(f"https://api.nuget.org/v3-flatcontainer/python/{ver}/python.{ver}.nupkg", CACHE / f"python.{ver}.nupkg")
    with zipfile.ZipFile(nupkg) as z:
        for name in z.namelist():
            if name.startswith("tools/") and not name.endswith("/"):
                target = runtime / name[len("tools/"):]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(name))
    for junk in PY_JUNK:
        shutil.rmtree(runtime / junk, ignore_errors=True)
    for exe in ("python.exe", "pythonw.exe"):
        assert (runtime / exe).exists(), f"в пакете nuget нет {exe}"
    return ver


def requirement_lines() -> list[str]:
    """Все Python-зависимости центра и ботов, без тестовых пакетов. Вложенные -r раскрываются."""
    files = [ROOT / "requirements.txt", *sorted(ROOT.glob("bots/*/requirements.txt"))]
    seen_files: set[Path] = set()
    out: list[str] = []

    def walk(path: Path) -> None:
        path = path.resolve()
        if path in seen_files or not path.exists():
            return
        seen_files.add(path)
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if line.startswith(("-r ", "--requirement ")):
                walk(path.parent / line.split(None, 1)[1])
            elif line.startswith("-"):
                continue
            elif not SKIP_REQS.match(line) and line not in out:
                out.append(line)

    for f in files:
        walk(f)
    return out


PURE_SDISTS = ["pyaes==1.6.1"]


def install_packages(site: Path, reqs: list[str]) -> list[str]:
    wheels = CACHE / f"wheels-{PY_MINOR}"
    shutil.rmtree(wheels, ignore_errors=True)
    wheels.mkdir(parents=True)
    # чисто-питоновские пакеты, которые на PyPI есть только исходниками (pyaes для telethon):
    # собираем из них универсальный wheel локально, pip download найдёт его через --find-links
    for sdist in PURE_SDISTS:
        run([sys.executable, "-m", "pip", "wheel", "--disable-pip-version-check", "--no-deps",
             "-w", str(wheels), sdist])
    run([sys.executable, "-m", "pip", "download", "--disable-pip-version-check", "--only-binary=:all:",
         "--platform", "win_amd64", "--python-version", PY_MINOR, "--implementation", "cp",
         "--find-links", str(wheels), "-d", str(wheels), *reqs])
    site.mkdir(parents=True, exist_ok=True)
    names = []
    for whl in sorted(wheels.glob("*.whl")):
        names.append(whl.name)
        with zipfile.ZipFile(whl) as z:
            for name in z.namelist():
                top, _, rest = name.partition("/")
                if top.endswith(".data"):
                    kind, _, rest = rest.partition("/")
                    if kind not in ("purelib", "platlib") or not rest:
                        continue                         # scripts/headers/data в рантайме не нужны
                    target = site / rest
                else:
                    target = site / name
                if name.endswith("/"):
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(name))
    return names


# ---------------------------------------------------------------- Node.js (для LisBot)
def install_node(dest: Path) -> str:
    index = get_json("https://nodejs.org/dist/index.json")
    ver = next(r["version"] for r in index if r["version"].startswith(f"v{NODE_MAJOR}.") and r.get("lts"))
    zname = f"node-{ver}-win-x64"
    archive = fetch(f"https://nodejs.org/dist/{ver}/{zname}.zip", CACHE / f"{zname}.zip")
    with zipfile.ZipFile(archive) as z:
        for name in z.namelist():
            if name.endswith("/") or not name.startswith(zname + "/"):
                continue
            target = dest / name[len(zname) + 1:]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(name))
    return ver


def install_lisbot_modules(app_lisbot: Path, node_dir: Path) -> None:
    """node_modules LisBot для Windows. Единственный нативный модуль — better-sqlite3, а он с версии 13
    несёт готовые сборки под все ОС (prebuilds/win32-x64.node), поэтому собрать можно и на Linux."""
    if sys.platform == "win32":
        env = {**os.environ, "PATH": str(node_dir) + os.pathsep + os.environ.get("PATH", "")}
        run([str(node_dir / "npm.cmd"), "ci", "--omit=dev", "--no-audit", "--no-fund"], cwd=app_lisbot, env=env)
        run([str(node_dir / "node.exe"), "-e", "require('better-sqlite3')(':memory:').prepare('select 1').get()"],
            cwd=app_lisbot, env=env)
        return
    npm = shutil.which("npm")
    if not npm:
        sys.exit("для --lisbot на Linux нужен npm (Node.js 20+)")
    run([npm, "ci", "--omit=dev", "--no-audit", "--no-fund", "--ignore-scripts"], cwd=app_lisbot)
    mods = app_lisbot / "node_modules"
    win = list(mods.glob("better-sqlite3/prebuilds/win32-x64*.node")) + list(mods.glob("better-sqlite3/lib/win32-x64*"))
    if not win:
        sys.exit("в better-sqlite3 нет готовой сборки win32-x64 — соберите с --lisbot на Windows")
    stray = [p for p in mods.rglob("*.node") if "prebuilds" not in p.parts and "better-sqlite3" not in p.parts]
    if stray:
        sys.exit("в node_modules есть нативные модули, собранные под Linux: " + ", ".join(map(str, stray[:5])))


# ---------------------------------------------------------------- код и ярлыки
def copy_code(app: Path, with_lisbot: bool) -> list[str]:
    shutil.copytree(ROOT / "center", app / "center", ignore=code_ignore(ROOT / "center"))
    bots = []
    for bot in sorted(p for p in (ROOT / "bots").iterdir() if p.is_dir()):
        if bot.name == "lisbot" and not with_lisbot:
            continue
        shutil.copytree(bot, app / "bots" / bot.name, ignore=code_ignore(bot))
        bots.append(bot.name)
    for f in ("requirements.txt", "README.md"):
        if (ROOT / f).exists():
            shutil.copy2(ROOT / f, app / f)
    shutil.copy2(HERE / "launch.pyw", app / "launch.pyw")
    shutil.copytree(HERE / "desktop", app / "desktop", ignore=IGNORE_ANYWHERE)
    return bots


BAT = """@echo off
chcp 65001 >nul
title Центр (консоль)
cd /d "%~dp0app"
"%~dp0runtime\\python.exe" launch.pyw --console {args}%*
if errorlevel 1 pause
"""

README_TXT = """Центр: StarsBot (Telegram Stars через Fragment) + MannBot + LisBot — версия для Windows 10/11
========================================================================================

ЗАПУСК
  • Установщик: ярлык «Центр» в меню Пуск / на рабочем столе. Откроется панель в браузере.
  • Портативная версия: распакуйте архив (лучше в папку без пробелов, например C:\\StarsCenter)
    и запустите «Центр.bat» (с окном журнала) или app\\launch.pyw.
  • «Центр (демо).bat» — посмотреть программу без аккаунтов: эмуляция FunPay/Steam/Fragment.
  • Первый запуск сразу откроет мастер настройки StarsBot (FunPay, кошелёк TON, Fragment).
  • Программа живёт в трее (значок-звезда у часов): клик — панель, правый клик — меню
    (боты, папка данных, журналы, автозапуск, обновления, выход).
  • Выход: «Выключить центр и ботов» в меню трея или кнопка в панели.
    Закрытие вкладки браузера программу НЕ останавливает.

ОБНОВЛЕНИЯ
  Программа сама проверяет новые версии (GitHub Releases), если в файле
  %LOCALAPPDATA%\\StarsCenter\\updates.json указан токен GitHub только на чтение:
    {"repo": "Freak-ii/p2p-liskins-steam-funpay", "token": "github_pat_..."}
  Обновление ставится по клику на уведомление, данные сохраняются.

ГДЕ ДАННЫЕ
  %LOCALAPPDATA%\\StarsCenter\\center-data  (настройки, базы, журналы, ключи).
  Свою папку можно задать переменной CENTER_HOME или параметром --home.
  Переустановка и удаление программы эти данные не трогают. Резервная копия = копия этой папки.

ВАЖНО
  • Пока бот продаёт, компьютер должен быть включён и не засыпать
    (Параметры → Система → Питание → «Переводить в спящий режим» = Никогда).
  • SmartScreen может показать «Windows защитила ваш компьютер» — программа не подписана
    сертификатом. Нажмите «Подробнее» → «Выполнить в любом случае».
  • Панель доступна только с этого компьютера (127.0.0.1).
  • Сид-фразу TON-кошелька и golden_key храните только в настройках бота, никому не отправляйте.
"""


def write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.write_bytes(text.replace("\r\n", "\n").replace("\n", "\r\n").encode(encoding))


# ---------------------------------------------------------------- иконка (без Pillow)
def star_icon(path: Path) -> None:
    """Рисует иконку-звезду (жёлтая звезда на синем круге) и сохраняет .ico с PNG-кадрами 16…256."""
    import math

    def render(n: int) -> bytes:
        pts = []
        for i in range(10):
            r = (0.40 if i % 2 == 0 else 0.17) * n
            a = -math.pi / 2 + i * math.pi / 5
            pts.append((n / 2 + r * math.cos(a), n / 2 + 0.03 * n + r * math.sin(a)))

        def inside(x: float, y: float) -> bool:
            c = False
            for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
                if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                    c = not c
            return c

        ss = 4                                           # сглаживание: 4×4 подвыборки на пиксель
        rows = bytearray()
        for py in range(n):
            rows.append(0)
            for px in range(n):
                star = disk = 0
                for sy in range(ss):
                    for sx in range(ss):
                        x, y = px + (sx + .5) / ss, py + (sy + .5) / ss
                        if (x - n / 2) ** 2 + (y - n / 2) ** 2 <= (n * .48) ** 2:
                            disk += 1
                            star += inside(x, y)
                k = ss * ss
                fs, fd = star / k, disk / k
                bg = (0x22, 0x9E, 0xD9)                  # синий Telegram
                fg = (0xFF, 0xC8, 0x2E)
                rgb = [round(fg[i] * fs / max(fd, 1e-9) + bg[i] * (1 - fs / max(fd, 1e-9))) if fd else 0 for i in range(3)]
                rows += bytes(rgb) + bytes([round(255 * fd)])
        raw = zlib.compress(bytes(rows), 9)

        def chunk(t: bytes, d: bytes) -> bytes:
            return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0))
                + chunk(b"IDAT", raw) + chunk(b"IEND", b""))

    sizes = [16, 32, 48, 256]
    images = [render(s) for s in sizes]
    head = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = b""
    for s, img in zip(sizes, images):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(img), offset)
        offset += len(img)
    path.write_bytes(head + entries + b"".join(images))


# ---------------------------------------------------------------- main
def make_zip(src: Path, out: Path) -> None:
    out.unlink(missing_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                z.write(f, Path(APP_NAME) / f.relative_to(src))


def winpath(p: Path) -> str:
    """Путь Linux → путь Wine (диск Z: — это корень файловой системы)."""
    return "Z:" + str(Path(p).resolve()).replace("/", "\\")


def find_iscc() -> str | None:
    for c in (shutil.which("iscc"), shutil.which("ISCC"),
              r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe", r"C:\Program Files\Inno Setup 6\ISCC.exe",
              os.path.expandvars(r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe")):
        if c and Path(c).exists():
            return c
    return None


def main() -> None:
    for stream in (sys.stdout, sys.stderr):          # Windows-раннер пишет в cp1252: стрелки и кириллица роняли сборку
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--lisbot", action="store_true", help="включить LisBot и Node.js (только при сборке на Windows)")
    ap.add_argument("--installer", action="store_true", help="собрать установщик Inno Setup (нужен ISCC.exe)")
    ap.add_argument("--version", default=os.environ.get("CENTER_VERSION", "1.0.0"))
    ap.add_argument("--no-zip", action="store_true")
    ap.add_argument("--iscc", help="команда компилятора Inno Setup, например \"wine C:/is/ISCC.exe\"")
    args = ap.parse_args()

    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    CACHE.mkdir(parents=True, exist_ok=True)

    runtime = OUT / "runtime"
    py_ver = install_python(runtime)
    reqs = requirement_lines()
    wheels = install_packages(runtime / "Lib" / "site-packages", reqs)

    app = OUT / "app"
    bots = copy_code(app, args.lisbot)
    node_ver = ""
    if args.lisbot:
        node_ver = install_node(OUT / "node")
        install_lisbot_modules(app / "bots" / "lisbot", OUT / "node")

    star_icon(app / "center.ico")
    write_text(OUT / "Центр.bat", BAT.format(args=""))
    write_text(OUT / "Центр (демо).bat", BAT.format(args="--demo "))
    write_text(OUT / "ПРОЧТИ МЕНЯ.txt", README_TXT, "utf-8-sig")
    (OUT / "build-info.json").write_text(json.dumps({
        "version": args.version, "python": py_ver, "node": node_ver, "bots": bots, "requirements": reqs,
        "wheels": wheels}, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nПапка: {OUT}\n  Python {py_ver}, боты: {', '.join(bots)}" + (f", Node {node_ver}" if node_ver else ""))
    if not args.no_zip:
        z = DIST / f"{APP_NAME}-{args.version}-win64-portable.zip"
        make_zip(OUT, z)
        print(f"  Архив: {z} ({z.stat().st_size / 1e6:.1f} МБ)")
    if args.installer:
        if args.iscc:
            import shlex
            iscc_cmd = shlex.split(args.iscc)
            paths = [winpath(OUT), winpath(DIST), winpath(HERE / "StarsCenter.iss")] if iscc_cmd[0].endswith("wine") \
                else [str(OUT), str(DIST), str(HERE / "StarsCenter.iss")]
        else:
            iscc = find_iscc()
            if not iscc:
                sys.exit("Не найден Inno Setup 6 (ISCC.exe): https://jrsoftware.org/isdl.php")
            iscc_cmd, paths = [iscc], [str(OUT), str(DIST), str(HERE / "StarsCenter.iss")]
        run([*iscc_cmd, f"/DAppVersion={args.version}", f"/DSourceDir={paths[0]}", f"/DOutputDir={paths[1]}", paths[2]])
        print(f"  Установщик: {DIST / f'{APP_NAME}-{args.version}-setup.exe'}")


if __name__ == "__main__":
    main()
