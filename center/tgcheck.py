"""Проверка Telegram-бота: токен, chat id и пробное сообщение — с понятным объяснением, что не так.

Частая ошибка при настройке — в поле chat id вставляют id самого бота (число в начале токена до двоеточия),
а нужен id своего аккаунта (его присылает @userinfobot). Писать другому боту Telegram не даёт, а команды
боты принимают только из чата с указанным id, поэтому бот просто «молчит».
"""
from __future__ import annotations

import asyncio
import re

import aiohttp

TOKEN_RE = re.compile(r"^(\d+):[\w-]+$")


def bot_id(token: str) -> str:
    m = TOKEN_RE.match((token or "").strip())
    return m.group(1) if m else ""


def split_ids(chat_ids) -> list[str]:
    if isinstance(chat_ids, (list, tuple)):
        return [str(c).strip() for c in chat_ids if str(c).strip()]
    return [c.strip() for c in str(chat_ids or "").split(",") if c.strip()]


def static_problems(token: str, chat_ids) -> list[str]:
    """Ошибки, видные без обращения к Telegram."""
    token = (token or "").strip()
    ids = split_ids(chat_ids)
    out: list[str] = []
    if not token:
        out.append("не указан токен бота")
    elif not bot_id(token):
        out.append("токен не похож на токен от @BotFather (вид: 1234567890:AAH…), скопируйте его целиком")
    if not ids:
        out.append("не указан chat id (ваш id пришлёт @userinfobot)")
    bid = bot_id(token)
    for c in ids:
        if bid and c == bid:
            out.append(f"chat id {c} — это id самого бота (начало токена). Нужен id вашего аккаунта: "
                       "напишите @userinfobot, он ответит числом Id")
        elif not re.fullmatch(r"-?\d+", c):
            out.append(f"chat id «{c}» должен быть числом (ваш id из @userinfobot, не имя пользователя)")
    return out


def explain(description: str) -> str:
    d = (description or "").lower()
    if "unauthorized" in d or d.strip() == "not found":
        return "Telegram не принял токен: проверьте, что скопирован весь токен от @BotFather"
    if "chat not found" in d:
        return "чат не найден: откройте своего бота в Telegram и нажмите Start (/start), и проверьте, что chat id ваш"
    if "bots can't send messages to bots" in d:
        return "указан id бота, а нужен id вашего аккаунта (@userinfobot)"
    if "blocked by the user" in d:
        return "вы заблокировали бота: разблокируйте его в Telegram и нажмите Start"
    if "can't initiate conversation" in d or "bot can't initiate" in d:
        return "откройте своего бота в Telegram и нажмите Start (/start): первым бот написать не может"
    return description or "неизвестная ошибка Telegram"


async def check(token: str, chat_ids, who: str = "Центр", api: str = "https://api.telegram.org") -> dict:
    """{ok, message, problems}. api — адрес Bot API (в тестах — подставной сервер)."""
    problems = static_problems(token, chat_ids)
    if problems:
        return {"ok": False, "message": "Telegram: " + "; ".join(problems), "problems": problems}
    token = token.strip()
    url = f"{api}/bot{token}"
    notes: list[str] = []
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async def call(method: str, payload: dict | None = None) -> dict:
                async with s.post(f"{url}/{method}", json=payload or {}) as r:
                    try:
                        return await r.json(content_type=None)
                    except (ValueError, aiohttp.ContentTypeError):
                        return {"ok": False, "description": f"HTTP {r.status}"}

            me = await call("getMe")
            if not me.get("ok"):
                p = explain(me.get("description", ""))
                return {"ok": False, "message": f"Telegram: {p}", "problems": [p]}
            name = "@" + str((me.get("result") or {}).get("username") or "бот")
            hook = await call("getWebhookInfo")
            if hook.get("ok") and (hook.get("result") or {}).get("url"):
                notes.append("на боте включён вебхук, из-за него бот не видит команды: откройте в браузере "
                             f"api.telegram.org/bot<токен>/deleteWebhook или создайте нового бота")
            for c in split_ids(chat_ids):
                r = await call("sendMessage", {"chat_id": c, "text": f"✅ {who}: Telegram подключён, бот {name} пишет вам."})
                if not r.get("ok"):
                    p = explain(r.get("description", ""))
                    return {"ok": False, "message": f"Telegram ({name}, chat id {c}): {p}", "problems": [p] + notes}
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        p = f"нет связи с api.telegram.org ({e.__class__.__name__}): проверьте интернет, VPN или прокси"
        return {"ok": False, "message": "Telegram: " + p, "problems": [p]}
    msg = f"Сообщение от {name} отправлено, проверьте Telegram."
    if notes:
        msg += " Но " + "; ".join(notes) + "."
    return {"ok": not notes, "message": msg, "problems": notes}
