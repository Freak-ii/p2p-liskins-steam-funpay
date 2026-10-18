"""ИИ-координатор действий центра: брифинги, рекомендации и действия с уровнями полномочий.

Как это устроено:
- Инструменты координатора — это API центра (TOOLS): пауза/возобновление закупок и торговли,
  перезапуск ботов, подтверждение тревог, настройки стратегии и лимитов LisBot, боевой режим, вывод денег.
  У каждого инструмента уровень полномочий:
    auto     — координатор делает сам (по умолчанию только пауза закупок LisBot при явной аномалии);
    propose  — только предлагает, выполняется после «Одобрить» в панели;
    confirm  — требует явного подтверждения FrayZ (боевой режим, смена лимитов);
    manual   — центр этого не делает вообще: после одобрения FrayZ выполняет сам (вывод денег);
    off      — инструмент выключен.
  Уровни настраиваются в coordinator.permissions (center.yaml), но LOCKED-инструменты нельзя
  опустить ниже confirm/manual, а auto разрешён только для AUTO_ALLOWED.
- Сторож аномалий (правила без ИИ) раз в check_seconds смотрит на базы ботов (только чтение) и состояние
  воркеров: всплеск трат LisBot, серия убыточных продаж, всплеск возвратов MannBot, бот упал окончательно.
  Сам ИИ ничего не выполняет: его рекомендации всегда становятся предложениями.
- Брифинг раз в день (и по кнопке): факты из аналитики и мониторинга → Claude API (или заглушка без ключа,
  в тестах и демо) → текст и рекомендации. Уведомления уходят через Telegram-модуль мониторинга.
- Все решения (действия, предложения, брифинги, ответы) — в таблице coord_decisions в center.sqlite3.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import time

import aiohttp
from aiohttp import web

from center.analytics import _rows
from center.monitor import parse_hhmm
from center.workers import CRASHED, RUNNING, STATE_TEXT

log = logging.getLogger("center.coordinator")

SCHEMA = """
CREATE TABLE IF NOT EXISTS coord_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,          -- action (сделано само) | proposal | briefing | answer | anomaly
    tool TEXT,
    args TEXT,                   -- JSON аргументов инструмента
    tier TEXT,                   -- auto | propose | confirm | manual
    status TEXT NOT NULL,        -- done | failed | proposed | approved | rejected | expired | manual | info
    title TEXT NOT NULL,
    reason TEXT,
    result TEXT,
    source TEXT,                 -- rule:<код> | ai | stub | panel
    decided_at REAL,
    data TEXT
);
CREATE INDEX IF NOT EXISTS idx_coord_ts ON coord_decisions(ts);
CREATE TABLE IF NOT EXISTS coord_kv (k TEXT PRIMARY KEY, v TEXT);
"""

TIERS = ("auto", "propose", "confirm", "manual", "off")
TIER_TEXT = {"auto": "делает сам", "propose": "предлагает", "confirm": "нужно подтверждение FrayZ",
             "manual": "только вручную", "off": "выключено"}
AUTO_ALLOWED = {"pause_buying", "pause_bot"}                  # что вообще можно разрешить делать самому
LOCKED = {"change_limits": "confirm", "set_live_mode": "confirm", "withdraw": "manual"}
ORDER = ("auto", "propose", "confirm", "manual")          # от мягкого к строгому
BOTS = ("lisbot", "mannbot")

# Ключи настроек LisBot, которые считаются лимитами (деньги и риск) — их меняет только change_limits
LIMIT_KEYS = {"strategy.maxSpendPerHourUsd", "strategy.maxSpendPerDayUsd", "strategy.maxOpenExposureUsd",
              "strategy.maxItemCapitalShare", "strategy.maxItemPriceUsd", "strategy.balanceReservePct",
              "strategy.maxPerItemPerDay", "strategy.maxHeldPerItem", "risk.maxDailyLossUsd", "risk.lowBalanceUsd",
              "risk.unknownSpendIsCritical", "risk.strictConfirmations", "risk.acceptFreeOffers"}
STRATEGY_KEYS = {"strategy.minRoi", "strategy.roiPerHoldDay", "strategy.valuationQuantile", "strategy.minDailyVolume",
                 "strategy.bottomTolerancePct", "strategy.maxFloorRisePct", "strategy.maxBookSharePerScan",
                 "strategy.maxShareOfDailyVolume", "strategy.minItemPriceUsd", "strategy.scanIntervalSec",
                 "strategy.rtbaEnabled", "sell.minMarginOverCost", "sell.repriceAfterHours", "sell.maxListingsPerHour",
                 "sell.maxListingsPerDay", "steam.activeHours.enabled", "steam.activeHours.from", "steam.activeHours.to"}


class ToolError(ValueError):
    pass


def _bot(args: dict, allowed=BOTS) -> str:
    b = str(args.get("bot") or "lisbot")
    if b not in allowed:
        raise ToolError(f"бот должен быть одним из: {', '.join(allowed)}")
    return b


def _item_name(args: dict) -> str:
    name = str(args.get("name") or "").strip()
    if not name or len(name) > 200:
        raise ToolError("нужно название предмета (name) из списка закупок LisBot")
    return name


def _changes(args: dict, keys: set[str]) -> dict:
    ch = args.get("changes")
    if not isinstance(ch, dict) or not ch:
        raise ToolError("нужен непустой словарь changes {ключ настройки LisBot: значение}")
    bad = [k for k in ch if k not in keys]
    if bad:
        raise ToolError(f"эти настройки нельзя менять этим инструментом: {', '.join(bad)}")
    for k, v in ch.items():
        if not isinstance(v, (int, float, bool, str)):
            raise ToolError(f"{k}: значение должно быть числом, да/нет или строкой")
    return ch


# name: (описание для ИИ и панели, уровень по умолчанию, проверка аргументов → нормализованные args)
TOOLS: dict[str, tuple[str, str, object]] = {
    "pause_buying": ("Пауза закупок LisBot (продажи идут дальше)", "auto", lambda a: {"bot": _bot(a, ("lisbot",))}),
    "resume_buying": ("Возобновить закупки LisBot", "propose", lambda a: {"bot": _bot(a, ("lisbot",))}),
    "pause_bot": ("Пауза торговли бота (bot: lisbot | mannbot)", "propose", lambda a: {"bot": _bot(a)}),
    "resume_bot": ("Снять паузу торговли бота (bot: lisbot | mannbot)", "propose", lambda a: {"bot": _bot(a)}),
    "restart_bot": ("Перезапустить процесс бота (bot: lisbot | mannbot)", "propose", lambda a: {"bot": _bot(a)}),
    "ack_alerts": ("Подтвердить все тревоги бота (bot: lisbot | mannbot)", "propose", lambda a: {"bot": _bot(a)}),
    "disable_item": ("Перестать закупать предмет LisBot (name): выключить его в списке закупок, продажи не трогает",
                     "propose", lambda a: {"name": _item_name(a)}),
    "change_strategy": ("Изменить настройки стратегии LisBot (changes: {ключ: значение}; ключи: "
                        + ", ".join(sorted(STRATEGY_KEYS)) + ")", "propose",
                        lambda a: {"changes": _changes(a, STRATEGY_KEYS)}),
    "change_limits": ("Изменить лимиты и риск LisBot (changes: {ключ: значение}; ключи: "
                      + ", ".join(sorted(LIMIT_KEYS)) + ")", "confirm", lambda a: {"changes": _changes(a, LIMIT_KEYS)}),
    "set_live_mode": ("Включить боевой режим (live: true) или вернуть тестовый (live: false) у бота", "confirm",
                      lambda a: {"bot": _bot(a), "live": bool(a.get("live"))}),
    "withdraw": ("Вывести деньги (amount, currency, note) — центр сам не выводит, только FrayZ вручную", "manual",
                 lambda a: {"amount": float(a.get("amount") or 0), "currency": str(a.get("currency") or "RUB"),
                            "note": str(a.get("note") or "")[:300]}),
}

RECO_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Что сделать, коротко"},
        "why": {"type": "string", "description": "Почему, с цифрами из фактов"},
        "tool": {"type": "string", "enum": [*TOOLS, "none"], "description": "Инструмент центра или none"},
        "args_json": {"type": "string", "description": "Аргументы инструмента в JSON, например {\"bot\":\"lisbot\"}; {} если none"},
    },
    "required": ["title", "why", "tool", "args_json"],
    "additionalProperties": False,
}
BRIEFING_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "Брифинг: 3–8 коротких предложений по-русски, простым текстом"},
        "highlights": {"type": "array", "items": {"type": "string"}, "description": "Ключевые цифры и события"},
        "recommendations": {"type": "array", "items": RECO_SCHEMA},
    },
    "required": ["summary", "highlights", "recommendations"],
    "additionalProperties": False,
}
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}, "recommendations": {"type": "array", "items": RECO_SCHEMA}},
    "required": ["answer", "recommendations"],
    "additionalProperties": False,
}

SYSTEM = """Ты — ИИ-координатор центра перепродажи FrayZ. В центре два бота:
- LisBot покупает скины CS2 на LIS-SKINS и продаёт их на торговой площадке Steam (деньги в USD);
- MannBot продаёт ключи Mann Co на FunPay (деньги в рублях).
Тебе дают факты о центре в JSON (сводки ботов, тревоги, мониторинг за сутки, P&L за 1 и 7 дней, открытые
предложения, недавние решения, уровни полномочий инструментов). Отвечай по-русски, коротко и по делу,
опираясь только на факты; если данных нет — так и скажи, не выдумывай цифры.

