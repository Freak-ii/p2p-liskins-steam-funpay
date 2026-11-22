"""Один аккаунт Steam на оба бота: реестр исходящих обменов (center-data/steam-share.sqlite3).

LisBot считает взломом любой исходящий обмен, который создал не он. Когда LisBot и MannBot торгуют
с одного аккаунта, MannBot перед отправкой ключей покупателю записывает сюда, кому и какие предметы
уйдут (намерение), а после отправки добавляет номер обмена. LisBot спрашивает центр о каждом чужом
исходящем обмене: «свой» только тот, у которого совпали получатель и РОВНО записанные предметы
и от нас ничего не просят взамен. Всё остальное по-прежнему тревога в LisBot.

Боты обращаются сюда по отдельному токену (не токену панели): он передаётся им при запуске
в CENTER_STEAM_TOKEN, адрес центра в CENTER_STEAM_URL.
"""
from __future__ import annotations

import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from aiohttp import web

INTENT_TTL = 30 * 60          # намерение без номера обмена живёт столько секунд
KEEP_DAYS = 30                # записанные обмены храним столько дней
BOTS = ("mannbot", "lisbot")
STEAM64_BASE = 76561197960265728

SCHEMA = """
CREATE TABLE IF NOT EXISTS outgoing (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    bot TEXT NOT NULL,
    ref TEXT NOT NULL,            -- номер заказа FunPay или другой ссылки бота
    partner TEXT NOT NULL,        -- SteamID64 получателя
    appid INTEGER NOT NULL,
    assets TEXT NOT NULL,         -- JSON-список assetid, отсортированный
    offer_id TEXT,                -- номер обмена Steam, когда известен
    bound_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_out_offer ON outgoing(offer_id);
"""


def partner_steam64(trade_url: str) -> str:
    m = re.search(r"[?&]partner=(\d+)", trade_url or "")
    return str(STEAM64_BASE + int(m.group(1))) if m else ""


def lis_steam64(center) -> str:
    """SteamID64 аккаунта LisBot по его трейд-ссылке из подключений (пусто, если не задана)."""
    try:
        return partner_steam64(center.workers["lisbot"].secrets().get("STEAM_TRADE_URL", ""))
    except Exception:  # noqa: BLE001 — хранилище недоступно
        return ""


def bridge_same_account(bc: dict, lis_id: str) -> bool:
    """Мост ключей без обмена: трейд-ссылки нет или она ведёт на сам аккаунт LisBot (MannBot там же)."""
    url = str((bc or {}).get("tradeUrl") or "").strip()
    return not url or (bool(lis_id) and partner_steam64(url) == lis_id)


def _steam64(v) -> str:
    s = str(v or "").strip()
    if not s.isdigit() or len(s) != 17 or not s.startswith("7656119"):
        raise ValueError("partner должен быть SteamID64")
    return s


def _assets(v) -> list[str]:
    if not isinstance(v, list) or not v or len(v) > 200:
        raise ValueError("assets: нужен непустой список assetid")
    out = sorted({str(a).strip() for a in v})
    if any(not a.isdigit() for a in out) or len(out) != len(v):
        raise ValueError("assets: assetid должны быть разными числами")
    return out


