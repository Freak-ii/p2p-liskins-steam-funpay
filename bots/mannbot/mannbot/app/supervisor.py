"""Supervisor: управляет жизненным циклом бота внутри приложения (запуск/остановка/перезапуск,
сохранение настроек, проверка подключений). База и CRM доступны всегда, даже когда бот остановлен."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import secrets
import time
from collections import deque
from pathlib import Path
from typing import Any

import yaml

from mannbot.analytics.analytics import Analytics
from mannbot.config import DEFAULTS, Section, load_config, validate_config
from mannbot.coord import Coordinator
from mannbot.core import messages as M
from mannbot.core.engine import Engine
from mannbot.crm.db import Database
from mannbot.notify.notifier import Notifier

log = logging.getLogger("app")

SECRET_FIELDS = {("funpay", "golden_key"), ("steam", "password"), ("steam", "shared_secret"),
                 ("steam", "identity_secret"), ("steam", "login_secure_cookie"), ("steam", "api_key"),
                 ("notifications.telegram", "bot_token"), ("dashboard", "token")}


class MemoryLogHandler(logging.Handler):
    """Последние строки журнала для страницы «Журнал»."""

    def __init__(self, capacity: int = 1500):
        super().__init__()
        self.lines: deque[dict] = deque(maxlen=capacity)
        self.seq = 0
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)-8s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.seq += 1
            self.lines.append({"n": self.seq, "level": record.levelname, "text": self.format(record)})
        except Exception:  # noqa: BLE001
            pass


class Supervisor:
    def __init__(self, home: Path, *, demo: bool = False, overrides: dict | None = None):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.config_path = self.home / "config.yaml"
        self.demo = demo
        self.overrides = overrides or {}
        self.raw: dict = self._read_raw()
        self.cfg: Section = self._load()
        db_path = self.cfg.general.database
        if db_path != ":memory:" and not Path(db_path).is_absolute():
            db_path = str(self.home / db_path)
        self.db = Database(db_path)
        self.state = "stopped"          # stopped | starting | running | error
        self.last_error = ""
        self.started_at: float | None = None
        self.restart_required = False
        self.task: asyncio.Task | None = None
        self._started_once = False
        self.quit_event: asyncio.Event | None = None     # выставляется кнопкой «Выход» в интерфейсе
        self.engine: Engine
        self.analytics: Analytics
        self.notifier: Notifier
        self._build()

    # ------------------------------------------------------------------ config
    def _read_raw(self) -> dict:
        if self.config_path.exists():
            return yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        return {}

    def _load(self) -> Section:
        cfg = load_config(self.config_path if self.config_path.exists() else None, self.overrides)
        if cfg.general.log_file and not Path(cfg.general.log_file).is_absolute():
            cfg.general.log_file = str(self.home / cfg.general.log_file)
        M.set_overrides(cfg.get("messages"))
        return cfg

    def ensure_token(self) -> str:
        token = str(self.cfg.dashboard.token or "")
        if len(token) < 12:
            token = secrets.token_urlsafe(18)
            self.save({"dashboard": {"token": token}}, rebuild=False)
        return token

    @property
    def problems(self) -> list[str]:
        return validate_config(self.cfg)

    @property
    def configured(self) -> bool:
        return not [p for p in self.problems if not p.startswith("dashboard")]

    def save(self, updates: dict, *, rebuild: bool = True) -> None:
        """Сливает изменения в config.yaml. Пустая строка в секретном поле = «не менять»."""
        raw = copy.deepcopy(self.raw)

        def merge(dst: dict, src: dict, path: str = "") -> None:
            for k, v in src.items():
                p = f"{path}.{k}" if path else k
                if isinstance(v, dict) and k != "messages":
                    dst.setdefault(k, {})
                    if not isinstance(dst[k], dict):
                        dst[k] = {}
                    merge(dst[k], v, p)
                else:
                    if (path, k) in SECRET_FIELDS and v in ("", None):
                        continue
                    dst[k] = v
        merge(raw, updates)
        if "messages" in updates:
            raw["messages"] = updates["messages"]
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text("# Настройки MannBot (редактируются в приложении)\n" +
                       yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
        tmp.replace(self.config_path)
        self.raw = raw
        self.cfg = self._load()
        if rebuild:
            if self.state == "running":
                self.restart_required = True
            else:
                self._build()

    def secret_is_set(self, section: str, key: str) -> bool:
        node: Any = self.cfg
        for part in section.split("."):
            node = node.get(part, {})
        return bool(node.get(key))

    # ------------------------------------------------------------------ build / run
    def _build(self) -> None:
        cfg = self.cfg
        self.notifier = Notifier(cfg.notifications, self.db)
        # FUNPAY_COORD_DB задаёт Центр, когда на том же аккаунте FunPay работает StarsBot
        coord = Coordinator.from_env("mannbot", cfg.funpay.get("coord_db") or None)
        if cfg.general.demo or self.demo:
            from mannbot.demo import FakeFunPay, FakeSteam
            fp, steam = FakeFunPay(), FakeSteam(keys=60)
        else:
            from mannbot.funpay.client import FunPayClient
            from mannbot.steam.client import SteamClient
            fp = FunPayClient(cfg.funpay.golden_key, cfg.funpay.user_agent, dry_run=cfg.general.dry_run,
                              coordinator=coord)
            steam = SteamClient(cfg.steam, dry_run=cfg.general.dry_run)
        self.engine = Engine(cfg, self.db, fp, steam, self.notifier, coord=coord)
        self.analytics = Analytics(cfg, self.db, self.notifier, self.engine)
        self.engine.analytics = self.analytics

    async def start(self) -> str:
        if self.state in ("running", "starting"):
            return "Бот уже запущен"
        problems = [p for p in self.problems if not p.startswith("dashboard")]
        if problems and not (self.cfg.general.demo or self.demo):
            return "Сначала заполните настройки: " + "; ".join(problems)
        if not (self.demo and not self._started_once):   # в демо сохраняем данные эмулятора после сценария
            self._build()
        self._started_once = True
        self.restart_required = False
        self.state, self.last_error, self.started_at = "starting", "", time.time()
        self.task = asyncio.create_task(self._run())
        return "Бот запускается…"

    async def _run(self) -> None:
        try:
            starter = asyncio.create_task(self.engine.start())
            # engine.start() сначала логинится, потом крутит циклы бесконечно
            while not starter.done() and not self.engine._tasks:
                await asyncio.sleep(0.2)
            if not starter.done():
                self.state = "running"
                log.info("Бот запущен")
            await starter
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.state, self.last_error = "error", f"{type(e).__name__}: {e}"
            log.exception("Бот остановился с ошибкой")
            await self.notifier.notify("CRITICAL", "bot_crash", f"Бот остановился с ошибкой: {e}")
        finally:
            await self._close_clients()

    async def stop(self) -> str:
        if self.task and not self.task.done():
            await self.engine.stop()
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self._close_clients()
        if self.state != "error":
            self.state = "stopped"
        self.started_at = None
        log.info("Бот остановлен")
        self._build()
        return "Бот остановлен"

    async def restart(self) -> str:
        await self.stop()
        self.state = "stopped"
        return await self.start()

    async def _close_clients(self) -> None:
        for c in (self.engine.fp, self.engine.steam, self.notifier):
            try:
                await c.close()
            except Exception:  # noqa: BLE001
                pass

    async def shutdown(self) -> None:
        await self.stop()

    def status(self) -> dict:
        return {"state": self.state, "error": self.last_error, "configured": self.configured,
                "restart_required": self.restart_required, "dry_run": bool(self.cfg.general.dry_run),
                "paused": self.engine.paused, "uptime": (time.time() - self.started_at) if self.started_at else 0,
                "stock": self.db.kv_get("last_stock"), "demo": bool(self.cfg.general.demo or self.demo)}

    # ------------------------------------------------------------------ connection tests
    async def run_test(self, coro, timeout: float = 45) -> dict:
        try:
            return await asyncio.wait_for(coro, timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "text": f"Нет ответа за {timeout:.0f} с — проверьте интернет/доступ к сайту"}

    async def test_funpay(self, golden_key: str | None = None) -> dict:
        from mannbot.funpay.client import FunPayClient
        fp = FunPayClient(golden_key or self.cfg.funpay.golden_key, self.cfg.funpay.user_agent, dry_run=True)
        try:
            await fp.login()
            sales = await fp.get_sales(state="paid")
            return {"ok": True, "text": f"Вход выполнен: {fp.username} (id {fp.user_id}). "
                                        f"Оплаченных заказов сейчас: {len(sales)}."}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "text": f"Не удалось войти на FunPay: {e}"}
        finally:
            await fp.close()

    async def test_steam(self, overrides: dict | None = None) -> dict:
        from mannbot.steam.client import SteamClient
        st = Section({**self.cfg.steam, **{k: v for k, v in (overrides or {}).items() if v}})
        client = SteamClient(st, dry_run=True)
        try:
            await client.login()
            keys = await client.get_tradable_keys()
            conf = ""
            if st.identity_secret:
                try:
                    n = await client.pending_confirmations()
                    conf = f" Подтверждения Steam Guard доступны (ожидают: {n})."
                except Exception as e:  # noqa: BLE001
                    conf = f" ⚠ Подтверждения недоступны: {e}"
            return {"ok": True, "text": f"Steam: {client.persona_name} ({client.steam_id}), уровень {client.level}. "
                                        f"Обмениваемых ключей: {len(keys)}.{conf}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "text": f"Steam: {e}"}
        finally:
            await client.close()

    async def test_telegram(self, token: str | None = None, chat_ids: list | None = None) -> dict:
        tg = Section({**self.cfg.notifications.telegram, "enabled": True})
        if token:
            tg["bot_token"] = token
        if chat_ids:
            tg["admin_chat_ids"] = chat_ids
        if not tg.bot_token or not tg.admin_chat_ids:
            return {"ok": False, "text": "Укажите токен бота и chat_id"}
        n = Notifier(Section({**self.cfg.notifications, "telegram": tg}), self.db)
        try:
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                for cid in tg.admin_chat_ids:
                    async with s.post(f"https://api.telegram.org/bot{tg.bot_token}/sendMessage",
                                      json={"chat_id": cid, "text": "✅ MannBot: уведомления подключены"}) as r:
                        data = await r.json()
                        if not data.get("ok"):
                            return {"ok": False, "text": f"Telegram: {data.get('description')}"}
            return {"ok": True, "text": "Тестовое сообщение отправлено в Telegram"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "text": f"Telegram недоступен: {e}"}
        finally:
            await n.close()


def parse_mafile(content: bytes | str) -> dict:
    """maFile (Steam Desktop Authenticator) → поля настроек Steam."""
    data = json.loads(content)
    out = {"shared_secret": data.get("shared_secret", ""), "identity_secret": data.get("identity_secret", "")}
    if data.get("account_name"):
        out["username"] = data["account_name"]
    sid = (data.get("Session") or {}).get("SteamID") or data.get("steamid")
    if sid:
        out["steam_id"] = str(sid)
    if not out["shared_secret"] or not out["identity_secret"]:
        raise ValueError("в maFile нет shared_secret/identity_secret (файл зашифрован? отключите шифрование в SDA)")
    return out


def default_config_dict() -> dict:
    return copy.deepcopy(DEFAULTS)