Рекомендации: только когда есть основания в фактах, не больше пяти. Если действие можно выразить
инструментом центра — укажи tool и args_json; иначе tool = "none". Ты ничего не выполняешь сам:
каждая рекомендация становится предложением, которое одобряет FrayZ. Не предлагай повторно то, что уже
есть в открытых предложениях. Боевой режим, лимиты и вывод денег предлагай только с ясным обоснованием.
Не предлагай инструменты с уровнем off."""

FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5")


class BackendError(Exception):
    pass


class ClaudeBackend:
    """Один запрос Messages API со структурированным JSON-ответом. Модель ничего не вызывает сама."""

    def __init__(self, api_key: str, model: str, effort: str = "medium", timeout: float = 120):
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover - зависит от окружения
            raise BackendError("не установлен пакет anthropic: pip install -r requirements.txt") from e
        self._a = anthropic
        self.client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout, max_retries=2)
        self.model = model
        self.effort = effort
        self.server_fallback = model in FALLBACK_MODELS

    async def complete(self, user: str, schema: dict) -> tuple[dict, dict]:
        a = self._a
        kwargs: dict = {"model": self.model, "max_tokens": 16000, "system": SYSTEM,
                        "messages": [{"role": "user", "content": user}],
                        "output_config": {"effort": self.effort, "format": {"type": "json_schema", "schema": schema}}}
        if self.server_fallback:
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        t0 = time.monotonic()
        try:
            resp = await (self.client.beta.messages.create(**kwargs) if self.server_fallback
                          else self.client.messages.create(**kwargs))
        except a.AuthenticationError as e:
            raise BackendError("Claude API: неверный API-ключ") from e
        except a.PermissionDeniedError as e:
            raise BackendError("Claude API: у ключа нет доступа к модели") from e
        except a.NotFoundError as e:
            raise BackendError(f"Claude API: модель {self.model} не найдена") from e
        except a.RateLimitError as e:
            raise BackendError("Claude API: превышен лимит запросов") from e
        except a.APIStatusError as e:
            raise BackendError(f"Claude API: ошибка {e.status_code}: {getattr(e, 'message', e)}") from e
        except a.APIConnectionError as e:
            raise BackendError("Claude API недоступен (сеть)") from e
        if resp.stop_reason == "refusal":
            raise BackendError("Claude отказался отвечать")
        if resp.stop_reason == "max_tokens":
            raise BackendError("ответ модели обрезан (max_tokens)")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise BackendError("модель вернула не JSON") from e
        u = resp.usage
        meta = {"model": getattr(resp, "model", self.model), "latency_ms": int((time.monotonic() - t0) * 1000),
                "tokens_in": int(getattr(u, "input_tokens", 0) or 0), "tokens_out": int(getattr(u, "output_tokens", 0) or 0)}
        return data, meta


def _n(v, d=2) -> str:
    if v is None:
        return "—"
    return f"{v:,.{d}f}".replace(",", " ") if isinstance(v, (int, float)) else str(v)


class StubBackend:
    """Без ключа, в тестах и демо: брифинг по правилам из тех же фактов, без сети."""

    model = "stub"

    async def complete(self, user: str, schema: dict) -> tuple[dict, dict]:
        f = json.loads(user.split("<facts>", 1)[1].split("</facts>", 1)[0])
        question = user.split("<question>", 1)[1].split("</question>", 1)[0] if "<question>" in user else ""
        hi, recs = [], []
        open_tools = {(p["tool"], json.dumps(p["args"], sort_keys=True)) for p in f.get("open_proposals", [])}

        def reco(title, why, tool="none", args=None):
            if (tool, json.dumps(args or {}, sort_keys=True)) not in open_tools:
                recs.append({"title": title, "why": why, "tool": tool, "args_json": json.dumps(args or {})})

        for b in f.get("bots", []):
            s = b.get("summary") or {}
            mode = "демо" if s.get("demo") else "тестовый режим" if s.get("dry_run") else "боевой режим" if s else "нет данных"
            hi.append(f"{b['title']}: {b['state_text']}, {mode}"
                      + (f", аптайм за сутки {_n(b['uptime_24h'] * 100, 1)}%" if b.get("uptime_24h") is not None else ""))
            if b["state"] == CRASHED:
                reco(f"Перезапустить {b['title']}", "Бот упал несколько раз подряд, центр остановил перезапуски.",
                     "restart_bot", {"bot": b["name"]})
            if b.get("open_alerts"):
                reco(f"Разобрать тревоги {b['title']}", f"Неподтверждённых тревог: {b['open_alerts']}.",
                     "ack_alerts", {"bot": b["name"]})
        p1, p7 = f.get("pnl_1d") or {}, f.get("pnl_7d") or {}
        t1, t7 = (p1.get("total") or {}).get("RUB") or {}, (p7.get("total") or {}).get("RUB") or {}
        if p7:
            hi.append(f"За 7 дней: сделок {p7['total'].get('deals', 0)}, выручка {_n(t7.get('revenue'), 0)} ₽, "
                      f"прибыль {_n(t7.get('profit'), 0)} ₽, ROI {_n(t7.get('roi'), 1)}%")
        if p1:
            hi.append(f"За сутки: сделок {p1['total'].get('deals', 0)}, прибыль {_n(t1.get('profit'), 0)} ₽")
        lis7 = ((p7.get("bots") or {}).get("lisbot") or {}).get("USD") or {}
        if (lis7.get("roi") is not None and lis7["roi"] < 0) and (p7.get("bots") or {}).get("lisbot", {}).get("deals", 0) >= 3:
            reco("Поднять минимальный ROI LisBot", f"ROI LisBot за 7 дней {_n(lis7['roi'], 1)}% — сделки в минус.",
                 "change_strategy", {"changes": {"strategy.minRoi": 0.12}})
        for w in (p7.get("warnings") or [])[:2]:
            reco("Проверить настройки MannBot", w)
        inv = f.get("inventory") or {}
        il, im = inv.get("lisbot") or {}, inv.get("mannbot") or {}
        if il.get("n"):
            hi.append(f"Склад LisBot: {il['n']} шт на ${_n(il.get('cost'), 2)}, по рынку {_n(il.get('pnl'), 2)} $")
        if il.get("losing") or il.get("old"):
            reco("Разобрать залежавшиеся предметы LisBot",
                 f"В минусе по рынку: {il.get('losing', 0)}, лежат дольше {_n(il.get('old_days'), 0)} дн: {il.get('old', 0)}. "
                 "Решите, держать или снизить цену (раздел «Склад» в аналитике).")
        if im.get("level") in ("low", "empty"):
            reco("Пополнить склад ключей MannBot",
                 f"На складе {im.get('stock')} ключей" + (f", хватит примерно на {_n(im.get('days_left'), 1)} дн."
                                                          if im.get("days_left") is not None else "."))
        cu = f.get("customers") or {}
        if (cu.get("counts") or {}).get("sleeping"):
            names = ", ".join(x["username"] or str(x["funpay_id"]) for x in cu.get("sleeping_top", [])[:3])
            reco("Вернуть уснувших покупателей FunPay",
                 f"Не покупают дольше {_n(cu.get('sleep_days'), 0)} дн: {cu['counts']['sleeping']} покупателей"
                 + (f" ({names})" if names else "") + ". Напишите им в чат FunPay или сделайте скидку (вкладка CRM → «Уснул»).")
        for a in f.get("anomalies_24h", [])[:3]:
            hi.append(f"Аномалия: {a}")
        bad = [b["title"] for b in f.get("bots", []) if b["state"] != RUNNING]
        summary = ("Базовая сводка без ИИ (ключ Claude API не задан или включено демо). "
                   + (f"Не работают: {', '.join(bad)}. " if bad else "Оба бота работают. ")
                   + (f"Рекомендаций: {len(recs)}." if recs else "Срочных действий не требуется."))
        if question:
            return {"answer": "ИИ выключен, отвечаю сводкой фактов. " + "; ".join(hi), "recommendations": recs}, {"model": "stub"}
        return {"summary": summary, "highlights": hi, "recommendations": recs}, {"model": "stub"}


class Coordinator:
    def __init__(self, center, clock=time.time):
        self.center = center
        self.events = center.events
        self.db = self.events.db
        self._lock = self.events._lock
        self.clock = clock
        with self._lock:
            self.db.executescript(SCHEMA)
            self.db.commit()
        self._tasks: list[asyncio.Task] = []
        self._busy = asyncio.Lock()
        self.last_check: dict = {"at": None, "anomalies": []}
        self.backend_override = None                 # тесты подменяют бэкенд
        self.backend_error = ""

    # ------------------------------------------------------------------ настройки и полномочия
    @property
    def cfg(self) -> dict:
        return self.center.cfg["coordinator"]

    def tier(self, tool: str) -> str:
        default = TOOLS[tool][1]
        t = str((self.cfg.get("permissions") or {}).get(tool) or default)
        if t not in TIERS:
            t = default
        if t == "auto" and tool not in AUTO_ALLOWED:
            t = "propose"
        floor = LOCKED.get(tool)
        if floor and t != "off" and ORDER.index(t) < ORDER.index(floor):
            t = floor
        return t

    def permissions(self) -> list[dict]:
        return [{"tool": k, "title": v[0].split(" (")[0], "tier": self.tier(k), "default": v[1],
                 "locked": k in LOCKED, "auto_allowed": k in AUTO_ALLOWED} for k, v in TOOLS.items()]

    def api_key(self) -> str:
        """Ключ Claude API: в зашифрованном хранилище центра (старое значение из center.yaml переносится туда)."""
        ai = self.cfg.get("ai") or {}
        vault = getattr(self.center, "vault", None)
        if vault is None:
            return str(ai.get("api_key") or "")
        if ai.get("api_key"):
            vault.set("center.coordinator.api_key", str(ai["api_key"]))
            self.center.cfg.save({"coordinator": {"ai": {"api_key": ""}}})
        try:
            return vault.get("center.coordinator.api_key")
        except Exception:  # noqa: BLE001 — хранилище не расшифровано
            return ""

    def backend(self):
        if self.backend_override is not None:
            return self.backend_override
        ai = self.cfg.get("ai") or {}
        key = self.api_key()
        if self.center.demo or not key or not ai.get("enabled", True):
            return StubBackend()
        try:
            be = ClaudeBackend(key, str(ai.get("model") or "claude-opus-5-5"), str(ai.get("effort") or "medium"))
        except BackendError as e:
            self.backend_error = str(e)
            return StubBackend()
        self.backend_error = ""
        return be

    def settings(self) -> dict:
        ai = self.cfg.get("ai") or {}
        key = self.api_key()
        return {"enabled": bool(self.cfg.get("enabled", True)),
                "ai": {"enabled": bool(ai.get("enabled", True)), "model": ai.get("model") or "claude-opus-5-5",
                       "effort": ai.get("effort") or "medium", "api_key_set": bool(key),
                       "api_key_hint": f"…{key[-4:]}" if len(key) > 8 else ""},
                "briefing": dict(self.cfg.get("briefing") or {}), "anomaly": dict(self.cfg.get("anomaly") or {}),
                "permissions": self.permissions()}

    def save_settings(self, d: dict) -> None:
        upd: dict = {}
        if "enabled" in d:
            upd["enabled"] = bool(d["enabled"])
        ai = d.get("ai") or {}
        if ai:
            a: dict = {}
            vault = getattr(self.center, "vault", None)
            new_key = str(ai["api_key"]).strip() if ai.get("api_key") else ("" if ai.get("clear_key") else None)
            if new_key is not None:
                if vault is not None:
                    vault.set("center.coordinator.api_key", new_key or None)
                    a["api_key"] = ""
                else:
                    a["api_key"] = new_key
            for k in ("model", "effort"):
                if ai.get(k):
                    a[k] = str(ai[k]).strip()
            if "enabled" in ai:
                a["enabled"] = bool(ai["enabled"])
            if a.get("effort") and a["effort"] not in ("low", "medium", "high", "xhigh", "max"):
                raise ValueError("effort: low, medium, high, xhigh или max")
            upd["ai"] = a
        br = d.get("briefing") or {}
        if br:
            if br.get("time") and parse_hhmm(br["time"]) is None:
                raise ValueError("время брифинга в формате ЧЧ:ММ")
            upd["briefing"] = {k: br[k] for k in ("enabled", "time", "telegram") if k in br}
        perms = d.get("permissions") or {}
        if perms:
            for k, v in perms.items():
                if k not in TOOLS or v not in TIERS:
                    raise ValueError(f"неизвестный инструмент или уровень: {k}={v}")
            upd["permissions"] = dict(perms)
        if upd:
            self.center.cfg.save({"coordinator": upd})

    # ------------------------------------------------------------------ журнал решений
    def _add(self, kind: str, status: str, title: str, *, tool=None, args=None, tier=None, reason="", result="",
             source="", data=None) -> int:
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO coord_decisions(ts, kind, tool, args, tier, status, title, reason, result, source, data) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (self.clock(), kind, tool, json.dumps(args, ensure_ascii=False) if args is not None else None, tier, status,
                 title, reason, result, source, json.dumps(data, ensure_ascii=False) if data is not None else None))
            self.db.commit()
            return int(cur.lastrowid)

    def _update(self, did: int, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self.db.execute(f"UPDATE coord_decisions SET {cols} WHERE id=?", (*fields.values(), did))
            self.db.commit()

    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        for k in ("args", "data"):
            d[k] = json.loads(d[k]) if d.get(k) else None
        return d

    def get(self, did: int) -> dict | None:
        with self._lock:
            r = self.db.execute("SELECT * FROM coord_decisions WHERE id=?", (did,)).fetchone()
        return self._row(r) if r else None

    def decisions(self, limit: int = 50, kind: str | None = None, status: str | None = None) -> list[dict]:
        self._expire()
        sql, args = "SELECT * FROM coord_decisions WHERE 1=1", []
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [self._row(r) for r in self.db.execute(sql, args).fetchall()]

    def _expire(self) -> None:
        ttl = float(self.cfg.get("proposal_ttl_hours") or 24) * 3600
        with self._lock:
            self.db.execute("UPDATE coord_decisions SET status='expired', decided_at=? WHERE status='proposed' AND ts<?",
                            (self.clock(), self.clock() - ttl))
            self.db.commit()

    def kv_get(self, k: str) -> str | None:
        with self._lock:
            r = self.db.execute("SELECT v FROM coord_kv WHERE k=?", (k,)).fetchone()
        return r[0] if r else None

    def kv_set(self, k: str, v) -> None:
        with self._lock:
            self.db.execute("INSERT INTO coord_kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
            self.db.commit()

    # ------------------------------------------------------------------ выполнение инструментов
    def validate(self, tool: str, args: dict | None) -> dict:
        if tool not in TOOLS:
            raise ToolError(f"нет инструмента {tool}")
        try:
            return TOOLS[tool][2](args or {})
        except (TypeError, ValueError) as e:
            raise ToolError(str(e)) from e

    def _worker(self, name: str):
        w = self.center.workers[name]
        if w.state != RUNNING:
            raise ToolError(f"{w.title} не запущен ({STATE_TEXT.get(w.state, w.state)})")
        return w

    async def _lis_settings(self, changes: dict) -> str:
        w = self._worker("lisbot")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), headers=w._headers()) as s:
            async with s.post(f"{w.base_url}/api/settings", json={"changes": changes}) as r:
                d = await r.json(content_type=None)
                if r.status != 200 or not d.get("ok"):
                    raise ToolError(f"LisBot не принял настройки: {d.get('error') or r.status}")
        if "dryRun" in changes:                       # режим применяется при старте бота
            return "настройки сохранены. " + await w.restart("координатор: смена режима")
        return "настройки сохранены"

    async def _disable_item(self, name: str) -> str:
        w = self._worker("lisbot")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), headers=w._headers()) as s:
            async with s.get(f"{w.base_url}/api/watchlist") as r:
                items = await r.json(content_type=None)
            it = next((x for x in items if isinstance(x, dict) and x.get("name") == name), None) if isinstance(items, list) else None
            if it is None:
                raise ToolError(f"«{name}» нет в списке закупок LisBot")
            if not it.get("enabled"):
                return "уже выключен"
            keep = {k: it.get(k) for k in ("max_held", "max_per_day", "min_roi", "max_price_usd", "notes")}
            note = (keep["notes"] + "; " if keep["notes"] else "") + time.strftime("выключен координатором %d.%m")
            async with s.post(f"{w.base_url}/api/watchlist", json={**keep, "name": name, "enabled": False, "notes": note}) as r:
                d = await r.json(content_type=None)
                if r.status != 200 or not d.get("ok"):
                    raise ToolError(f"LisBot не принял изменение: {d.get('error') or r.status}")
        return "закупки предмета выключены"

    async def execute(self, tool: str, args: dict) -> str:
        """Выполнить инструмент через API центра. Проверка полномочий — у вызывающего."""
        args = self.validate(tool, args)
        c = self.center
        if tool in ("pause_buying", "resume_buying"):
            return await self._worker("lisbot").control("buyPause" if tool == "pause_buying" else "buyResume")
        if tool in ("pause_bot", "resume_bot"):
            return await self._worker(args["bot"]).control("pause" if tool == "pause_bot" else "resume")
        if tool == "restart_bot":
            return await c.workers[args["bot"]].restart("координатор центра")
        if tool == "ack_alerts":
            ok, msg = await c.monitor.ack(args["bot"], None)
            if not ok:
                raise ToolError(msg)
            return msg
        if tool == "disable_item":
            return await self._disable_item(args["name"])
        if tool in ("change_strategy", "change_limits"):
            return await self._lis_settings(args["changes"])
        if tool == "set_live_mode":
            if args["bot"] == "lisbot":
                return await self._lis_settings({"dryRun": not args["live"]})
            raise ToolError("режим MannBot переключается в его панели (настройки → тестовый режим)")
        raise ToolError("этот инструмент центр не выполняет — только вручную")

    def describe(self, tool: str, args: dict) -> str:
        bot = {"lisbot": "LisBot", "mannbot": "MannBot"}.get(args.get("bot", ""), "")
        if tool in ("change_strategy", "change_limits"):
            return f"{TOOLS[tool][0].split(' (')[0]}: " + ", ".join(f"{k} = {v}" for k, v in args["changes"].items())
        if tool == "disable_item":
            return f"Перестать закупать «{args['name']}»"
        if tool == "set_live_mode":
            return f"{bot}: {'включить боевой режим' if args['live'] else 'вернуть тестовый режим'}"
        if tool == "withdraw":
            return f"Вывести {_n(args['amount'])} {args['currency']}" + (f" ({args['note']})" if args["note"] else "")
        return TOOLS[tool][0].split(" (")[0] + (f": {bot}" if bot and "buying" not in tool else "")

    async def act_or_propose(self, tool: str, args: dict, *, reason: str, source: str, title: str = "",
                             allow_auto: bool = False) -> dict | None:
        """Точка входа для всех действий. allow_auto — только сторож аномалий: ИИ ничего не делает сам."""
        try:
            args = self.validate(tool, args)
        except ToolError as e:
            log.info("координатор: отклонён %s %s: %s", tool, args, e)
            return None
        tier = self.tier(tool)
        if tier == "off":
            return None
        title = title or self.describe(tool, args)
        if tier == "auto" and allow_auto:
            try:
                result, status = await self.execute(tool, args), "done"
            except Exception as e:  # noqa: BLE001
                result, status = f"не удалось: {e}", "failed"
            did = self._add("action", status, title, tool=tool, args=args, tier=tier, reason=reason, result=result,
                            source=source)
            lvl = "warn" if status == "done" else "critical"
            self.events.add("center", lvl, "coordinator.action", f"Координатор: {title} — {result}. Причина: {reason}")
            await self._notify(f"coord:action:{tool}", lvl, f"<b>🤖 Координатор сделал сам</b>\n{html.escape(title)}: "
                               f"{html.escape(result)}\n<i>{html.escape(reason)}</i>")
            return self.get(did)
        if tier == "auto":
            tier = "propose"
        same = [p for p in self.decisions(200, kind="proposal", status="proposed")
                if p["tool"] == tool and p["args"] == args]
        if same:
            return same[0]
        did = self._add("proposal", "proposed", title, tool=tool, args=args, tier=tier, reason=reason, source=source)
        need = {"confirm": "нужно ваше подтверждение", "manual": "выполняется только вами вручную"}.get(tier, "предложение")
        self.events.add("center", "info", "coordinator.proposal", f"Координатор предлагает: {title} ({need})")
        return self.get(did)

    async def decide(self, did: int, approve: bool, *, confirmed: bool = False) -> tuple[bool, str]:
        self._expire()
        d = self.get(did)
        if d is None or d["kind"] != "proposal":
            return False, "нет такого предложения"
        if d["status"] != "proposed":
            return False, f"предложение уже закрыто ({d['status']})"
        now = self.clock()
        if not approve:
            self._update(did, status="rejected", decided_at=now, result="отклонено FrayZ")
            self.events.add("center", "info", "coordinator.rejected", f"Предложение координатора отклонено: {d['title']}")
            return True, "Предложение отклонено"
        tier = self.tier(d["tool"]) if d["tool"] in TOOLS else "manual"
        if tier == "off":
            return False, "инструмент сейчас выключен в полномочиях координатора"
        if d["tier"] in ORDER and ORDER.index(d["tier"]) > ORDER.index(tier):
            tier = d["tier"]                         # при создании было строже — так и оставляем
        if tier in ("confirm", "manual") and not confirmed:
            return False, "Это действие требует явного подтверждения"
        if tier == "manual":
            self._update(did, status="manual", decided_at=now, result="одобрено, выполните вручную")
            self.events.add("center", "info", "coordinator.manual", f"Одобрено к ручному выполнению: {d['title']}")
            return True, "Одобрено. Центр это не выполняет — сделайте вручную"
        try:
            result, status = await self.execute(d["tool"], d["args"] or {}), "done"
        except Exception as e:  # noqa: BLE001
            result, status = f"не удалось: {e}", "failed"
        self._update(did, status=status, decided_at=now, result=result)
        self.events.add("center", "info" if status == "done" else "warn", "coordinator.approved",
                        f"Одобрено FrayZ: {d['title']} — {result}")
        return status == "done", result

    async def _notify(self, key: str, level: str, text: str, buttons: dict | None = None) -> None:
        try:
            if buttons:
                await self.center.monitor.notify(key, level, text, buttons=buttons)
            else:
                await self.center.monitor.notify(key, level, text)
        except Exception:  # noqa: BLE001
            log.exception("уведомление координатора")

    def _buttons(self, d: dict) -> dict | None:
        """Кнопки одобрения в Telegram, если у центра свой Telegram-бот с командами."""
        tg = getattr(self.center, "tg", None)
        if tg is None or not tg.target()[0]:
            return None
        return tg.keyboard(d)

    # ------------------------------------------------------------------ сторож аномалий
    def _lis_db(self):
        return self.center.analytics.lis_db

    def _mann_db(self):
        return self.center.analytics.mann_db

    def detect(self) -> list[dict]:
        """Правила без ИИ. Каждое: code, title, reason, tool, args, auto (явная аномалия → можно действовать самому)."""
        a = self.cfg.get("anomaly") or {}
        now = self.clock()
        out: list[dict] = []
        # 1. всплеск трат LisBot: за час больше factor × средний час за неделю и не меньше min_usd
        rows = _rows(self._lis_db(), "SELECT created_at, buy_price_usd FROM items WHERE dry_run=0 AND status<>'failed' "
                     "AND created_at>=?", ((now - 8 * 86400) * 1000,))
        hour = sum(r["buy_price_usd"] or 0 for r in rows if r["created_at"] >= (now - 3600) * 1000)
        week = sum(r["buy_price_usd"] or 0 for r in rows if r["created_at"] < (now - 3600) * 1000)
        base = week / (7 * 24)
        limit = max(float(a.get("spend_hour_min_usd") or 30), float(a.get("spend_hour_factor") or 3) * base)
        if hour > limit:
            out.append({"code": "lis_spend_spike", "auto": True, "tool": "pause_buying", "args": {"bot": "lisbot"},
                        "title": "LisBot: всплеск закупок",
                        "reason": f"LisBot потратил ${hour:.2f} за последний час при обычных ${base:.2f}/ч "
                                  f"(порог ${limit:.2f})"})
        # 2. серия убыточных продаж LisBot
        n = int(a.get("loss_streak") or 4)
        sold = _rows(self._lis_db(), "SELECT sold_at, profit_usd FROM items WHERE dry_run=0 AND status='sold' "
                     "ORDER BY sold_at DESC LIMIT ?", (n,))
        if len(sold) == n and all((r["profit_usd"] or 0) < 0 for r in sold) and sold[0]["sold_at"] >= (now - 86400) * 1000:
            loss = -sum(r["profit_usd"] for r in sold)
            if loss >= float(a.get("loss_streak_min_usd") or 5):
                out.append({"code": "lis_loss_streak", "auto": True, "tool": "pause_buying", "args": {"bot": "lisbot"},
                            "title": "LisBot: серия убыточных продаж",
                            "reason": f"{n} последних продаж LisBot подряд в минус, всего −${loss:.2f}"})
        # 3. всплеск возвратов MannBot за сутки — пауза торговли только предлагается
        refunds = _rows(self._mann_db(), "SELECT COUNT(*) n FROM orders WHERE status IN ('refunded','refund','cancelled') "
                        "AND created_at>=?", (now - 86400,))
        nref = refunds[0]["n"] if refunds else 0
        if nref >= int(a.get("refunds_24h") or 3):
            out.append({"code": "mann_refunds", "auto": False, "tool": "pause_bot", "args": {"bot": "mannbot"},
                        "title": "MannBot: много возвратов",
                        "reason": f"За сутки {nref} возвратов/отмен заказов MannBot"})
        # 4. предмет LisBot стабильно в минус — предложить выключить его закупку
        k, days = int(a.get("item_loss_sales") or 3), float(a.get("item_loss_days") or 30)
        for r in _rows(self._lis_db(), "SELECT i.name, COUNT(*) n, SUM(i.profit_usd) p FROM items i JOIN watchlist w "
                       "ON w.name=i.name WHERE i.dry_run=0 AND i.status='sold' AND i.sold_at>=? AND w.enabled=1 "
                       "GROUP BY i.name HAVING COUNT(*)>=? AND SUM(i.profit_usd)<=?",
                       ((now - days * 86400) * 1000, k, -float(a.get("item_loss_min_usd") or 3))):
            out.append({"code": f"lis_item_loss:{r['name']}", "auto": False, "tool": "disable_item", "args": {"name": r["name"]},
                        "title": f"LisBot: «{r['name']}» в минусе",
                        "reason": f"«{r['name']}»: {r['n']} продаж за {days:.0f} дн, итог −${-r['p']:.2f}. "
                                  "Предлагаю перестать его закупать (уже купленное продаётся как обычно)."})
        # 5. бот упал окончательно — центр перестал его перезапускать
        for name, w in self.center.workers.items():
            if w.state == CRASHED:
                out.append({"code": f"{name}_crashed", "auto": False, "tool": "restart_bot", "args": {"bot": name},
                            "title": f"{w.title}: упал", "reason": f"{w.title}: {w.detail or 'упал несколько раз подряд'}"})
        return out

    def _buy_paused(self) -> bool:
        s = self.center.summaries.get("lisbot") or {}
        return bool(s.get("paused")) or s.get("bot_state") in ("paused", "halted") or "закупка на паузе" in (s.get("flags") or [])

    async def check(self) -> list[dict]:
        found = self.detect()
        cooldown = float((self.cfg.get("anomaly") or {}).get("cooldown_hours") or 6) * 3600
        now = self.clock()
        for an in found:
            last = float(self.kv_get(f"anomaly:{an['code']}") or 0)
            if now - last < cooldown:
                continue
            self.kv_set(f"anomaly:{an['code']}", now)
            self._add("anomaly", "info", an["title"], tool=an["tool"], args=an["args"], reason=an["reason"],
                      source=f"rule:{an['code']}")
            if an["tool"] == "pause_buying" and self._buy_paused():
                await self._notify(f"coord:anomaly:{an['code']}", "warn",
                                   f"<b>🤖 Координатор: {html.escape(an['title'])}</b>\n{html.escape(an['reason'])}\n"
                                   "Закупки уже на паузе, ничего не делаю.")
                continue
            d = await self.act_or_propose(an["tool"], an["args"], reason=an["reason"], source=f"rule:{an['code']}",
                                          allow_auto=an["auto"])
            if d and d["kind"] == "proposal":
                kb = self._buttons(d)
                where = "кнопкой ниже или в панели центра" if kb else "в панели центра → «Координатор»"
                await self._notify(f"coord:anomaly:{an['code']}", "warn",
                                   f"<b>🤖 Координатор: {html.escape(an['title'])}</b>\n{html.escape(an['reason'])}\n"
                                   f"Предлагаю: {html.escape(d['title'])}. Одобрить можно {where}.", kb)
        self.last_check = {"at": now, "anomalies": [a["title"] for a in found]}
        return found

    # ------------------------------------------------------------------ факты, брифинг, вопросы
    async def facts(self) -> dict:
        c = self.center
        h = c.monitor.history(24)
        bots = []
        for name, w in c.workers.items():
            s = c.summaries.get(name)
            alerts = [x for x in w.alerts(30) if not x["acked"] and x["level"] not in ("info", "ok")]
            hb = h["bots"].get(name) or {}
            bots.append({"name": name, "title": w.title, "state": w.state, "state_text": STATE_TEXT.get(w.state, w.state),
                         "detail": w.detail, "restarts": w.restarts, "summary": s, "open_alerts": len(alerts),
                         "alerts": [{"level": x["level"], "message": x["message"]} for x in alerts[:5]],
                         "uptime_24h": hb.get("uptime"), "crashes_24h": hb.get("crashes"),
                         "unresponsive_24h": hb.get("unresponsive"), "latency_ms": hb.get("latency_avg")})
        out: dict = {"now": time.strftime("%Y-%m-%d %H:%M", time.localtime(self.clock())), "bots": bots}
        for key, days in (("pnl_1d", 1), ("pnl_7d", 7)):
            try:
                p = await c.analytics.pnl(days)
                out[key] = {"total": p["total"], "warnings": p["warnings"],
                            "bots": {k: {"deals": v["deals"], "USD": v["USD"], "RUB": v["RUB"]} for k, v in p["bots"].items()}}
            except Exception as e:  # noqa: BLE001
                out[key] = {"error": str(e)}
        with self._lock:
            crit = [dict(r) for r in self.db.execute(
                "SELECT source, kind, message FROM events WHERE ts>=? AND level IN ('critical','warn') ORDER BY id DESC LIMIT 15",
                (self.clock() - 86400,))]
        try:
            out["month_goal"] = await c.analytics.goal(self.clock())
        except Exception as e:  # noqa: BLE001
            out["month_goal"] = {"error": str(e)}
        try:
            out["inventory"] = c.inventory.brief()
        except Exception as e:  # noqa: BLE001
            out["inventory"] = {"error": str(e)}
        try:
            out["customers"] = await asyncio.to_thread(c.crm.segments_brief, self.clock())
        except Exception as e:  # noqa: BLE001
            out["customers"] = {"error": str(e)}
        out["events_24h"] = crit
        out["open_proposals"] = [{"id": p["id"], "title": p["title"], "tool": p["tool"], "args": p["args"]}
                                 for p in self.decisions(20, kind="proposal", status="proposed")]
        out["recent_decisions"] = [{"title": d["title"], "kind": d["kind"], "status": d["status"],
                                    "when": time.strftime("%d.%m %H:%M", time.localtime(d["ts"]))}
                                   for d in self.decisions(10) if d["kind"] in ("action", "proposal")]
        out["anomalies_24h"] = [d["reason"] for d in self.decisions(20, kind="anomaly") if d["ts"] >= self.clock() - 86400]
        out["permissions"] = {p["tool"]: p["tier"] for p in self.permissions()}
        return out

    async def _ai(self, facts: dict, schema: dict, question: str = "") -> tuple[dict, dict, str]:
        user = f"<facts>{json.dumps(facts, ensure_ascii=False, default=str)}</facts>"
        if question:
            user += f"\n<question>{question}</question>\nОтветь на вопрос FrayZ."
        else:
            user += "\nСоставь ежедневный брифинг центра и рекомендации."
        be = self.backend()
        try:
            data, meta = await be.complete(user, schema)
            return data, meta, "ai" if be.model != "stub" else "stub"
        except BackendError as e:
            log.warning("координатор: %s — использую сводку без ИИ", e)
            data, meta = await StubBackend().complete(user, schema)
            meta["error"] = str(e)
            return data, meta, "stub"

    async def _recos(self, recs: list[dict], source: str) -> list[int]:
        ids = []
        for r in recs[:5]:
            tool = r.get("tool") or "none"
            if tool == "none" or tool not in TOOLS:
                ids.append(self._add("proposal", "info", str(r.get("title") or "")[:200], reason=str(r.get("why") or ""),
                                     source=source, tier="propose"))
                continue
            try:
                args = json.loads(r.get("args_json") or "{}")
            except json.JSONDecodeError:
                args = {}
            d = await self.act_or_propose(tool, args if isinstance(args, dict) else {}, reason=str(r.get("why") or ""),
                                          source=source, title=str(r.get("title") or "")[:200])
            if d:
                ids.append(d["id"])
        return ids

    async def briefing(self, *, send: bool = True) -> dict:
        async with self._busy:
            f = await self.facts()
            data, meta, source = await self._ai(f, BRIEFING_SCHEMA)
            ids = await self._recos(data.get("recommendations") or [], source)
            text = str(data.get("summary") or "")
            did = self._add("briefing", "info", "Брифинг центра", result=text, source=source,
                            data={"highlights": data.get("highlights") or [], "recommendations": ids, "meta": meta})
            self.kv_set("briefing_date", time.strftime("%Y-%m-%d", time.localtime(self.clock())))
        b = self.get(did)
        if send and (self.cfg.get("briefing") or {}).get("telegram", True):
            recs = [self.get(i) for i in ids]
            lines = [f"<b>🤖 Брифинг координатора</b> · {time.strftime('%d.%m %H:%M', time.localtime(self.clock()))}",
                     "", html.escape(text)]
            hl = data.get("highlights") or []
            if hl:
                lines += [""] + [f"• {html.escape(str(x))}" for x in hl[:8]]
            if recs:
                lines += ["", "<b>Рекомендации</b>"] + [
                    f"{i + 1}. {html.escape(r['title'])}" + (" — нужно подтверждение" if r.get("tier") == "confirm" else "")
                    for i, r in enumerate(recs) if r]
                lines.append("<i>Одобрить или отклонить — в панели центра, раздел «Координатор»"
                             + (", или командой /proposals." if self._buttons({"kind": "proposal", "status": "proposed", "id": 0})
                                else ".") + "</i>")
            await self._notify("coord:briefing", "info", "\n".join(lines))
        self.events.add("center", "info", "coordinator.briefing", "Координатор подготовил брифинг")
        return b

    async def ask(self, question: str) -> dict:
        question = question.strip()[:2000]
        if not question:
            raise ToolError("пустой вопрос")
        async with self._busy:
            f = await self.facts()
            data, meta, source = await self._ai(f, ANSWER_SCHEMA, question)
            ids = await self._recos(data.get("recommendations") or [], source)
            did = self._add("answer", "info", question[:200], result=str(data.get("answer") or ""), source=source,
                            data={"recommendations": ids, "meta": meta})
        return self.get(did)

    # ------------------------------------------------------------------ жизненный цикл
    async def start(self) -> None:
        if not self.cfg.get("enabled", True):
            return
        self._tasks = [asyncio.create_task(self._check_loop(), name="coord-check"),
                       asyncio.create_task(self._briefing_loop(), name="coord-briefing")]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _check_loop(self) -> None:
        await asyncio.sleep(30)                      # дать ботам стартовать и центру собрать сводки
        while True:
            try:
                await self.check()
            except Exception:  # noqa: BLE001
                log.exception("сторож аномалий")
            await asyncio.sleep(max(15.0, float(self.cfg.get("check_seconds") or 60)))

    async def _briefing_loop(self) -> None:
        while True:
            try:
                br = self.cfg.get("briefing") or {}
                at = parse_hhmm(br.get("time") or "09:05")
                lt = time.localtime(self.clock())
                today = time.strftime("%Y-%m-%d", lt)
                if br.get("enabled", True) and at is not None and lt.tm_hour * 60 + lt.tm_min >= at \
                        and self.kv_get("briefing_date") != today:
                    self.kv_set("briefing_date", today)
                    if self.clock() - self.center.started_at < 180:
                        await asyncio.sleep(180)
                    await self.briefing()
            except Exception:  # noqa: BLE001
                log.exception("брифинг координатора")
            await asyncio.sleep(30)

    def status(self) -> dict:
        be = self.backend()
        ai = self.cfg.get("ai") or {}
        mode = "демо (заглушка)" if self.center.demo else \
            f"Claude ({be.model})" if be.model != "stub" else \
            ("ИИ выключен" if not ai.get("enabled", True) else self.backend_error
             or "нет ключа Claude API — сводки без ИИ")
        last = self.decisions(1, kind="briefing")
        return {"enabled": bool(self.cfg.get("enabled", True)), "ai": be.model != "stub", "mode": mode,
                "last_check": self.last_check, "briefing": last[0] if last else None,
                "open": len(self.decisions(200, kind="proposal", status="proposed")), "busy": self._busy.locked()}


def routes(center) -> list:
    """Маршруты панели для координатора; подключаются в center/web.py."""
    co: Coordinator = center.coordinator

    async def status(request: web.Request):
        return web.json_response({**co.status(), "proposals": co.decisions(50, kind="proposal", status="proposed"),
                                  "journal": co.decisions(int(request.query.get("limit", 40)))})

    async def settings(request: web.Request):
        if request.method == "POST":
            try:
                co.save_settings(await request.json())
            except (ValueError, TypeError) as e:
                return web.json_response({"ok": False, "message": str(e)}, status=400)
            center.events.add("center", "info", "settings", "Настройки координатора изменены из панели центра")
            return web.json_response({"ok": True, "message": "Настройки координатора сохранены"})
        return web.json_response(co.settings())

    async def briefing(request: web.Request):
        if co._busy.locked():
            return web.json_response({"ok": False, "message": "координатор уже думает, подождите"}, status=409)
        b = await co.briefing(send=bool((await request.json()).get("send")) if request.can_read_body else False)
        return web.json_response({"ok": True, "briefing": b})

    async def ask(request: web.Request):
        try:
            a = await co.ask(str((await request.json()).get("question") or ""))
        except ToolError as e:
            return web.json_response({"ok": False, "message": str(e)}, status=400)
        return web.json_response({"ok": True, "answer": a, "recommendations": [co.get(i) for i in a["data"]["recommendations"]]})

    async def decide(request: web.Request):
        d = await request.json() if request.can_read_body else {}
        ok, msg = await co.decide(int(request.match_info["id"]), request.match_info["verb"] == "approve",
                                  confirmed=bool(d.get("confirm")))
        return web.json_response({"ok": ok, "message": msg}, status=200 if ok else 409)

    async def check(request: web.Request):
        found = await co.check()
        return web.json_response({"ok": True, "message": f"аномалий: {len(found)}" if found else "аномалий нет",
                                  "anomalies": found})

    return [
        web.get("/api/coordinator/status", status), web.get("/api/coordinator/settings", settings),
        web.post("/api/coordinator/settings", settings), web.post("/api/coordinator/briefing", briefing),
        web.post("/api/coordinator/ask", ask), web.post("/api/coordinator/check", check),
        web.post(r"/api/coordinator/decisions/{id:\d+}/{verb:approve|reject}", decide),
    ]
