"""Проверка ответа ИИ перед отправкой покупателю: правила FunPay и безопасность продавца.

Проверки работают в коде, а не в промпте: что бы ни написал покупатель (в том числе попытку
«переубедить» модель), ответ с нарушением не уйдёт автоматически и не будет одобрен в один клик.
"""
from __future__ import annotations

import re

from mannbot.core import messages as M

ALLOWED_HOSTS = ("steamcommunity.com", "funpay.com")
# Статусы, после которых можно просить подтвердить заказ (п. 2.1.2 — только после фактической выдачи)
CONFIRM_OK_STATUSES = {"delivered", "completed"}

_URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+|\b[a-z0-9][a-z0-9-]{1,62}\.(?:com|ru|net|org|me|gg|io|su|"
                     r"xyz|cc|ly|to|link|shop|store|pro|top|site|online|app)(?:/\S*)?\b")
_HANDLE_RE = re.compile(r"(?<![\w.])@[a-z0-9_]{4,}", re.I)
_PHONE_RE = re.compile(r"(?<!\d)\+?\d[\d\s\-()]{9,}\d")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_CONFIRM_RE = re.compile(r"(?i)(подтверд\w*\s+(?:заказ|выполнени|получени)|confirm\w*\s+(?:the\s+)?order)")
_SECRET_ASK_RE = re.compile(r"(?i)(пришлите|отправьте|скиньте|напишите|сообщите|укажите|дайте|send|give|tell)"
                            r"[^.!?\n]{0,40}(парол|логин|код\w*\s+(?:steam\s*)?guard|api[\s-]*(?:ключ|key)|"
                            r"password|login|guard\s+code|mafile)")
_REFUND_PROMISE_RE = re.compile(r"(?i)(верн(?:ём|ем|у)\s+(?:вам\s+)?(?:деньги|средства)|оформ\w+\s+возврат|"
                                r"возврат\s+(?:будет|оформлен)|we\s*(?:'ll|\s+will)\s+refund|refund\s+(?:is|has been)\s+issued)")
_DISCOUNT_RE = re.compile(r"(?i)(скидк\w+|discount|бесплатн\w+\s+ключ|free\s+key|бонус\w*\s+ключ)")

ISSUE_TEXT = {
    "empty": "пустой ответ",
    "too_long": "слишком длинный ответ",
    "link": "ссылка вне steamcommunity.com / funpay.com",
    "contact": "контакты или общение вне FunPay",
    "confirm_too_early": "просьба подтвердить заказ до выдачи ключей",
    "asks_secrets": "просит логин, пароль, коды или API-ключ",
    "refund_promise": "обещает возврат денег (решает только продавец)",
    "discount_promise": "обещает скидку или бесплатные ключи",
}


def _hosts(text: str) -> list[str]:
    out = []
    for m in _URL_RE.finditer(text):
        u = m.group(0).lower().rstrip(".,!?)»\"'")
        u = re.sub(r"^(https?://)?(www\.)?", "", u)
        out.append(u.split("/")[0].split("?")[0])
    return out


def check_reply(text: str, *, order_status: str | None = None, max_chars: int = 700) -> list[str]:
    """Список кодов нарушений (пустой — ответ можно отправлять)."""
    t = (text or "").strip()
    if not t:
        return ["empty"]
    issues: list[str] = []
    if len(t) > max_chars:
        issues.append("too_long")
    for host in _hosts(t):
        if not any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS):
            issues.append("link")
            break
    no_mail = _EMAIL_RE.sub(" ", t)
    if (M.is_off_platform_request(t) or _HANDLE_RE.search(no_mail) or _EMAIL_RE.search(t)
            or _PHONE_RE.search(_URL_RE.sub(" ", t))):
        issues.append("contact")
    if _CONFIRM_RE.search(t) and (order_status or "") not in CONFIRM_OK_STATUSES:
        issues.append("confirm_too_early")
    if _SECRET_ASK_RE.search(t):
        issues.append("asks_secrets")
    if _REFUND_PROMISE_RE.search(t):
        issues.append("refund_promise")
    if _DISCOUNT_RE.search(t):
        issues.append("discount_promise")
    return issues


def describe(issues: list[str]) -> str:
    return "; ".join(ISSUE_TEXT.get(i, i) for i in issues)
