"""Готовность к боевому режиму: что ещё не настроено или настроено опасно, до того как боты начнут тратить деньги.

Проверки только читают настройки и зашифрованное хранилище центра, сами ключи наружу не отдаются.
Главное — LisBot и MannBot должны работать на разных аккаунтах Steam (LisBot отменяет чужие исходящие трейды
как взлом и останавливается) и с разными Telegram-ботами (два процесса не могут читать один getUpdates).
"""
from __future__ import annotations

import re

from aiohttp import web

from center import tgcheck
from center.config import check_config

STEAM64_BASE = 76561197960265728
LEVELS = ("fail", "warn", "info", "ok")
_STEAM_SECRET = re.compile(r"^[A-Za-z0-9+/]{27}=$")       # shared/identity_secret из maFile: base64 от 20 байт


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
        bad = [n for k, n in (("STEAM_SHARED_SECRET", "shared_secret"), ("STEAM_IDENTITY_SECRET", "identity_secret"))
               if lis.get(k) and not _STEAM_SECRET.match(lis[k].strip())]
        if bad:
            add("lis_secret_format", "fail", f"LisBot: {', '.join(bad)} не похож на ключ из maFile",
                "В maFile Steam Desktop Authenticator это строка из 28 символов, которая кончается на «=». "
                "Если maFile зашифрован, снимите шифрование в SDA и скопируйте значение заново, без кавычек.",
                "LisBot → «Подключения»")
        add("lis_steam", "fail" if miss else "ok",
            f"LisBot: Steam — не хватает: {', '.join(miss)}" if miss else "LisBot: вход в Steam настроен",
            where="LisBot → «Подключения»" if miss else "")
        lsum = summaries.get("lisbot") or {}
        if not miss and lsum.get("steam_ready") is False:
            # настройки есть, но вход не удался: не показываем «готово», пока бот не в Steam
            err = lsum.get("steam_error") or ("" if lsum.get("bot_state") == "running" else lsum.get("error") or "")
            if not err:
                add("lis_steam_online", "info", "LisBot: входит в Steam…")
            else:
                net = any(w in err for w in ("getaddrinfo", "ENOTFOUND", "ENOENT", "Нет связи"))
                add("lis_steam_online", "fail", "LisBot: нет входа в Steam", err.rstrip(".") + ". " + (
                    "Это ошибка сети или DNS на компьютере: проверьте интернет, VPN и антивирус. "
                    "Бот повторяет вход сам и не тратит на это суточный лимит логинов." if net else
                    "Если Steam отклонил код, проверьте shared_secret из maFile SDA и время на компьютере."),
                    "LisBot → «Тревоги»")
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
        shared = self.center.steamshare.shared
        if same and not shared:
            add("steam_separate", "fail", "LisBot и MannBot на одном аккаунте Steam",
                "В center.yaml выключен режим одного аккаунта (steam.shared_account: false), поэтому LisBot считает "
                "обмены MannBot чужими: отменит выдачу ключей и остановится. Включите steam.shared_account "
                "или дайте ботам разные аккаунты.", "center.yaml → steam.shared_account")
        elif same:
            add("steam_separate", "ok", "LisBot и MannBot на одном аккаунте Steam",
                "MannBot записывает свои обмены в центр, LisBot пропускает только их. Остальные чужие обмены "
                "по-прежнему останавливают LisBot.")
        elif (lis_name or lis_id) and (mann_name or mann_id):
            add("steam_separate", "ok", "LisBot и MannBot на разных аккаунтах Steam")

        # ---- мост ключей: трейд-ссылка в LisBot ведёт на MannBot, MannBot принимает ключи от LisBot
        kb = lcfg.get("keyBridge") if isinstance(lcfg.get("keyBridge"), dict) else {}
        sec_m = mcfg.get("security") if isinstance(mcfg.get("security"), dict) else {}
        gifts = sec_m.get("accept_gifts_from") or []
        gifts = {str(x).strip() for x in (gifts if isinstance(gifts, list) else [gifts]) if str(x).strip()}
        to = _partner_steam64(kb.get("tradeUrl", ""))
        if kb.get("enabled") and same and (not str(kb.get("tradeUrl") or "").strip() or to == lis_id):
            add("key_bridge", "ok", "Ключи с кошелька Steam: LisBot покупает их в общий инвентарь",
                "MannBot продаёт их сам, когда пройдут 7 дней блокировки обмена.")
        elif kb.get("enabled") and same:
            add("key_bridge", "warn", "Мост ключей отправляет ключи на другой аккаунт",
                f"Оба бота на одном аккаунте, а трейд-ссылка моста ведёт на SteamID {to}. Очистите её, "
                "чтобы ключи оставались в общем инвентаре.", "LisBot: «Открыть панель бота» → «Настройки» → «Мост ключей»")
        elif kb.get("enabled") or gifts:
            where = "LisBot: «Открыть панель бота» → «Настройки» → «Мост ключей»; MannBot: «Настройки» → «Безопасность»"
            bad = []
            if kb.get("enabled"):
                if not to:
                    bad.append("в LisBot не задана трейд-ссылка аккаунта MannBot (keyBridge.tradeUrl): "
                               "купленные ключи останутся на аккаунте LisBot")
                elif to == lis_id:
                    bad.append("трейд-ссылка моста ведёт на сам аккаунт LisBot, нужна ссылка MannBot")
                elif mann_id and to != mann_id:
                    bad.append(f"трейд-ссылка моста ведёт на SteamID {to}, а у MannBot SteamID {mann_id}")
                if not gifts:
                    bad.append("MannBot не принимает ключи: security.accept_gifts_from пуст"
                               + (f" (впишите SteamID64 аккаунта LisBot: {lis_id})" if lis_id else ""))
                elif lis_id and lis_id not in gifts:
                    bad.append(f"в security.accept_gifts_from у MannBot нет SteamID аккаунта LisBot ({lis_id})")
            if bad:
                text = "; ".join(bad)
                add("key_bridge", "warn", "Мост ключей настроен не до конца", text[:1].upper() + text[1:] + ".", where)
            elif not kb.get("enabled"):
                add("key_bridge", "info", "Мост ключей выключен в LisBot",
                    "MannBot готов принимать ключи от LisBot, но LisBot их не отправляет.", where)
            else:
                add("key_bridge", "ok", "Мост ключей настроен: LisBot отправляет ключи на аккаунт MannBot")

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
        mtg = ((mcfg.get("notifications") or {}).get("telegram") or {}) if isinstance(mcfg, dict) else {}
        chats = {"LisBot": (tokens["LisBot"], lis.get("TELEGRAM_CHAT_ID", "")),
                 "MannBot": (tokens["MannBot"], mtg.get("admin_chat_ids") or []),
                 "центр": (center_token, str((mon.cfg.get("telegram") or {}).get("chat_id") or ""))}
        wrong = [f"{who}: {p}" for who, (t, ids) in chats.items() if t and tgcheck.split_ids(ids)
                 for p in tgcheck.static_problems(t, ids)]
        if wrong:
            add("tg_chat_id", "fail", "Telegram настроен с ошибкой, бот будет молчать", "; ".join(wrong) + ".",
                "Подключения ботов и «Настройки уведомлений» (там же кнопка проверки)")
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

        cfg = self.center.cfg
        cfg_problems = [(lv, t) for lv, t in getattr(cfg, "problems", []) if lv == "critical"] + \
            [("warn", t) for t in check_config(cfg.user)]          # опечатки — по текущему файлу, после правки пропадают
        if cfg_problems:
            crit = any(lv == "critical" for lv, _ in cfg_problems)
            add("center_yaml", "fail" if crit else "warn",
                "center.yaml был с ошибкой и не прочитан" if crit else f"В center.yaml есть опечатки: {len(cfg_problems)}",
                "; ".join(t for _, t in cfg_problems[:3]) + (f" и ещё {len(cfg_problems) - 3}" if len(cfg_problems) > 3 else ""),
                "center.yaml в папке данных центра")
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
