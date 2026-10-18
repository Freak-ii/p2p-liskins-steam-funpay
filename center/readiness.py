"""Готовность к боевому режиму: что ещё не настроено или настроено опасно, до того как боты начнут тратить деньги.

Проверки только читают настройки и зашифрованное хранилище центра, сами ключи наружу не отдаются.
Главное — LisBot и MannBot должны работать на разных аккаунтах Steam (LisBot отменяет чужие исходящие трейды
как взлом и останавливается) и с разными Telegram-ботами (два процесса не могут читать один getUpdates).
"""
from __future__ import annotations

import re

from aiohttp import web

STEAM64_BASE = 76561197960265728
LEVELS = ("fail", "warn", "info", "ok")


def _partner_steam64(trade_url: str) -> str:
    m = re.search(r"[?&]partner=(\d+)", trade_url or "")
    return str(STEAM64_BASE + int(m.group(1))) if m else ""


class Readiness:
    def __init__(self, center):
        self.center = center

    def _lis(self) -> tuple[dict, dict]:
        w = self.center.workers.get("lisbot")
        try:
            sec = w.secrets() if w is not None else {}
        except Exception:  # noqa: BLE001 — хранилище не расшифровано
            sec = {}
        return sec, self.center.analytics.lis_config()

    def _mann(self) -> tuple[dict, dict]:
        w = self.center.workers.get("mannbot")
        cfg = self.center.analytics.mann_config()
        try:
            stored = w.stored_secrets() if w is not None else {}
        except Exception:  # noqa: BLE001
            stored = {}

        def get(section: str, key: str) -> str:
            node = cfg
            for part in section.split("."):
                node = (node or {}).get(part) or {}
            return str(stored.get((section, key)) or (node.get(key) if isinstance(node, dict) else "") or "").strip()
        return {"get": get}, cfg

    def check(self) -> dict:
        items: list[dict] = []

        def add(key: str, level: str, title: str, detail: str = "", where: str = "") -> None:
            items.append({"key": key, "level": level, "title": title, "detail": detail, "where": where})

        lis, lcfg = self._lis()
        mann, mcfg = self._mann()
        g = mann["get"]
        summaries = self.center.summaries

        # ---- LisBot
        if lis.get("LIS_API_KEY"):
            add("lis_api", "ok", "LisBot: ключ API LIS-SKINS задан")
        else:
            add("lis_api", "fail", "LisBot: не задан ключ API LIS-SKINS", where="LisBot → «Подключения»")
        miss = [n for k, n in (("STEAM_ACCOUNT_NAME", "логин"), ("STEAM_PASSWORD", "пароль"),
                               ("STEAM_SHARED_SECRET", "shared_secret"), ("STEAM_IDENTITY_SECRET", "identity_secret"))
                if not lis.get(k)]
        add("lis_steam", "fail" if miss else "ok",
            f"LisBot: Steam — не хватает: {', '.join(miss)}" if miss else "LisBot: вход в Steam настроен",
            where="LisBot → «Подключения»" if miss else "")
        if not lis.get("STEAM_TRADE_URL"):
            add("lis_trade_url", "fail", "LisBot: не задана трейд-ссылка Steam", "На неё LIS-SKINS отправляет купленные предметы.",
                "LisBot → «Подключения»")
        lis_dry = (summaries.get("lisbot") or {}).get("dry_run", lcfg.get("dryRun", True))
        add("lis_mode", "info" if lis_dry else "ok",
            "LisBot в тестовом режиме: покупки не совершаются" if lis_dry else "LisBot в боевом режиме")

        # ---- MannBot
        add("mann_funpay", "ok" if g("funpay", "golden_key") else "fail",
            "MannBot: golden_key FunPay задан" if g("funpay", "golden_key") else "MannBot: не задан golden_key FunPay",
            where="" if g("funpay", "golden_key") else "MannBot → «Подключения»")
        login = g("steam", "login_secure_cookie") or (g("steam", "username") and g("steam", "password") and g("steam", "shared_secret"))
        add("mann_steam", "ok" if login else "fail",
            "MannBot: вход в Steam настроен" if login else
            "MannBot: для Steam нужен login_secure_cookie или логин + пароль + shared_secret", where="" if login else "MannBot → «Подключения»")
        if not g("steam", "identity_secret"):
            add("mann_confirm", "fail", "MannBot: не задан identity_secret",
                "Без него обмены с ключами не подтверждаются, покупатели не получат ключи.", "MannBot → «Подключения»")
        cost = float(((mcfg.get("rtba") or {}).get("cost_per_key")) or 0)
        add("mann_cost", "ok" if cost > 0 else "warn",
            f"MannBot: себестоимость ключа {cost:g} ₽" if cost > 0 else "MannBot: не задана себестоимость ключа",
            "" if cost > 0 else "Без неё центр не считает прибыль MannBot, а RTBA не держит минимальную маржу.",
            "" if cost > 0 else "RTBA и лимиты → MannBot")

        # ---- разные аккаунты Steam
        lis_name, mann_name = (lis.get("STEAM_ACCOUNT_NAME") or "").strip().lower(), g("steam", "username").lower()
        lis_id, mann_id = _partner_steam64(lis.get("STEAM_TRADE_URL", "")), g("steam", "steam_id")
        same = (lis_name and lis_name == mann_name) or (lis_id and lis_id == mann_id)
        if same:
            add("steam_separate", "fail", "LisBot и MannBot на одном аккаунте Steam",
                "LisBot считает чужие исходящие обмены взломом: отменит выдачу ключей MannBot и остановится. "
                "Нужен отдельный аккаунт Steam для каждого бота.", "Подключения обоих ботов")
        elif (lis_name or lis_id) and (mann_name or mann_id):
            add("steam_separate", "ok", "LisBot и MannBot на разных аккаунтах Steam")

        # ---- Telegram: у каждого свой бот
        tokens = {"LisBot": lis.get("TELEGRAM_BOT_TOKEN", ""), "MannBot": g("notifications.telegram", "bot_token")}
        mon = self.center.monitor
        center_token = mon._token()
        if center_token and (mon.cfg.get("telegram") or {}).get("commands", True):
            tokens["центр"] = center_token
        by_token: dict[str, list[str]] = {}
        for who, t in tokens.items():
            if t:
                by_token.setdefault(t, []).append(who)
        clash = [who for who in by_token.values() if len(who) > 1]
        if clash:
            add("tg_separate", "fail", "Один Telegram-бот у нескольких: " + "; ".join(" и ".join(w) for w in clash),
                "Два процесса не могут читать команды одного бота: команды и кнопки будут теряться. Создайте отдельного "
                "бота в @BotFather для каждого.", "Подключения ботов и «Настройки уведомлений»")
        token, ids, src = mon.telegram_target()
        add("tg_center", "ok" if token else "warn",
            f"Уведомления центра в Telegram: {src}" if token else "Уведомления центра в Telegram не настроены",
            "" if token else "Падения ботов и тревоги будут видны только в панели.", "" if token else "Мониторинг → «Настройки уведомлений»")

        # ---- страховка
        b = getattr(self.center, "backup", None)
        if b is not None:
            on = bool(b.cfg.get("enabled", True))
            add("backup", "ok" if on else "warn", "Резервные копии по расписанию включены" if on else "Резервные копии выключены",
                where="" if on else "Резервные копии → «Расписание и папка»")
        hb = ((mon.cfg.get("heartbeat") or {}).get("url") or "").strip()
        add("heartbeat", "ok" if hb else "info", "Сторожевой пинг наружу настроен" if hb else "Сторожевой пинг не настроен",
            "" if hb else "Без него никто не заметит, что выключился сам компьютер с центром.",
            "" if hb else "Мониторинг → «Настройки уведомлений»")
        try:
            low = [d for d in mon.disk()["disks"] if d["low"]]
        except Exception:  # noqa: BLE001
            low = []
        if low:
            add("disk", "warn", "Мало места на диске: " + ", ".join(f"{d['label']} {d['free_gb']:.1f} ГБ" for d in low))
        budget = float((self.center.cfg.data.get("budget") or {}).get("total_usd") or 0)
        add("budget", "ok" if budget else "info", f"Общий капитал центра: ${budget:g}" if budget else "Общий капитал центра не задан",
            "" if budget else "Лимиты ботов не согласованы с вашим бюджетом.", "" if budget else "RTBA и лимиты")

        items.sort(key=lambda i: LEVELS.index(i["level"]))
        counts = {lv: sum(1 for i in items if i["level"] == lv) for lv in LEVELS}
        return {"ready": counts["fail"] == 0, "counts": counts, "items": items}

    def lines(self) -> list[str]:
        r = self.check()
        bad = [i for i in r["items"] if i["level"] in ("fail", "warn")]
        if not bad:
            return ["✅ К боевому режиму всё готово"]
        return [("⛔ " if i["level"] == "fail" else "⚠ ") + i["title"] + (f" ({i['where']})" if i["where"] else "") for i in bad]


def routes(center) -> list:
    async def readiness(request: web.Request):
        if center.demo:
            return web.json_response({"ready": True, "demo": True, "counts": {}, "items": []})
        return web.json_response(center.readiness.check())

    return [web.get("/api/readiness", readiness)]
