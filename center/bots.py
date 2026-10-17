"""Адаптеры ботов: как запустить каждого бота, как понять, что он жив, как получить его сводку,
тревоги и как передать ему команду. Торговая логика ботов не трогается: центр общается с ними
через их собственные веб-API и читает их базы только на чтение.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import sys
from pathlib import Path
from urllib.parse import unquote_plus

import aiohttp
import yaml

from center.config import BOTS_DIR
from center.workers import MISSING_DEPS, NOT_CONFIGURED, STOPPED, Worker

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=8)


def _read_only(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    con.row_factory = sqlite3.Row
    return con


class BotWorker(Worker):
    """Общее для ботов с веб-панелью на своём порту."""

    def __init__(self, home: Path, events, *, port: int, restart: dict | None = None, executable: str = ""):
        super().__init__(home, events, restart=restart)
        self.port = int(port)
        self.executable = executable
        self.home.mkdir(parents=True, exist_ok=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def prepare(self) -> None:
        # если порт занят чужой программой — берём следующий свободный, чтобы бот и проверки смотрели в одно место
        for p in range(self.port, self.port + 50):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("127.0.0.1", p))
                except OSError:
                    continue
            if p != self.port:
                self._append(f"порт {self.port} занят, бот запускается на {p}", "center")
                self.port = p
            return

    def token(self) -> str:
        raise NotImplementedError

    def panel_url(self) -> str:
        raise NotImplementedError

    async def summary(self) -> dict | None:
        return None

    def alerts(self, limit: int = 50) -> list[dict]:
        return []

    async def control(self, action: str) -> str:
        raise NotImplementedError

    async def ack_alert(self, alert_id: int | None) -> bool:
        """Подтвердить тревогу в самом боте (alert_id=None — все). Базы ботов центр не меняет."""
        raise NotImplementedError

    actions: dict[str, str] = {}

    def info(self) -> dict:
        return {**super().info(), "port": self.port, "actions": self.actions}


# ====================================================================== MannBot
class MannBotWorker(BotWorker):
    """MannBot (Python): запускается своим лаунчером в режиме приложения, без браузера."""

    name = "mannbot"
    title = "MannBot · FunPay"
    src = BOTS_DIR / "mannbot"
    actions = {"bot_start": "Запустить торговлю", "bot_stop": "Остановить торговлю",
               "pause": "Пауза", "resume": "Продолжить"}

    def __init__(self, *a, demo: bool = False, **kw):
        super().__init__(*a, **kw)
        self.demo = demo

    def check_deps(self) -> str | None:
        if not (self.src / "mannbot").is_dir():
            return f"нет исходников MannBot в {self.src}"
        try:
            import aiohttp as _a, bs4 as _b, jinja2 as _j, yaml as _y  # noqa: F401
        except ImportError as e:
            return f"не установлены Python-пакеты ({e.name}): pip install -r requirements.txt"
        return None

    def command(self) -> list[str]:
        py = self.executable or sys.executable
        cmd = [py, "-m", "mannbot.app.launcher", "--home", str(self.home), "--port", str(self.port), "--no-browser"]
        if self.demo:
            cmd.append("--demo")
        return cmd

    def cwd(self) -> Path:
        return self.src

    def env(self) -> dict[str, str]:
        return {"PYTHONPATH": str(self.src) + os.pathsep + os.environ.get("PYTHONPATH", "")}

    @property
    def _cfg_path(self) -> Path:
        return self.home / ("demo" if self.demo else "") / "config.yaml"

    def token(self) -> str:
        try:
            raw = yaml.safe_load(self._cfg_path.read_text(encoding="utf-8")) or {}
            return str((raw.get("dashboard") or {}).get("token") or "")
        except (OSError, yaml.YAMLError):
            return ""

    def panel_url(self) -> str:
        return f"{self.base_url}/login?token={self.token()}"

    async def health(self) -> bool:
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as s:
            async with s.get(f"{self.base_url}/api/ping") as r:
                return r.status == 200 and "mannbot" in await r.text()

    def _cookies(self) -> dict:
        return {"mb_token": self.token()}

    async def summary(self) -> dict | None:
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT, cookies=self._cookies()) as s:
            async with s.get(f"{self.base_url}/api/status", allow_redirects=False) as r:
                if r.status != 200:
                    return None
                d = await r.json()
        sm = d.get("summary") or {}
        return {
            "bot_state": d.get("state"), "paused": d.get("paused"), "dry_run": d.get("dry_run"),
            "configured": d.get("configured"), "error": d.get("error"), "demo": d.get("demo"),
            "kpi": {"Заказов за 24 ч": sm.get("orders"), "Выдано": sm.get("delivered"),
                    "Выручка за 24 ч, ₽": sm.get("revenue"), "Прибыль за 24 ч, ₽": sm.get("profit"),
                    "Активных заказов": sm.get("active"), "Ключей на складе": sm.get("stock")},
        }

    async def control(self, action: str) -> str:
        form = {"bot_start": "start", "bot_stop": "stop", "pause": "pause", "resume": "resume"}.get(action)
        if not form:
            return "неизвестное действие"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60), cookies=self._cookies()) as s:
            async with s.post(f"{self.base_url}/control", data={"action": form}, allow_redirects=False) as r:
                loc = r.headers.get("Location", "")
        m = re.search(r"msg=([^&]+)", loc)
        return unquote_plus(m.group(1)) if m else "команда отправлена"

    def alerts(self, limit: int = 50) -> list[dict]:
        con = _read_only(self.home / ("demo" if self.demo else "") / "data" / "mannbot.sqlite3")
        if con is None:
            return []
        try:
            rows = con.execute("SELECT id, ts, level, code, text, acknowledged FROM alerts ORDER BY id DESC LIMIT ?",
                               (limit,)).fetchall()
        except sqlite3.Error:
            return []
        finally:
            con.close()
        return [{"source": self.name, "id": r["id"], "ts": float(r["ts"] or 0), "level": str(r["level"] or "").lower(),
                 "code": r["code"], "message": r["text"], "acked": bool(r["acknowledged"])} for r in rows]

    async def ack_alert(self, alert_id: int | None) -> bool:
        body = {"all": True} if alert_id is None else {"id": int(alert_id)}
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT, cookies=self._cookies()) as s:
            async with s.post(f"{self.base_url}/api/alerts/ack", json=body, allow_redirects=False) as r:
                if r.status == 200:
                    return True
            if r.status == 404 and alert_id is None:      # старая версия MannBot: только форма «все прочитаны»
                async with s.post(f"{self.base_url}/alerts/ack", allow_redirects=False) as r2:
                    return r2.status in (200, 302)
        return False


# ====================================================================== LisBot
LIS_SECRET_KEYS = ["LIS_API_KEY", "STEAM_ACCOUNT_NAME", "STEAM_PASSWORD", "STEAM_SHARED_SECRET",
                   "STEAM_IDENTITY_SECRET", "STEAM_TRADE_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def write_env_file(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


class LisBotWorker(BotWorker):
    """LisBot (Node.js): ядро бота src/index.js без Electron-оболочки.

    Секреты лежат в <home>/secrets.env и передаются боту переменными окружения, настройки стратегии —
    в <home>/config.json (их же меняет вкладка «Настройки» панели LisBot), база и логи — в <home>/data.
    """

    name = "lisbot"
    title = "LisBot · LIS-SKINS → Steam"
    src = BOTS_DIR / "lisbot"
    actions = {"pause": "Пауза", "resume": "Продолжить / снять остановку", "buyPause": "Пауза закупки",
               "buyResume": "Возобновить закупку", "halt": "Аварийная остановка"}

    @property
    def secrets_path(self) -> Path:
        return self.home / "secrets.env"

    @property
    def config_path(self) -> Path:
        return self.home / "config.json"

    @property
    def data_dir(self) -> Path:
        return self.home / "data"

    def node(self) -> str | None:
        return self.executable or shutil.which("node")

    def check_deps(self) -> str | None:
        if not self.node():
            return "не найден Node.js 20+ (https://nodejs.org)"
        if not (self.src / "node_modules" / "better-sqlite3").is_dir():
            return "не установлены зависимости LisBot: нажмите «Установить зависимости» или выполните npm ci в bots/lisbot"
        return None

    def secrets(self) -> dict[str, str]:
        s = read_env_file(self.secrets_path)
        if len(s.get("DASHBOARD_TOKEN", "")) < 16:
            s["DASHBOARD_TOKEN"] = secrets.token_hex(24)
            write_env_file(self.secrets_path, s)
        return s

    def save_secrets(self, values: dict[str, str]) -> None:
        s = self.secrets()
        for k in LIS_SECRET_KEYS:
            v = values.get(k)
            if v is None or (k == "STEAM_PASSWORD" and v == ""):
                continue                                        # пустой пароль = «не менять»
            s[k] = str(v).strip()
        write_env_file(self.secrets_path, s)

    def masked_secrets(self) -> dict:
        s = self.secrets()
        return {k: ("" if k == "STEAM_PASSWORD" else s.get(k, "")) for k in LIS_SECRET_KEYS} | {
            "STEAM_PASSWORD_SET": bool(s.get("STEAM_PASSWORD"))}

    def prepare(self) -> None:
        super().prepare()
        user = {}
        if self.config_path.exists():
            try:
                user = json.loads(self.config_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                user = {}
        user["dataDir"] = str(self.data_dir)
        user.setdefault("dashboard", {})
        user["dashboard"].update({"enabled": True, "host": "127.0.0.1", "port": self.port})
        # Telegram включается, только когда заданы токен и chat id — иначе бот не стартует из-за проверки настроек
        s = self.secrets()
        user.setdefault("telegram", {})["enabled"] = bool(s.get("TELEGRAM_BOT_TOKEN") and s.get("TELEGRAM_CHAT_ID"))
        self.config_path.write_text(json.dumps(user, ensure_ascii=False, indent=2), encoding="utf-8")
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def command(self) -> list[str]:
        return [self.node() or "node", str(self.src / "src" / "index.js")]

    def cwd(self) -> Path:
        return self.src

    def env(self) -> dict[str, str]:
        s = self.secrets()
        env = {k: s.get(k, "") for k in LIS_SECRET_KEYS}
        env["DASHBOARD_TOKEN"] = s["DASHBOARD_TOKEN"]
        env["BOT_CONFIG"] = str(self.config_path)
        return env

    def classify_exit(self, code: int | None, tail: str) -> tuple[str, str] | None:
        if code == 1 and "Ошибки конфигурации" in tail:
            problems = [ln.strip(" -") for ln in tail.splitlines() if ln.strip().startswith("- ")]
            return NOT_CONFIGURED, "; ".join(problems) or "не заполнены настройки"
        if "Cannot find module" in tail or "ERR_MODULE_NOT_FOUND" in tail or "NODE_MODULE_VERSION" in tail:
            return MISSING_DEPS, "зависимости LisBot не установлены или собраны под другую версию Node: npm ci в bots/lisbot"
        return None

    def token(self) -> str:
        return read_env_file(self.secrets_path).get("DASHBOARD_TOKEN", "")

    def panel_url(self) -> str:
        return f"{self.base_url}/#token={self.token()}"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token()}"}

    async def health(self) -> bool:
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT, headers=self._headers()) as s:
            async with s.get(f"{self.base_url}/api/meta") as r:
                return r.status == 200

    async def summary(self) -> dict | None:
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT, headers=self._headers()) as s:
            async with s.get(f"{self.base_url}/api/summary") as r:
                if r.status != 200:
                    return None
                d = await r.json()
        g = d.get("guard") or {}
        today = d.get("today") or {}
        sold = d.get("sold") or {}
        state = "halted" if g.get("halted") else "paused" if g.get("paused") else "running"
        return {
            "bot_state": state, "paused": bool(g.get("paused")), "dry_run": d.get("dryRun"),
            "error": g.get("haltReason") or "", "configured": True,
            "flags": [t for k, t in (("buyPaused", "закупка на паузе"), ("sellPaused", "продажа на паузе")) if g.get(k)],
            "kpi": {"Баланс LIS, $": d.get("balance"), "В товаре, $": d.get("exposure"),
                    "Куплено сегодня": today.get("bought"), "Потрачено сегодня, $": today.get("spent"),
                    "Прибыль сегодня": today.get("profit"), "Продано всего": sold.get("n"),
                    "Прибыль всего": sold.get("profit")},
        }

    async def control(self, action: str) -> str:
        if action not in self.actions:
            return "неизвестное действие"
        cmd = "stop" if action == "halt" else action          # "stop" у центра — остановка процесса
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT, headers=self._headers()) as s:
            async with s.post(f"{self.base_url}/api/control", json={"action": cmd}) as r:
                d = await r.json(content_type=None)
        return "готово" if d.get("ok") else str(d.get("error") or "ошибка")

    def alerts(self, limit: int = 50) -> list[dict]:
        con = _read_only(self.data_dir / "bot.sqlite")
        if con is None:
            return []
        try:
            rows = con.execute("SELECT id, ts, level, code, message, acked FROM alerts ORDER BY id DESC LIMIT ?",
                               (limit,)).fetchall()
        except sqlite3.Error:
            return []
        finally:
            con.close()
        return [{"source": self.name, "id": r["id"], "ts": (r["ts"] or 0) / 1000, "level": str(r["level"] or "").lower(),
                 "code": r["code"], "message": r["message"], "acked": bool(r["acked"])} for r in rows]

    async def ack_alert(self, alert_id: int | None) -> bool:
        body = {"all": True} if alert_id is None else {"id": int(alert_id)}
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT, headers=self._headers()) as s:
            async with s.post(f"{self.base_url}/api/alerts/ack", json=body) as r:
                return r.status == 200 and bool((await r.json(content_type=None)).get("ok"))

    async def install_deps(self) -> str:
        npm = shutil.which("npm")
        if not npm:
            return "не найден npm (ставится вместе с Node.js)"
        self._append("$ npm ci --omit=dev", "center")
        proc = await asyncio.create_subprocess_exec(
            npm, "ci", "--omit=dev", "--no-audit", "--no-fund", cwd=str(self.src),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            self._append(line.decode("utf-8", "replace").rstrip(), "npm")
        code = await proc.wait()
        if code == 0 and self.state == MISSING_DEPS:
            self._set(STOPPED, "")
        return "зависимости LisBot установлены" if code == 0 else f"npm завершился с кодом {code}, подробности в журнале"
