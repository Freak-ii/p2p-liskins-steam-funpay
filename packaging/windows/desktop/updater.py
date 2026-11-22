"""Автообновление Windows-версии через GitHub Releases.

Релизы публикует workflow «Build Windows» по тегу center-v<версия>: в релизе лежат
StarsCenter-<версия>-setup.exe и SHA256SUMS.txt. Репозиторий приватный, поэтому для проверки нужен
токен GitHub только на чтение (fine-grained, Contents: Read) в файле updates.json рядом с center-data:

    %LOCALAPPDATA%\\StarsCenter\\updates.json   {"repo": "Freak-ii/p2p-liskins-steam-funpay", "token": "github_pat_…"}

Без токена (или с "enabled": false) обновления просто не проверяются.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEFAULT_REPO = "Freak-ii/p2p-liskins-steam-funpay"
TAG_PREFIX = "center-v"
CHECK_EVERY = 24 * 3600
API = "https://api.github.com"


def parse_version(v: str) -> tuple[int, ...]:
    v = v.strip()
    if v.startswith(TAG_PREFIX):
        v = v[len(TAG_PREFIX):]
    v = v.lstrip("vV")
    m = re.match(r"^(\d+(?:\.\d+)*)", v)
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def is_newer(candidate: str, current: str) -> bool:
    c, cur = parse_version(candidate), parse_version(current)
    if not c:
        return False
    n = max(len(c), len(cur))
    return c + (0,) * (n - len(c)) > cur + (0,) * (n - len(cur))


@dataclass
class Update:
    version: str
    tag: str
    asset_name: str
    asset_api_url: str
    size: int
    sha256: str = ""
    notes: str = ""


class Settings:
    def __init__(self, path: Path):
        self.path = path
        try:
            raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError):
            raw = {}
        self.raw: dict = raw if isinstance(raw, dict) else {}

    @property
    def repo(self) -> str:
        return str(self.raw.get("repo") or DEFAULT_REPO)

    @property
    def token(self) -> str:
        return str(self.raw.get("token") or os.environ.get("STARSCENTER_GITHUB_TOKEN") or "")

    @property
    def enabled(self) -> bool:
        return bool(self.raw.get("enabled", True)) and bool(self.token)

    @property
    def last_check(self) -> float:
        return float(self.raw.get("last_check") or 0)

    def due(self, now: float | None = None) -> bool:
        return self.enabled and (now or time.time()) - self.last_check >= CHECK_EVERY

    def save(self, **updates) -> None:
        self.raw.update(updates)
        if "token" not in self.raw:
            self.raw["token"] = ""
        self.raw.setdefault("repo", DEFAULT_REPO)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.raw, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def _open(url: str, token: str, accept: str, timeout: float = 30):
    """GET с токеном; редиректы на другой хост (S3 с подписанной ссылкой) — уже без Authorization."""
    opener = urllib.request.build_opener(_NoRedirect)
    host = urllib.parse.urlsplit(url).netloc
    for _ in range(5):
        headers = {"Accept": accept, "User-Agent": "StarsCenter-updater", "X-GitHub-Api-Version": "2022-11-28"}
        if token and urllib.parse.urlsplit(url).netloc == host:
            headers["Authorization"] = f"Bearer {token}"
        try:
            return opener.open(urllib.request.Request(url, headers=headers), timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                url = urllib.parse.urljoin(url, e.headers["Location"])
                continue
            raise
    raise OSError("слишком много перенаправлений")


def pick_update(release: dict, current: str) -> Update | None:
    """Из ответа /releases/latest выбирает setup.exe, если версия новее текущей."""
    tag = str(release.get("tag_name") or "")
    if release.get("draft") or release.get("prerelease") or not tag.startswith(TAG_PREFIX):
        return None
    version = tag[len(TAG_PREFIX):]
    if not is_newer(version, current):
        return None
    assets = release.get("assets") or []
    setup = next((a for a in assets if re.fullmatch(r"StarsCenter-.+-setup\.exe", str(a.get("name") or ""))), None)
    if not setup:
        return None
    return Update(version=version, tag=tag, asset_name=setup["name"], asset_api_url=setup["url"],
                  size=int(setup.get("size") or 0), notes=str(release.get("body") or "")[:2000])


def parse_sums(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            out[parts[-1].lstrip("*")] = parts[0].lower()
    return out


def check(settings: Settings, current: str) -> Update | None:
    """Проверяет последний релиз. Бросает OSError/ValueError при сетевых ошибках и плохом токене."""
    with _open(f"{API}/repos/{settings.repo}/releases/latest", settings.token, "application/vnd.github+json") as r:
        release = json.load(r)
    settings.save(last_check=time.time())
    upd = pick_update(release, current)
    if upd:
        sums = next((a for a in release.get("assets") or [] if a.get("name") == "SHA256SUMS.txt"), None)
        if sums:
            with _open(sums["url"], settings.token, "application/octet-stream") as r:
                upd.sha256 = parse_sums(r.read().decode("utf-8", "replace")).get(upd.asset_name, "")
    return upd


def download(settings: Settings, upd: Update, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / upd.asset_name
    part = dest.with_suffix(".part")
    h = hashlib.sha256()
    size = 0
    with _open(upd.asset_api_url, settings.token, "application/octet-stream", timeout=120) as r, open(part, "wb") as f:
        while chunk := r.read(1 << 16):
            f.write(chunk)
            h.update(chunk)
            size += len(chunk)
    if upd.size and size != upd.size:
        part.unlink(missing_ok=True)
        raise ValueError(f"файл обновления скачан не полностью ({size} из {upd.size} байт)")
    if not upd.sha256:
        part.unlink(missing_ok=True)
        raise ValueError("в релизе нет SHA256SUMS.txt — обновление не проверить, установка отменена")
    if h.hexdigest() != upd.sha256:
        part.unlink(missing_ok=True)
        raise ValueError("контрольная сумма обновления не совпала — файл повреждён или подменён")
    os.replace(part, dest)
    return dest


def installer_command(setup: Path) -> list[str]:
    # /RELAUNCH — свой флаг установщика: после тихой установки снова запустить центр
    return [str(setup), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER", "/RELAUNCH"]
