"""Сервисы: VPN-ключи (VLESS для Happ), подарочные карты App Store (США), подписки Claude/ChatGPT через карту
App Store, Spotify Premium на аккаунт покупателя, пополнение Steam по логину.

Магазин — посредник: продаёт товар, а закупает его у источника (source) подешевле:
  stock   — склад кодов/ключей, купленных заранее (оптом, за USDT). Выдача сразу, полностью автоматически.
  panel   — свой VPN-сервер с панелью Marzban или 3x-ui: бот сам создаёт ключ VLESS на нужный срок.
  resale  — перепродажа: бот ставит оператору задачу «купить на GGSEL/FunPay» (ссылка, вариант, данные
            покупателя), оператор присылает код/«готово» — бот выдаёт. Автопокупка через API подключается сюда же,
            когда будет проверенный канал (B2B g-engine от GGSEL, Bitrefill и т.п.).
Источник выбирается по себестоимости: самый дешёвый доступный, но автоматический (stock/panel) предпочитается,
если дороже не больше чем на prefer_auto_within_pct.

Доказательства для споров: до оплаты покупатель видит, КАК получит товар (notice), и жмёт «Понятно» — согласие
пишется в историю заказа (текст, хэш, время). Выданный код/ключ сохраняется в service_jobs и в событии заказа
(маской), повторно показывается в «Мои заказы». Логин/пароль покупателя шифруются (Fernet) и стираются после
выдачи или возврата.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import re
import time
import uuid
from typing import Any

import aiohttp

log = logging.getLogger("services")

KIND_FIELDS = {"code": [], "vpn": [], "account": ["login", "password"], "topup": ["login"]}
FIELD_ASK = {
    "login": "👤 Пришлите <b>логин</b> (почту или имя пользователя) от аккаунта {title}.",
    "password": "🔑 Пришлите <b>пароль</b> от аккаунта {title}.\nОн шифруется и удаляется сразу после выдачи. "
                "После получения подписки рекомендуем сменить пароль.",
}
STEAM_LOGIN_ASK = ("👤 Пришлите <b>логин Steam</b> — тот, что вводите при входе (не ник и не ссылку на профиль). "
                   "Пароль не нужен.")

SCHEMA = """
CREATE TABLE IF NOT EXISTS service_stock (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sku TEXT, code TEXT, cost_rub REAL DEFAULT 0, note TEXT DEFAULT '',
    added_at REAL, ref TEXT DEFAULT '', issued_at REAL
);
CREATE INDEX IF NOT EXISTS service_stock_sku ON service_stock(sku, ref);
CREATE TABLE IF NOT EXISTS service_jobs (
    ref TEXT PRIMARY KEY, order_id TEXT, sku TEXT, source TEXT, status TEXT, detail TEXT DEFAULT '',
    payload TEXT DEFAULT '', cost_rub REAL DEFAULT 0, offer TEXT DEFAULT '', created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS service_jobs_order ON service_jobs(order_id);
CREATE TABLE IF NOT EXISTS order_secrets (order_id TEXT PRIMARY KEY, data TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS service_costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sku TEXT, cost_rub REAL, order_id TEXT DEFAULT '', at REAL
);
CREATE INDEX IF NOT EXISTS service_costs_sku ON service_costs(sku, at);
"""

COST_RE = re.compile(r"\s*(?:цена|себестоимость|cost)\s*[:=]?\s*(\d+(?:[.,]\d+)?)\s*(?:₽|р|руб\.?)?\s*$", re.I)


def split_cost(text: str) -> tuple[str, float | None]:
    """«КОД-123 цена 2050» → («КОД-123», 2050.0). Оператор пишет фактическую цену закупки в конце сообщения."""
    text = str(text or "")
    m = COST_RE.search(text)
    if not m:
        return text.strip(), None
    return text[:m.start()].strip(), float(m.group(1).replace(",", "."))

# Куда уходят деньги на закупку (для круга, shopbot/loop.py)
SPEND_ROUTE = {
    "stock": "склад куплен заранее (USDT из хаба / баланс площадки)",
    "panel": "VPS раз в месяц (USDT/крипта у хостера) — себестоимость ключа ≈ 0",
    "resale": "баланс GGSEL/FunPay (выручка FunPay тратится сразу, без вывода; GGSEL — пополнение СБП/картой)",
}


def mask(code: str) -> str:
    code = str(code or "")
    return code if len(code) <= 6 else code[:3] + "…" + code[-3:]


class Vault:
    """Шифрование данных входа покупателя. Ключ — services.secret_key (создаётся сам)."""

    def __init__(self, cfg):
        key = cfg.get("services.secret_key") or ""
        self.f = None
        try:
            from cryptography.fernet import Fernet
            if not key:
                key = Fernet.generate_key().decode()
                if not cfg.is_demo:
                    cfg.save({"services": {"secret_key": key}})
            self.f = Fernet(key.encode())
        except Exception as e:  # noqa: BLE001 — без cryptography: base64 + предупреждение
            log.warning("cryptography недоступна (%s): данные входа хранятся без шифрования до удаления", e)

    def seal(self, data: dict) -> str:
        raw = json.dumps(data, ensure_ascii=False).encode()
        return self.f.encrypt(raw).decode() if self.f else "b64:" + base64.b64encode(raw).decode()

    def open(self, blob: str) -> dict:
        if not blob:
            return {}
        if blob.startswith("b64:"):
            return json.loads(base64.b64decode(blob[4:]))
        return json.loads(self.f.decrypt(blob.encode())) if self.f else {}


# ---------------------------------------------------------------------- каталог
class Catalog:
    def __init__(self, cfg):
        self.cfg = cfg

    def items(self) -> list[dict]:
        return [i for i in (self.cfg.get("catalog.services.items") or []) if i.get("enabled", True)]

    def item(self, sid: str) -> dict | None:
        return next((i for i in self.items() if i["id"] == sid), None)

    @staticmethod
    def split(key: str) -> tuple[str, str]:
        sid, _, vid = str(key).partition(":")
        return sid, vid

    def variant(self, key: str) -> tuple[dict | None, dict | None]:
        sid, vid = self.split(key)
        it = self.item(sid)
        if it is None:
            return None, None
        v = next((v for v in it.get("variants") or [] if str(v["id"]) == vid), None)
        return it, v

    def sku(self, key: str) -> str:
        """Что физически выдаём: Claude Pro = карта App Store США на 20 $ (uses), поэтому склад общий."""
        it, v = self.variant(key)
        if it is None or v is None:
            return key
        if it.get("uses"):
            return f"{it['uses']}:{v.get('usd') or v['id']}"
        return f"{it['id']}:{v['id']}"

    def fields(self, it: dict) -> list[str]:
        return list(it.get("fields") or KIND_FIELDS.get(it.get("kind", "code"), []))

    def title(self, key: str, amount: int = 0) -> str:
        it, v = self.variant(key)
        if it is None:
            return key
        if it.get("kind") == "topup":
            return f"{it.get('emoji', '')} {it['title']} на {amount} ₽".strip()
        return f"{it.get('emoji', '')} {it['title']} · {v['title'] if v else ''}".strip()

    def validate(self, key: str, amount: int) -> str:
        it, v = self.variant(key)
        if it is None or v is None:
            return "Нет такого товара"
        if it.get("kind") == "topup":
            lo, hi = int(v.get("min_rub") or 100), int(v.get("max_rub") or 15000)
            if not lo <= amount <= hi:
                return f"Сумма от {lo} до {hi} ₽"
        return ""

    def notice_hash(self, sid: str) -> str:
        it = self.item(sid) or {}
        return hashlib.sha256(str(it.get("notice") or "").encode()).hexdigest()[:12]


def market_ref(it: dict, v: dict) -> float:
    """Цена рынка для варианта: явная market_ref_rub или курс рынка за $ (карты App Store)."""
    if v.get("market_ref_rub"):
        return float(v["market_ref_rub"])
    if it.get("market_rub_per_usd") and v.get("usd"):
        return max(float(it["market_rub_per_usd"]) * float(v["usd"]), float(it.get("market_min_rub") or 0))
    return 0.0


def service_price(cfg, it: dict, v: dict, cost_rub: float, amount: int = 0) -> tuple[float, str]:
    """Цена для клиента: фиксированная (price_rub варианта) или себестоимость + маржа; не ниже минимальной маржи."""
    c = cfg.get("catalog.services") or {}
    margin = float(v.get("margin_pct", it.get("margin_pct", c.get("margin_pct", 12))))
    floor_pct = float(v.get("min_margin_pct", it.get("min_margin_pct", c.get("min_margin_pct", 4))))
    floor = cost_rub * (1 + floor_pct / 100) + float(cfg.get("rtba.min_quote_margin_rub") or 0)
    if it.get("kind") == "topup":
        rate = float(v.get("price_rate") or 0)
        if rate:
            target, why = amount * rate + float(v.get("fee_rub") or 0), f"курс {rate:g} ₽ за 1 ₽ Steam"
        else:
            target, why = cost_rub * (1 + margin / 100), f"себестоимость + {margin:g}%"
    elif v.get("price_rub"):
        target, why = float(v["price_rub"]), "фиксированная цена"
    elif it.get("price_mode") == "market" and market_ref(it, v):
        cut = float(it.get("undercut_pct") or 0)
        target, why = market_ref(it, v) * (1 - cut / 100), f"рынок {market_ref(it, v):.0f} ₽ − {cut:g}%"
    else:
        target, why = cost_rub * (1 + margin / 100), f"себестоимость + {margin:g}%"
    if target < floor:
        target, why = floor, f"минимальная маржа {floor_pct:g}%"
    return round(-(-target // 1), 2), why     # вверх до рубля: цены без копеек


# ---------------------------------------------------------------------- источники
class StockSource:
    name, auto = "stock", True

    def __init__(self, db):
        self.db = db

    def count(self, sku: str) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM service_stock WHERE sku=? AND ref=''", (sku,), 0))

    def cost(self, sku: str) -> float | None:
        r = self.db.one("SELECT cost_rub FROM service_stock WHERE sku=? AND ref='' ORDER BY id LIMIT 1", (sku,))
        return float(r["cost_rub"]) if r else None

    def add(self, sku: str, codes: list[str], cost_rub: float, note: str = "") -> int:
        have = {r["code"] for r in self.db.q("SELECT code FROM service_stock WHERE sku=?", (sku,))}
        n = 0
        for c in codes:
            c = c.strip()
            if c and c not in have:
                self.db.exec("INSERT INTO service_stock(sku,code,cost_rub,note,added_at) VALUES(?,?,?,?,?)",
                             (sku, c, float(cost_rub), note[:100], time.time()))
                have.add(c)
                n += 1
        return n

    def take(self, sku: str, ref: str) -> tuple[str, float] | None:
        with self.db.lock:
            r = self.db.one("SELECT id, code, cost_rub FROM service_stock WHERE sku=? AND ref='' ORDER BY id LIMIT 1",
                            (sku,))
            if r is None:
                return None
            self.db.exec("UPDATE service_stock SET ref=?, issued_at=? WHERE id=?", (ref, time.time(), r["id"]))
        return r["code"], float(r["cost_rub"])


class PanelSource:
    """Свои VPN-серверы: 3x-ui (/login, /panel/api/inbounds/*) или Marzban (/api/admin/token, /api/user).

    Ключ создаётся сразу на ВСЕХ включённых серверах (services.panels) с одним UUID и сроком: покупатель получает
    vless://-ссылку каждого сервера и ссылку подписки основного (Happ сам обновляет её раз в 12 часов). Заблокировали
    один IP — работает второй. Срок 3x-ui/Marzban выключают сами (expiryTime/expire); продление сдвигает срок от
    max(сейчас, старый срок). Настройки сервера печатает bots/shopbot/vpn/install-3xui.sh.
    """

    name, auto = "panel", True

    def __init__(self, cfg, *, demo: bool = False):
        self.cfg = cfg
        self.demo = demo

    def panels(self) -> list[dict]:
        out = [p for p in (self.cfg.get("services.panels") or []) if p.get("enabled", True) and p.get("url")]
        old = self.cfg.get("services.panel") or {}
        if not out and old.get("url"):                     # старый формат: один сервер в services.panel
            out = [{"name": "vpn1", **old}]
        return out

    @property
    def p(self) -> dict:                                     # экономика: общие cost_month_rub/capacity
        return self.cfg.get("services.panel") or {}

    @property
    def configured(self) -> bool:
        return self.demo or any(p.get("username") and p.get("password") for p in self.panels())

    def cost(self, months: int) -> float:
        p = self.p
        servers = max(1, len(self.panels()) or 1)
        per_user = float(p.get("cost_month_rub") or 0) * servers / max(1, int(p.get("capacity") or 1))
        return round(per_user * max(1, months), 2)

    # ------------------------------------------------------------------ http
    @staticmethod
    def _session(p: dict) -> aiohttp.ClientSession:
        conn = aiohttp.TCPConnector(ssl=False) if p.get("verify_tls") is False else None
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), connector=conn,
                                     cookie_jar=aiohttp.CookieJar(unsafe=True))

    @staticmethod
    async def _json(r) -> dict:
        try:
            return await r.json(content_type=None)
        except Exception:  # noqa: BLE001
            raise RuntimeError(f"HTTP {r.status}: {(await r.text())[:150]}")

    async def _xui_login(self, s, p: dict) -> str:
        base = str(p["url"]).rstrip("/")
        async with s.post(base + "/login", data={"username": p["username"], "password": p["password"]}) as r:
            if not (await self._json(r)).get("success"):
                raise RuntimeError(f"{p.get('name')}: вход в 3x-ui не удался")
        return base

    async def _xui_link(self, s, base: str, p: dict, client_id: str, label: str) -> str:
        async with s.get(f"{base}/panel/api/inbounds/get/{int(p.get('inbound_id') or 1)}") as r:
            ib = (await self._json(r)).get("obj") or {}
        st = json.loads(ib.get("streamSettings") or "{}")
        rs = st.get("realitySettings") or {}
        rset = rs.get("settings") or {}
        host = p.get("host") or base.split("//", 1)[-1].split("/")[0].split(":")[0]
        q = {"type": st.get("network", "tcp"), "security": st.get("security", "reality"),
             "pbk": rset.get("publicKey", ""), "fp": rset.get("fingerprint", "chrome"),
             "sni": (rs.get("serverNames") or [""])[0], "sid": (rs.get("shortIds") or [""])[0],
             "spx": rset.get("spiderX", "/"), "flow": p.get("flow", "xtls-rprx-vision")}
        from urllib.parse import quote, urlencode
        return f"vless://{client_id}@{host}:{ib.get('port', 443)}?{urlencode(q)}#{quote(label)}"

    async def _xui_upsert(self, p: dict, client: dict, *, update: bool) -> tuple[str, str]:
        async with self._session(p) as s:
            base = await self._xui_login(s, p)
            body = {"id": int(p.get("inbound_id") or 1), "settings": json.dumps({"clients": [client]})}
            path = f"/panel/api/inbounds/updateClient/{client['id']}" if update else "/panel/api/inbounds/addClient"
            async with s.post(base + path, json=body) as r:
                res = await self._json(r)
                if not res.get("success"):
                    raise RuntimeError(f"{p.get('name')}: 3x-ui {res.get('msg')}")
            link = await self._xui_link(s, base, p, client["id"], f"DPB {p.get('name', '')}".strip())
        sub = f"{str(p['sub_base']).rstrip('/')}/{client['subId']}" if p.get("sub_base") else ""
        return link, sub

    async def _marzban(self, p: dict, name: str, expire: int, *, update: bool) -> tuple[str, str]:
        base = str(p["url"]).rstrip("/")
        async with self._session(p) as s:
            async with s.post(base + "/api/admin/token", data={"username": p["username"],
                                                              "password": p["password"]}) as r:
                tok = (await self._json(r))["access_token"]
            h = {"Authorization": f"Bearer {tok}"}
            if update:
                req = s.put(f"{base}/api/user/{name}", json={"expire": expire, "status": "active"}, headers=h)
            else:
                req = s.post(base + "/api/user", headers=h, json={
                    "username": name, "proxies": {"vless": {"flow": p.get("flow", "xtls-rprx-vision")}},
                    "inbounds": {"vless": list(p.get("inbound_tags") or [])}, "expire": expire,
                    "data_limit": int(float(p.get("data_limit_gb") or 0) * 1024 ** 3),
                    "data_limit_reset_strategy": "no_reset"})
            async with req as r:
                if r.status not in (200, 201):
                    raise RuntimeError(f"{p.get('name')}: Marzban {r.status} {(await r.text())[:120]}")
                u = await r.json(content_type=None)
        sub = str(u.get("subscription_url") or "")
        if sub.startswith("/"):
            sub = (p.get("sub_base") or base).rstrip("/") + sub
        links = [x for x in (u.get("links") or []) if str(x).startswith("vless://")]
        return (links[0] if links else ""), sub

    # ------------------------------------------------------------------ ключ
    async def create(self, ref: str, months: int, meta: dict | None = None) -> tuple[str, dict]:
        """Новый ключ (meta=None) или продление (meta — от прошлого ключа). → (текст для покупателя, meta)."""
        now = time.time()
        renew = bool(meta)
        meta = dict(meta or {})
        base_exp = max(now, float(meta.get("expires_at") or 0))
        expire = int(base_exp + months * 30 * 86400)
        name = meta.get("name") or f"dpb_{ref.lower()}"[:32]
        cid = meta.get("uuid") or str(uuid.uuid4())
        sub_id = meta.get("sub_id") or secrets.token_hex(8)
        meta.update(name=name, uuid=cid, sub_id=sub_id, expires_at=expire)
        if self.demo:
            return (f"vless://{cid}@vpn.demo.example:443?type=tcp&security=reality&fp=chrome#DPB\n"
                    f"https://vpn.demo.example/sub/{sub_id}"), meta
        links, subs, errors = [], [], []
        for p in self.panels():
            try:
                if p.get("kind", "3xui") == "marzban":
                    link, sub = await self._marzban(p, name, expire, update=renew)
                else:
                    client = {"id": cid, "email": f"{name}_{p.get('name', '')}"[:40], "enable": True,
                              "expiryTime": expire * 1000, "limitIp": int(p.get("limit_ip") or 3),
                              "totalGB": int(float(p.get("data_limit_gb") or 0) * 1024 ** 3),
                              "flow": p.get("flow", "xtls-rprx-vision"), "subId": sub_id, "tgId": "", "reset": 0}
                    link, sub = await self._xui_upsert(p, client, update=renew)
                if link:
                    links.append(link)
                if sub:
                    subs.append(sub)
            except Exception as e:  # noqa: BLE001 — один сервер упал, остальные выдают ключ
                log.warning("VPN %s: %s", p.get("name"), e)
                errors.append(f"{p.get('name')}: {e}")
        if not links and not subs:
            raise RuntimeError("; ".join(errors) or "нет серверов в services.panels")
        meta["servers"] = len(links)
        if errors:
            meta["errors"] = errors
        return "\n".join([*subs[:1], *links]), meta


class ResaleSource:
    """Перепродажа с GGSEL/FunPay: задача оператору (или будущему авто-покупателю)."""

    name, auto = "resale", False

    @staticmethod
    def best(it: dict, v: dict, amount: int, rates=None) -> dict | None:
        out = None
        for off in it.get("offers") or []:
            if not off.get("enabled", True):
                continue
            costs = off.get("costs") or {}
            cost = None
            if str(v["id"]) in costs:
                cost = float(costs[str(v["id"])])
            elif off.get("rub_per_usd") and v.get("usd"):
                cost = max(float(off["rub_per_usd"]) * float(v["usd"]), float(off.get("min_rub") or 0))
            elif off.get("rate") and it.get("kind") == "topup":
                cost = amount * float(off["rate"]) + float(off.get("fee_rub") or 0)
            if cost is None:
                continue
            if off.get("market_fee_pct"):
                cost *= 1 + float(off["market_fee_pct"]) / 100
            cand = {**off, "cost_rub": round(cost, 2)}
            if out is None or cand["cost_rub"] < out["cost_rub"]:
                out = cand
        return out


# ---------------------------------------------------------------------- поставщик для магазина
class ServiceSupplier:
    """Тот же контракт, что у ботов экосистемы (quote/order/status/mark), но с ключом товара «service:variant»."""

    name, kind = "services", "services"

    def __init__(self, cfg, db, *, demo: bool = False, rates=None):
        self.cfg, self.db, self.demo, self.rates = cfg, db, demo, rates
        with db.lock:
            db.con.executescript(SCHEMA)
            have = {r[1] for r in db.con.execute("PRAGMA table_info(service_jobs)")}
            for col, decl in (("meta", "TEXT DEFAULT ''"), ("expires_at", "REAL DEFAULT 0"),
                              ("reminded", "INTEGER DEFAULT 0")):
                if col not in have:
                    db.con.execute(f"ALTER TABLE service_jobs ADD COLUMN {col} {decl}")
            db.con.commit()
        self.catalog = Catalog(cfg)
        self.stock = StockSource(db)
        self.panel = PanelSource(cfg, demo=demo)
        self.resale = ResaleSource()
        self.vault = Vault(cfg)

    # совместимость с общим интерфейсом (RTBA пингует поставщика)
    async def ping(self) -> dict:
        return {"ok": True}

    async def quote(self, product: str, amount: int = 0, months: int = 0, gift: dict | None = None) -> dict:
        return {"available": False, "reason": "используйте quote_item"}

    def options(self, key: str, amount: int = 1) -> list[dict]:
        """Все доступные источники с себестоимостью (для выбора и для панели экономики)."""
        it, v = self.catalog.variant(key)
        if it is None or v is None:
            return []
        sku = self.catalog.sku(key)
        srcs = it.get("sources") or ["stock", "resale"]
        out = []
        if "stock" in srcs and self.stock.count(sku) > 0:
            out.append({"source": "stock", "auto": True, "cost_rub": self.stock.cost(sku) or 0.0,
                        "left": self.stock.count(sku)})
        if "panel" in srcs and it.get("kind") == "vpn" and self.panel.configured:
            out.append({"source": "panel", "auto": True, "cost_rub": self.panel.cost(int(v.get("months") or 1))})
        if "resale" in srcs:
            best = self.resale.best(it, v, amount, self.rates)
            if best is not None:
                cost, learned = best["cost_rub"], None
                if it.get("kind") == "topup":
                    rate = self.learned_cost("rate:" + sku)
                    if rate is not None:        # фактический курс последних пополнений
                        learned = round(amount * rate + float(best.get("fee_rub") or 0), 2)
                else:
                    learned = self.learned_cost(sku)
                if learned is not None:
                    cost = learned              # фактическая цена последних закупок важнее цены из конфига
                out.append({"source": "resale", "auto": False, "cost_rub": cost,
                            "offer": {k: best.get(k) for k in ("market", "url", "title", "option")},
                            **({"learned": True, "config_cost_rub": best["cost_rub"]} if learned is not None else {})})
        return out

    # ------------------------------------------------------------------ фактические цены закупки
    def record_cost(self, ref: str, cost_rub: float) -> None:
        """Оператор сообщил, за сколько реально купил: правим себестоимость заказа и запоминаем цену."""
        job = self._job(ref)
        if job is None or cost_rub is None or cost_rub <= 0:
            return
        self._set(ref, cost_rub=round(float(cost_rub), 2))
        sku, value = job["sku"], round(float(cost_rub), 2)
        it, _ = self.catalog.variant(job["sku"])
        if it is not None and it.get("kind") == "topup":     # для пополнения запоминаем курс (₽ за 1 ₽ Steam)
            o = self.db.one("SELECT amount FROM orders WHERE id=?", (job["order_id"],))
            if o is None or not int(o["amount"] or 0):
                return
            sku, value = "rate:" + job["sku"], round(float(cost_rub) / int(o["amount"]), 4)
        self.db.exec("INSERT INTO service_costs(sku, cost_rub, order_id, at) VALUES(?,?,?,?)",
                     (sku, value, job["order_id"] or "", time.time()))

    def learned_cost(self, sku: str) -> float | None:
        """Медиана последних 3 закупок за services.learned_cost_days (по умолчанию 14) дней."""
        days = float(self.cfg.get("services.learned_cost_days", 14) or 0)
        if days <= 0:
            return None
        rows = self.db.q("SELECT cost_rub FROM service_costs WHERE sku=? AND at>=? ORDER BY at DESC LIMIT 3",
                           (sku, time.time() - days * 86400))
        vals = sorted(float(r["cost_rub"]) for r in rows)
        if not vals:
            return None
        n = len(vals)
        return round(vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2, 2)

    def choose(self, key: str, amount: int = 1) -> dict | None:
        opts = self.options(key, amount)
        if not opts:
            return None
        cheapest = min(opts, key=lambda o: o["cost_rub"])
        within = float(self.cfg.get("catalog.services.prefer_auto_within_pct") or 0)
        autos = [o for o in opts if o["auto"]]
        if autos:
            a = min(autos, key=lambda o: o["cost_rub"])
            if a["cost_rub"] <= cheapest["cost_rub"] * (1 + within / 100) + 1e-9:
                return a
        return cheapest

    async def quote_item(self, key: str, amount: int = 1) -> dict:
        it, v = self.catalog.variant(key)
        if it is None or v is None:
            return {"available": False, "reason": "нет такого товара"}
        ch = self.choose(key, amount)
        if ch is None:
            return {"available": False, "reason": "нет источника: склад пуст, панель и предложения не настроены"}
        return {"available": True, "cost_rub": ch["cost_rub"], "source": ch["source"], "auto": ch["auto"]}

    # ------------------------------------------------------------------ секреты покупателя
    def save_secret(self, oid: str, data: dict) -> None:
        if data:
            self.db.exec("INSERT OR REPLACE INTO order_secrets(order_id,data,created_at) VALUES(?,?,?)",
                         (oid, self.vault.seal(data), time.time()))

    def secret(self, oid: str) -> dict:
        r = self.db.one("SELECT data FROM order_secrets WHERE order_id=?", (oid,))
        return self.vault.open(r["data"]) if r else {}

    def wipe(self, oid: str) -> None:
        self.db.exec("DELETE FROM order_secrets WHERE order_id=?", (oid,))

    # ------------------------------------------------------------------ заказ
    def _job(self, ref: str):
        return self.db.one("SELECT * FROM service_jobs WHERE ref=?", (ref,))

    def _set(self, ref: str, **f) -> None:
        f["updated_at"] = time.time()
        self.db.exec(f"UPDATE service_jobs SET {','.join(k + '=?' for k in f)} WHERE ref=?", [*f.values(), ref])

    async def order_item(self, ref: str, oid: str, key: str, recipient: str, amount: int = 1) -> dict:
        job = self._job(ref)
        if job is not None:
            return {"ok": True, "order_id": ref, **self._state(job)}
        it, v = self.catalog.variant(key)
        if it is None or v is None:
            return {"ok": False, "error": "нет такого товара"}
        ch = self.choose(key, amount)
        if self.renewable(recipient) is not None and self.panel.configured:     # продление — только своим сервером
            ch = {"source": "panel", "auto": True, "cost_rub": self.panel.cost(int(v.get("months") or 1))}
        if ch is None:
            return {"ok": False, "error": "нет источника товара"}
        now = time.time()
        self.db.exec("INSERT INTO service_jobs(ref,order_id,sku,source,status,cost_rub,offer,created_at,updated_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?)", (ref, oid, self.catalog.sku(key), ch["source"], "queued",
                                                   ch["cost_rub"], json.dumps(ch.get("offer") or {},
                                                                              ensure_ascii=False), now, now))
        if ch["source"] == "stock":
            got = self.stock.take(self.catalog.sku(key), ref)
            if got is None:                      # склад кончился между котировкой и заказом
                self._set(ref, source="resale", status="manual", detail="склад пуст — купить вручную")
            else:
                self._set(ref, status="delivered", payload=got[0], cost_rub=got[1])
        elif ch["source"] == "panel":
            old = self.renewable(recipient)
            try:
                link, meta = await self.panel.create(ref, int(v.get("months") or 1),
                                                     {**json.loads(old["meta"]), "expires_at": float(old["expires_at"] or 0)}
                                                     if old is not None else None)
                self._set(ref, status="delivered", payload=link, meta=json.dumps(meta),
                          expires_at=float(meta["expires_at"]), detail="продление" if old is not None else "")
                if old is not None:
                    self._set(old["ref"], reminded=1, detail="продлён заказом " + oid)
            except Exception as e:  # noqa: BLE001
                log.warning("панель VPN: %s", e)
                self._set(ref, status="manual", source="resale", detail=f"панель VPN: {e}"[:200])
        else:
            off = ch.get("offer") or {}
            self._set(ref, status="manual", detail=f"купить: {off.get('market', '')} {off.get('url', '')} "
                                                   f"{off.get('option', '')}".strip())
        return {"ok": True, "order_id": ref, **self._state(self._job(ref))}

    def renewable(self, recipient: str):
        """recipient «renew:<заказ>» — продлить ключ того заказа (тот же UUID и ссылка подписки)."""
        if not str(recipient).startswith("renew:"):
            return None
        return self.db.one("SELECT * FROM service_jobs WHERE order_id=? AND source='panel' AND meta!='' "
                           "ORDER BY updated_at DESC LIMIT 1", (recipient[6:],))

    def active_keys(self, uid: int) -> list:
        """VPN-ключи клиента с сервера магазина, которые можно продлить (живые и истёкшие до 30 дней)."""
        return self.db.q("SELECT j.*, o.item FROM service_jobs j JOIN orders o ON o.id=j.order_id WHERE o.user_id=? "
                         "AND j.source='panel' AND j.status='delivered' AND j.meta!='' AND j.reminded IN (0,2) AND "
                         "j.expires_at>? ORDER BY j.expires_at DESC", (uid, time.time() - 30 * 86400))

    def expiring(self, days: float = 3) -> list:
        """Ключи, у которых до конца срока меньше days и напоминания ещё не было."""
        now = time.time()
        return self.db.q("SELECT j.*, o.user_id FROM service_jobs j JOIN orders o ON o.id=j.order_id WHERE "
                         "j.source='panel' AND j.status='delivered' AND j.reminded=0 AND j.expires_at>? AND "
                         "j.expires_at<?", (now, now + days * 86400))

    def mark_reminded(self, ref: str) -> None:
        self._set(ref, reminded=2)

    @staticmethod
    def _state(job) -> dict:
        return {"status": job["status"], "detail": job["detail"], "payload": job["payload"], "source": job["source"],
                "cost_rub": job["cost_rub"]}

    async def status(self, ref: str) -> dict:
        job = self._job(ref)
        return self._state(job) if job else {"status": "unknown"}

    def mark(self, ref: str, status: str, payload: str = "") -> None:
        if self._job(ref) is None:
            return
        self._set(ref, status=status, **({"payload": payload} if payload else {}))

    def payload(self, oid: str) -> str:
        r = self.db.one("SELECT payload FROM service_jobs WHERE order_id=? AND payload!='' ORDER BY updated_at DESC "
                        "LIMIT 1", (oid,))
        return r["payload"] if r else ""

    # ------------------------------------------------------------------ экономика (панель, отчёт)
    def economics(self) -> list[dict]:
        rows = []
        for it in self.catalog.items():
            for v in it.get("variants") or []:
                key = f"{it['id']}:{v['id']}"
                amount = int(v.get("example_rub") or 1000) if it.get("kind") == "topup" else 1
                opts = self.options(key, amount)
                ch = self.choose(key, amount)
                row = {"key": key, "title": self.catalog.title(key, amount), "kind": it.get("kind"),
                       "sources": opts, "source": ch["source"] if ch else "", "ref_rub": market_ref(it, v) or None}
                if ch:
                    price, why = service_price(self.cfg, it, v, ch["cost_rub"], amount)
                    row.update(cost_rub=ch["cost_rub"], price_rub=price, why=why,
                               margin_rub=round(price - ch["cost_rub"], 2),
                               margin_pct=round((price - ch["cost_rub"]) / ch["cost_rub"] * 100, 1)
                               if ch["cost_rub"] else None)
                rows.append(row)
        return rows

    def stock_summary(self) -> dict[str, int]:
        return {r["sku"]: r["n"] for r in self.db.q("SELECT sku, COUNT(*) n FROM service_stock WHERE ref='' "
                                                     "GROUP BY sku")}


def delivery_text(it: dict, payload: str, expires_at: float = 0, renewed: bool = False) -> str:
    """Сообщение покупателю с кодом/ключом и инструкцией. Код в <code> — копируется одним нажатием."""
    if renewed:
        return (f"🔄 <b>Ключ VPN продлён до {time.strftime('%d.%m.%Y', time.localtime(expires_at))}</b>\n"
                "Ничего менять не нужно — Happ продолжит работать с той же подпиской.")
    till = (f"\nДействует до <b>{time.strftime('%d.%m.%Y', time.localtime(expires_at))}</b>"
            if expires_at else "")
    body = "\n".join(f"<code>{p}</code>" for p in str(payload).splitlines() if p.strip()) if payload else ""
    instr = str(it.get("instructions") or "")
    head = {"vpn": "🛡 <b>Ваш ключ VPN</b>", "code": "🎟 <b>Ваш код</b>", "account": "✅ <b>Подписка подключена</b>",
            "topup": "✅ <b>Баланс Steam пополнен</b>"}.get(it.get("kind", "code"), "✅ <b>Готово</b>")
    return f"{head}{till}\n{body}\n\n{instr}".strip()

