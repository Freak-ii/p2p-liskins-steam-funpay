"""Орбита: переезд данных с Windows-ПК на сервер одним зашифрованным файлом.

На ПК (центр закрыт!), встроенным Python портативной сборки:
    runtime\\python.exe app\\center\\migrate.py export --out D:\\orbita.bundle
        [--home %LOCALAPPDATA%\\StarsCenter\\center-data] [--keep-pc-bots]
На сервере:
    sudo orbita import /root/orbita.bundle

Что внутри: вся папка center-data (настройки, базы, сессии Telegram, cookies Fragment, сид TON в настройках
StarsBot) без журналов и резервных копий, плюс секреты из secrets.dat, расшифрованные DPAPI на ПК.
Файл целиком шифруется паролем, который вы вводите (scrypt + Fernet). Без пароля файл бесполезен.

Чтобы два экземпляра ботов не работали одновременно на одном аккаунте FunPay, одном кошельке TON
и одной сессии Telegram (двойная выдача заказов, вылет сессии), export выключает ботов на ПК
(workers.<бот>.enabled: false в center.yaml, копия — center.yaml.before-orbita) и кладёт рядом MOVED_TO_SERVER.txt.
Вернуть ПК в работу: восстановить center.yaml из копии — но только когда сервер остановлен.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import io
import json
import os
import shutil
import sys
import time
import zipfile
from pathlib import Path

MAGIC = b"ORBITA-BUNDLE-1\n"
SKIP_DIRS = {"logs", "backups", "demo", "__pycache__", "updates"}
SKIP_SUFFIX = (".log", ".log.1", ".tmp", "-wal", "-shm", "-journal")
BOTS = ("mannbot", "lisbot", "starsbot", "shopbot", "tggift")


def _key(password: str, salt: bytes) -> bytes:
    raw = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)
    return base64.urlsafe_b64encode(raw)


def seal(payload: bytes, password: str) -> bytes:
    from cryptography.fernet import Fernet
    salt = os.urandom(16)
    return MAGIC + salt + Fernet(_key(password, salt)).encrypt(payload)


def unseal(blob: bytes, password: str) -> bytes:
    from cryptography.fernet import Fernet, InvalidToken
    if not blob.startswith(MAGIC):
        raise ValueError("это не файл переезда Орбиты")
    salt, token = blob[len(MAGIC):len(MAGIC) + 16], blob[len(MAGIC) + 16:]
    try:
        return Fernet(_key(password, salt)).decrypt(token)
    except InvalidToken as e:
        raise ValueError("неверный пароль или файл повреждён") from e


def _files(home: Path):
    for p in sorted(home.rglob("*")):
        rel = p.relative_to(home)
        if any(part in SKIP_DIRS for part in rel.parts[:-1]) or (p.is_dir() and rel.name in SKIP_DIRS):
            continue
        if p.is_file() and not p.name.endswith(SKIP_SUFFIX) and p.name not in ("secrets.dat", "secret.key"):
            yield p, rel


def _sqlite_copy(src: Path) -> bytes:
    """Согласованная копия базы (на случай, если файл WAL не слит)."""
    import sqlite3
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "db"
        s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        t = sqlite3.connect(out)
        with t:
            s.backup(t)
        s.close(), t.close()
        return out.read_bytes()


def build_payload(home: Path, secrets: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p, rel in _files(home):
            data = p.read_bytes()
            if p.suffix in (".sqlite3", ".sqlite", ".db") and data[:16] == b"SQLite format 3\x00":
                try:
                    data = _sqlite_copy(p)
                except Exception:  # noqa: BLE001 — тогда как есть
                    pass
            z.writestr("data/" + rel.as_posix(), data)
        z.writestr("secrets.json", json.dumps(secrets, ensure_ascii=False))
        z.writestr("meta.json", json.dumps({"created": time.time(), "from": sys.platform, "home": str(home)}))
    return buf.getvalue()


def _center_running(home: Path) -> bool:
    import socket
    import urllib.request
    try:
        import yaml
        port = int(((yaml.safe_load((home / "center.yaml").read_text(encoding="utf-8")) or {}).get("center") or {})
                   .get("port") or 8800)
    except Exception:  # noqa: BLE001
        port = 8800
    with socket.socket() as s:
        s.settimeout(1)
        if s.connect_ex(("127.0.0.1", port)) != 0:
            return False
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2):
            return True
    except Exception:  # noqa: BLE001
        return True


def disable_pc_bots(home: Path) -> None:
    import yaml
    cfg_path = home / "center.yaml"
    raw = {}
    if cfg_path.exists():
        shutil.copy2(cfg_path, cfg_path.with_name("center.yaml.before-orbita"))
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    workers = raw.get("workers") if isinstance(raw.get("workers"), dict) else {}
    raw["workers"] = workers
    for b in BOTS:
        if not isinstance(workers.get(b), dict):
            workers[b] = {}
        workers[b]["enabled"] = False
    cfg_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (home / "MOVED_TO_SERVER.txt").write_text(
        "Данные перенесены на сервер Орбиты " + time.strftime("%Y-%m-%d %H:%M") + ".\n"
        "Боты на этом ПК выключены, чтобы не работать одновременно с сервером.\n"
        "Вернуть: остановите сервер, затем замените center.yaml копией center.yaml.before-orbita.\n", encoding="utf-8")


def cmd_export(a) -> int:
    home = Path(os.path.expandvars(a.home)).expanduser()
    if a.app:
        sys.path.insert(0, str(Path(a.app)))
    if not (home / "center.yaml").exists():
        print(f"не найдена папка данных центра: {home}")
        return 2
    if _center_running(home):
        print("Центр запущен. Закройте его (значок у часов → Выход) и запустите export снова.")
        return 2
    secrets = {}
    if (home / "secrets.dat").exists():
        from center.vault import SecretStore
        secrets = dict(SecretStore(home)._load())
    pw = a.password or getpass.getpass("Пароль для файла переезда: ")
    if not a.password and pw != getpass.getpass("Ещё раз: "):
        print("пароли не совпали")
        return 2
    if len(pw) < 10:
        print("пароль короче 10 символов — возьмите длиннее")
        return 2
    blob = seal(build_payload(home, secrets), pw)
    out = Path(a.out)
    out.write_bytes(blob)
    print(f"Готово: {out} ({len(blob) // 1024} КБ), секретов: {len(secrets)}")
    if not a.keep_pc_bots:
        disable_pc_bots(home)
        print("Боты на этом ПК выключены (center.yaml.before-orbita — прежние настройки).")
    return 0


WINDOWS_KEYS = ("python", "node")


def import_bundle(blob: bytes, password: str, home: Path, *, force: bool = False) -> dict:
    payload = unseal(blob, password)
    home.mkdir(parents=True, exist_ok=True)
    if any(home.glob("*.yaml")) and not force:
        raise RuntimeError(f"в {home} уже есть данные; добавьте --force, чтобы заменить (прежние уйдут в копию)")
    if any(home.iterdir()):
        bak = home.with_name(home.name + "-before-import-" + time.strftime("%Y%m%d-%H%M%S"))
        shutil.copytree(home, bak)
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        secrets = json.loads(z.read("secrets.json"))
        n = 0
        for info in z.infolist():
            if not info.filename.startswith("data/") or info.is_dir():
                continue
            rel = Path(info.filename[5:])
            if rel.is_absolute() or ".." in rel.parts:
                continue
            dest = home / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(z.read(info))
            os.chmod(dest, 0o600)
            n += 1
    # пути к python.exe/node.exe с Windows на сервере не нужны; MOVED-метка ПК серверу не нужна
    warnings = []
    try:
        import yaml
        cfg_path = home / "center.yaml"
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        for name, w in (raw.get("workers") or {}).items():
            if isinstance(w, dict):
                for k in WINDOWS_KEYS:
                    w.pop(k, None)
        cfg_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
        for y in home.rglob("*.yaml"):
            if ":\\" in y.read_text(encoding="utf-8", errors="replace"):
                warnings.append(f"{y.relative_to(home)}: есть пути Windows (C:\\...) — проверьте")
    except Exception as e:  # noqa: BLE001
        warnings.append(f"center.yaml: {e}")
    (home / "MOVED_TO_SERVER.txt").unlink(missing_ok=True)
    (home / "center.yaml.before-orbita").unlink(missing_ok=True)
    # секреты — заново в secrets.dat, уже ключом сервера (CENTER_SECRET_KEY, Fernet)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from center.vault import SecretStore
    (home / "secrets.dat").unlink(missing_ok=True)
    store = SecretStore(home, backend="fernet")
    store._data = {}
    store.update(secrets)
    return {"files": n, "secrets": len(secrets), "warnings": warnings}


def cmd_import(a) -> int:
    home = Path(a.home or os.environ.get("CENTER_HOME") or "/var/lib/orbita")
    pw = a.password or getpass.getpass("Пароль файла переезда: ")
    try:
        res = import_bundle(Path(a.bundle).read_bytes(), pw, home, force=a.force)
    except (ValueError, RuntimeError) as e:
        print(f"ошибка: {e}")
        return 2
    print(f"Перенесено файлов: {res['files']}, секретов: {res['secrets']} → {home}")
    for w in res["warnings"]:
        print("  ! " + w)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="migrate", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="на ПК: упаковать center-data и секреты в зашифрованный файл")
    e.add_argument("--home", default=r"%LOCALAPPDATA%\StarsCenter\center-data")
    e.add_argument("--app", default=str(Path(__file__).resolve().parent.parent),
                   help="папка с кодом центра (для чтения secrets.dat)")
    e.add_argument("--out", required=True)
    e.add_argument("--password", help=argparse.SUPPRESS)
    e.add_argument("--keep-pc-bots", action="store_true", help="не выключать ботов на ПК (опасно)")
    i = sub.add_parser("import", help="на сервере: распаковать файл переезда в папку данных")
    i.add_argument("bundle")
    i.add_argument("--home")
    i.add_argument("--force", action="store_true")
    i.add_argument("--password", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    return cmd_export(a) if a.cmd == "export" else cmd_import(a)


if __name__ == "__main__":
    sys.exit(main())