class SteamShare:
    def __init__(self, center):
        self.center = center
        self.path = Path(center.home) / "steam-share.sqlite3"
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ------------------------------------------------------------------ настройки
    @property
    def cfg(self) -> dict:
        return self.center.cfg["steam"]

    @property
    def shared(self) -> bool:
        """Оба бота на одном аккаунте Steam (center.yaml steam.shared_account, по умолчанию да)."""
        return bool(self.cfg.get("shared_account", True))

    def token(self) -> str:
        """Токен для ботов. Хранится в зашифрованном хранилище центра, создаётся при первом запуске."""
        t = self.center.vault.get("center.steam_share_token") or ""
        if len(t) < 32:
            t = secrets.token_hex(24)
            self.center.vault.set("center.steam_share_token", t)
        return t

    def bot_env(self) -> dict[str, str]:
        c = self.center.cfg["center"]
        # адрес, на котором панель центра реально слушает (--host/--port важнее center.yaml)
        host, port = getattr(self.center, "listen", None) or (c.get("host"), c.get("port"))
        host = host or "127.0.0.1"
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        return {"CENTER_STEAM_URL": f"http://{host}:{int(port or 8800)}/api/steam-share",
                "CENTER_STEAM_TOKEN": self.token(), "CENTER_STEAM_SHARED": "1" if self.shared else "0"}

    # ------------------------------------------------------------------ реестр
    def _cleanup(self) -> None:
        now = time.time()
        self.db.execute("DELETE FROM outgoing WHERE (offer_id IS NULL AND ts < ?) OR ts < ?",
                        (now - INTENT_TTL, now - KEEP_DAYS * 86400))

    def intent(self, bot: str, ref, partner, appid, assets) -> int:
        if bot not in BOTS:
            raise ValueError("неизвестный бот")
        p, a = _steam64(partner), _assets(assets)
        with self._lock:
            self._cleanup()
            cur = self.db.execute("INSERT INTO outgoing(ts,bot,ref,partner,appid,assets) VALUES(?,?,?,?,?,?)",
                                  (time.time(), bot, str(ref)[:64], p, int(appid), json.dumps(a)))
            self.db.commit()
            return int(cur.lastrowid)

    def bind(self, bot: str, intent_id: int, offer_id) -> bool:
        oid = str(offer_id or "").strip()
        if not oid.isdigit():
            raise ValueError("offer_id должен быть числом")
        with self._lock:
            cur = self.db.execute("UPDATE outgoing SET offer_id=?, bound_ts=? WHERE id=? AND bot=? "
                                  "AND (offer_id IS NULL OR offer_id=?)",
                                  (oid, time.time(), int(intent_id), bot, oid))
            self.db.commit()
            return cur.rowcount == 1

    def check(self, offer_id, partner, give: list, receive_count: int) -> dict:
        """Записан ли этот исходящий обмен другим ботом центра. give — [{appid, assetid}] из обмена."""
        oid = str(offer_id or "").strip()
        try:
            p = _steam64(partner)
        except ValueError:
            return {"own": False, "reason": "partner"}
        if int(receive_count or 0) or not isinstance(give, list) or not give:
            return {"own": False, "reason": "items"}
        appids = {int(i.get("appid") or 0) for i in give if isinstance(i, dict)}
        assets = sorted(str(i.get("assetid") or "") for i in give if isinstance(i, dict))
        if len(appids) != 1 or len(assets) != len(give) or len(set(assets)) != len(assets):
            return {"own": False, "reason": "items"}
        appid = appids.pop()
        with self._lock:
            self._cleanup()
            rows = self.db.execute(
                "SELECT * FROM outgoing WHERE partner=? AND appid=? AND assets=? AND (offer_id=? OR offer_id IS NULL) "
                "ORDER BY offer_id IS NULL, id DESC", (p, appid, json.dumps(assets), oid)).fetchall()
            if not rows:
                return {"own": False, "reason": "not_registered"}
            r = rows[0]
            if r["offer_id"] is None and oid.isdigit():   # обмен уже создан, а MannBot ещё не прислал номер
                self.db.execute("UPDATE outgoing SET offer_id=?, bound_ts=? WHERE id=?", (oid, time.time(), r["id"]))
                self.db.commit()
            return {"own": True, "bot": r["bot"], "ref": r["ref"]}

    def recent(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self.db.execute("SELECT * FROM outgoing ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [{**dict(r), "assets": json.loads(r["assets"])} for r in rows]

    def status(self) -> dict:
        return {"shared_account": self.shared, "recent": self.recent(20)}

    def close(self) -> None:
        self.db.close()


def routes(center) -> list:
    share: SteamShare = center.steamshare

    def bot_of(request: web.Request) -> str:
        given = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not given or not hmac.compare_digest(given, share.token()):
            raise web.HTTPUnauthorized(text="нужен токен центра для ботов")
        bot = request.headers.get("X-Bot", "")
        if bot not in BOTS:
            raise web.HTTPBadRequest(text="X-Bot: mannbot или lisbot")
        return bot

    async def body(request: web.Request) -> dict:
        try:
            d = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise web.HTTPBadRequest(text="нужен JSON")
        if not isinstance(d, dict):
            raise web.HTTPBadRequest(text="нужен JSON-объект")
        return d

    async def intent(request: web.Request):
        bot = bot_of(request)
        d = await body(request)
        try:
            iid = share.intent(bot, d.get("ref", ""), d.get("partner"), d.get("appid") or 0, d.get("assets"))
        except (ValueError, TypeError) as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        return web.json_response({"ok": True, "id": iid, "shared_account": share.shared})

    async def bind(request: web.Request):
        bot = bot_of(request)
        d = await body(request)
        try:
            ok = share.bind(bot, int(d.get("id") or 0), d.get("offer_id"))
        except (ValueError, TypeError) as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        return web.json_response({"ok": ok})

    async def check(request: web.Request):
        bot_of(request)
        d = await body(request)
        return web.json_response(share.check(d.get("offer_id"), d.get("partner"), d.get("give") or [],
                                             int(d.get("receive_count") or 0)))

    async def status(request: web.Request):
        return web.json_response(share.status())

    return [web.post("/api/steam-share/intent", intent), web.post("/api/steam-share/bind", bind),
            web.post("/api/steam-share/check", check), web.get("/api/steam-status", status)]
