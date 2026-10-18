"""Резервные копии данных центра и ботов: center-data/backups/center-backup-ГГГГММДД-ЧЧММСС.zip.

В копию попадает вся папка данных центра: center.yaml, center.sqlite3, secrets.dat, данные LisBot
(config.json, bot.sqlite) и MannBot (config.yaml, mannbot.sqlite3). Базы SQLite снимаются через
backup API SQLite — копия целостна, даже пока боты пишут в базу, — и проверяются integrity_check.
Не копируются журналы, -wal/-shm, сами копии и ключ secret.key (на Linux/Docker без него secrets.dat
не расшифровать, поэтому копия без ключа не раскрывает пароли; ключ хранится отдельно или задаётся
CENTER_SECRET_KEY). На Windows secrets.dat зашифрован DPAPI и расшифруется только этим пользователем.

Восстановление — только при остановленном центре и ботах: python -m center --restore ФАЙЛ, или кнопка
в панели (центр останавливается и восстанавливает копию при следующем запуске, до старта ботов).
Перед восстановлением текущие данные сохраняются копией «before-restore».
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path

from aiohttp import web

log = logging.getLogger("center.backup")

PREFIX = "center-backup-"
PENDING = "restore.pending"
SQLITE_MAGIC = b"SQLite format 3\x00"
SKIP_DIRS = {"logs", "backups", "__pycache__"}
SKIP_NAMES = {"secret.key", "last-port.txt", PENDING, "center.yaml.tmp"}
SKIP_SUFFIXES = ("-wal", "-shm", "-journal", ".log", ".tmp", ".lock", ".pid")
NAME_RE = re.compile(r"^center-backup-\d{8}-\d{6}(-[a-z-]+)?\.zip$")


def _is_sqlite(p: Path) -> bool:
    try:
        with p.open("rb") as f:
            return f.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def _skip(rel: Path) -> bool:
    if any(part in SKIP_DIRS for part in rel.parts[:-1]):
        return True
    n = rel.name
    return n in SKIP_NAMES or n.endswith(SKIP_SUFFIXES) or re.search(r"\.log\.\d+$", n) is not None


def _sqlite_copy(src: Path, dst: Path) -> str:
    """Целостный снимок живой базы; возвращает результат integrity_check копии."""
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=10)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
        d.execute("PRAGMA journal_mode=DELETE")    # копия — один файл без -wal
        return str(d.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        d.close()
        s.close()


def make_backup(home: Path, dest_dir: Path, *, label: str = "", now: float | None = None) -> dict:
    """Синхронно собирает zip-копию папки home; возвращает сведения о копии (manifest)."""
    home, dest_dir = Path(home), Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    now = time.time() if now is None else now
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    target = dest_dir / f"{PREFIX}{stamp}{'-' + label if label else ''}.zip"
    while target.exists():                       # две копии в одну секунду: следующая секунда в имени
        now += 1
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        target = dest_dir / f"{PREFIX}{stamp}{'-' + label if label else ''}.zip"
    files, problems = [], []
    dest_res = dest_dir.resolve()
    tmp_zip = target.with_name(target.name + ".part")
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(home.rglob("*")):
            if not p.is_file():
                continue
            try:
                if p.resolve().is_relative_to(dest_res):
                    continue
            except OSError:
                continue
            rel = p.relative_to(home)
            if _skip(rel):
                continue
            arc = rel.as_posix()
            entry = {"path": arc}
            try:
                if _is_sqlite(p):
                    snap = Path(tmp) / f"{len(files)}.sqlite"
                    entry["integrity"] = _sqlite_copy(p, snap)
                    entry["sqlite"] = True
                    if entry["integrity"] != "ok":
                        problems.append(f"{arc}: {entry['integrity']}")
                    z.write(snap, arc)
                    entry["size"] = snap.stat().st_size
                    snap.unlink()
                else:
                    z.write(p, arc)
                    entry["size"] = p.stat().st_size
            except (OSError, sqlite3.Error) as e:
                problems.append(f"{arc}: {e}")
                continue
            files.append(entry)
        manifest = {"app": "center", "version": 1, "created": now, "label": label, "home": str(home),
                    "files": files, "problems": problems,
                    "note": "secret.key не входит в копию: храните его отдельно или задайте CENTER_SECRET_KEY"}
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
    os.replace(tmp_zip, target)
    return {**manifest, "name": target.name, "size": target.stat().st_size}


def read_manifest(path: Path) -> dict:
    with zipfile.ZipFile(path) as z:
        m = json.loads(z.read("manifest.json"))
    if m.get("app") != "center":
        raise ValueError("это не резервная копия центра")
    return m


def restore_backup(path: Path, home: Path, *, keep_dir: Path | None = None) -> dict:
    """Восстанавливает копию в home. Центр и боты должны быть остановлены. Сначала сохраняет текущее
    состояние копией before-restore; заменяет только файлы из копии, удаляя -wal/-shm заменяемых баз."""
    path, home = Path(path), Path(home)
    m = read_manifest(path)
    keep_dir = keep_dir or home / "backups"
    before = make_backup(home, keep_dir, label="before-restore") if any(home.iterdir()) else None
    home_res = home.resolve()
    restored = []
    with zipfile.ZipFile(path) as z:
        names = [i.filename for i in z.infolist() if not i.is_dir() and i.filename != "manifest.json"]
        for arc in names:
            dst = (home / arc).resolve()
            if not dst.is_relative_to(home_res) or Path(arc).name == "secret.key":
                raise ValueError(f"недопустимый путь в копии: {arc}")
        for arc in names:
            dst = home / arc
            dst.parent.mkdir(parents=True, exist_ok=True)
            for suf in ("-wal", "-shm", "-journal"):
                side = dst.with_name(dst.name + suf)
                if side.exists():
                    side.unlink()
            tmp = dst.with_name(dst.name + ".restore-tmp")
            with z.open(arc) as src, tmp.open("wb") as out:
                shutil.copyfileobj(src, out)
            os.replace(tmp, dst)
            restored.append(arc)
    return {"restored": restored, "created": m.get("created"), "before": before and before["name"]}


def apply_pending_restore(home: Path) -> dict | None:
    """Вызывается при запуске центра до открытия баз: восстанавливает копию, выбранную в панели."""
    home = Path(home)
    flag = home / PENDING
    if not flag.exists():
        return None
    try:
        info = json.loads(flag.read_text(encoding="utf-8"))
        src = Path(info["path"])
        res = restore_backup(src, home, keep_dir=src.parent)
        res["name"] = src.name
        return res
    finally:
        flag.unlink(missing_ok=True)


class Backup:
    def __init__(self, center, clock=time.time):
        self.center = center
        self.clock = clock
        self._task: asyncio.Task | None = None
        self._running = asyncio.Lock()
        self.last: dict | None = None

    @property
    def cfg(self) -> dict:
        return self.center.cfg["backup"]

    @property
    def dir(self) -> Path:
        d = str(self.cfg.get("dir") or "").strip()
        return Path(d).expanduser() if d else self.center.home / "backups"

    def list(self) -> list[dict]:
        if not self.dir.is_dir():
            return []
        out = []
        for p in self.dir.glob(f"{PREFIX}*.zip"):
            st = p.stat()
            out.append({"name": p.name, "size": st.st_size, "ts": st.st_mtime,
                        "label": "before-restore" if "before-restore" in p.name else "manual" if "-manual" in p.name else ""})
        out.sort(key=lambda x: x["ts"], reverse=True)
        return out

    def path(self, name: str) -> Path:
        if not NAME_RE.match(name):
            raise ValueError("неверное имя копии")
        p = self.dir / name
        if not p.is_file():
            raise FileNotFoundError(name)
        return p

    def prune(self) -> int:
        keep = max(1, int(self.cfg.get("keep") or 14))
        removed = 0
        for label in ("", "manual", "before-restore"):     # у каждого вида копий свой счёт
            for b in [b for b in self.list() if b["label"] == label][keep:]:
                (self.dir / b["name"]).unlink(missing_ok=True)
                removed += 1
        return removed

    async def run(self, *, manual: bool = False) -> dict:
        async with self._running:
            t0 = time.perf_counter()
            try:
                info = await asyncio.to_thread(make_backup, self.center.home, self.dir,
                                               label="manual" if manual else "", now=self.clock())
            except Exception as e:  # noqa: BLE001
                log.exception("резервная копия")
                self.last = {"ok": False, "at": self.clock(), "error": str(e)}
                self.center.events.add("center", "warn", "backup.failed", f"Резервная копия не создана: {e}")
                return self.last
            removed = await asyncio.to_thread(self.prune)
            ok = not info["problems"]
            self.last = {"ok": ok, "at": self.clock(), "name": info["name"], "size": info["size"],
                         "files": len(info["files"]), "problems": info["problems"],
                         "seconds": round(time.perf_counter() - t0, 1)}
            self._kv_set("backup_last", json.dumps(self.last, ensure_ascii=False))
            if ok:
                self.center.events.add("center", "info", "backup.done",
                                       f"Резервная копия {info['name']} ({info['size'] / 1e6:.1f} МБ, файлов {len(info['files'])})"
                                       + (f", удалено старых {removed}" if removed else ""))
            else:
                self.center.events.add("center", "warn", "backup.failed",
                                       "Резервная копия создана с ошибками: " + "; ".join(info["problems"][:3]))
            return self.last

    # отметка о последней копии живёт в monitor_kv, чтобы пережить перезапуск центра
    def _kv_get(self, k: str) -> str | None:
        try:
            return self.center.monitor.kv_get(k)
        except Exception:  # noqa: BLE001
            return None

    def _kv_set(self, k: str, v: str) -> None:
        try:
            self.center.monitor.kv_set(k, v)
        except Exception:  # noqa: BLE001
            pass

    def due(self) -> bool:
        """Пора ли делать плановую копию: включено, наступило время дня и сегодня копии ещё не было."""
        from center.monitor import parse_hhmm
        c = self.cfg
        at = parse_hhmm(str(c.get("time") or "04:00"))
        if not c.get("enabled", True) or at is None:
            return False
        lt = time.localtime(self.clock())
        if lt.tm_hour * 60 + lt.tm_min < at:
            return False
        return self._kv_get("backup_date") != time.strftime("%Y-%m-%d", lt)

    async def _loop(self) -> None:
        while True:
            try:
                if self.due():
                    self._kv_set("backup_date", time.strftime("%Y-%m-%d", time.localtime(self.clock())))
                    await self.run()
            except Exception:  # noqa: BLE001
                log.exception("плановая резервная копия")
            await asyncio.sleep(60)

    async def start(self) -> None:
        raw = self._kv_get("backup_last")
        if raw:
            try:
                self.last = json.loads(raw)
            except ValueError:
                pass
        self._task = asyncio.create_task(self._loop(), name="backup")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def settings(self) -> dict:
        c = self.cfg
        return {"enabled": bool(c.get("enabled", True)), "time": str(c.get("time") or "04:00"),
                "keep": int(c.get("keep") or 14), "dir": str(c.get("dir") or ""), "dir_effective": str(self.dir)}

    def save_settings(self, d: dict) -> None:
        t = str(d.get("time") or "04:00").strip()
        if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", t):
            raise ValueError("время копии в формате ЧЧ:ММ")
        keep = int(d.get("keep") or 14)
        if not 1 <= keep <= 365:
            raise ValueError("хранить от 1 до 365 копий")
        folder = str(d.get("dir") or "").strip()
        if folder:
            p = Path(folder).expanduser()
            try:
                p.mkdir(parents=True, exist_ok=True)
                probe = p / ".center-write-test"
                probe.write_text("ok")
                probe.unlink()
            except OSError as e:
                raise ValueError(f"папка недоступна для записи: {e}") from e
        self.center.cfg.save({"backup": {"enabled": bool(d.get("enabled", True)), "time": t, "keep": keep, "dir": folder}})

    def status(self) -> dict:
        return {**self.settings(), "last": self.last, "running": self._running.locked(), "backups": self.list()}

    def schedule_restore(self, name: str) -> dict:
        p = self.path(name)
        m = read_manifest(p)
        (self.center.home / PENDING).write_text(json.dumps({"path": str(p.resolve()), "at": self.clock()}), encoding="utf-8")
        self.center.events.add("center", "warn", "backup.restore_scheduled",
                               f"Выбрано восстановление копии {name}: центр остановится и восстановит её при следующем запуске")
        return m


def routes(center) -> list:
    """Маршруты панели для резервных копий; подключаются в center/web.py."""
    b: Backup = center.backup

    async def status(request: web.Request):
        return web.json_response(b.status())

    async def run(request: web.Request):
        if b._running.locked():
            return web.json_response({"ok": False, "message": "копия уже делается"}, status=409)
        r = await b.run(manual=True)
        msg = (f"Копия {r['name']} готова" if r["ok"] else
               f"Копия не удалась: {r.get('error') or '; '.join(r.get('problems', []))}")
        return web.json_response({"ok": r["ok"], "message": msg, "last": r}, status=200 if r["ok"] else 500)

    async def settings(request: web.Request):
        if request.method == "POST":
            try:
                b.save_settings(await request.json())
            except (ValueError, TypeError) as e:
                return web.json_response({"ok": False, "message": str(e)}, status=400)
            center.events.add("center", "info", "settings", "Настройки резервных копий изменены из панели центра")
            return web.json_response({"ok": True, "message": "Настройки резервных копий сохранены"})
        return web.json_response(b.settings())

    async def download(request: web.Request):
        try:
            p = b.path(request.match_info["name"])
        except (ValueError, FileNotFoundError):
            raise web.HTTPNotFound()
        return web.FileResponse(p, headers={"Content-Disposition": f'attachment; filename="{p.name}"'})

    async def restore(request: web.Request):
        d = await request.json()
        if d.get("confirm") != "ВОССТАНОВИТЬ":
            return web.json_response({"ok": False, "message": "нужно подтверждение"}, status=400)
        try:
            m = b.schedule_restore(str(d.get("name") or ""))
        except (ValueError, FileNotFoundError, zipfile.BadZipFile, KeyError) as e:
            return web.json_response({"ok": False, "message": f"копию нельзя восстановить: {e}"}, status=400)
        asyncio.get_running_loop().call_later(1, center.quit_event.set)
        when = time.strftime("%d.%m.%Y %H:%M", time.localtime(m.get("created") or 0))
        return web.json_response({"ok": True, "message": f"Центр останавливается. Запустите его снова: копия от {when} "
                                                         "восстановится до запуска ботов."})

    return [
        web.get("/api/backup", status), web.post("/api/backup/run", run),
        web.get("/api/backup/settings", settings), web.post("/api/backup/settings", settings),
        web.get("/api/backup/download/{name}", download), web.post("/api/backup/restore", restore),
    ]
