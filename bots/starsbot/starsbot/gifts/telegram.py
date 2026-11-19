"""Telegram-аккаунт продавца (userbot на Telethon): вход по номеру, поиск получателя по @username,
баланс звёзд, каталог подарков и отправка подарков.

Подарок оплачивается звёздами с баланса этого аккаунта. Отправка необратима, поэтому каждый подарок
записывается в базу ДО запроса, а при неясном исходе (обрыв связи посреди запроса) заказ уходит продавцу —
бот сверяет историю звёзд аккаунта и не отправляет подарок повторно вслепую.

Нужны api_id и api_hash своего приложения (my.telegram.org → API development tools) — чужие ключи
официальных клиентов использовать нельзя: Telegram за это блокирует аккаунты.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from starsbot.gifts.planner import Gift

log = logging.getLogger("gifts")


class GiftError(Exception):
    """code: not_found | bad_format | cannot_receive | no_stars | auth | network | flood | unknown | config"""

    def __init__(self, text: str, code: str = "unknown"):
        super().__init__(text)
        self.code = code


@dataclass
class TgUser:
    user_id: int
    access_hash: int
    username: str
    name: str = ""
    bot: bool = False

    @property
    def ref(self) -> str:
        return f"{self.user_id}:{self.access_hash}"


def _rpc_code(e: Exception) -> str:
    name = type(e).__name__
    text = (getattr(e, "message", "") or str(e)).upper()
    if "FLOOD" in name.upper() or "FLOOD" in text:
        return "flood"
    if "BALANCE_TOO_LOW" in text or "STARS" in text and "LOW" in text:
        return "no_stars"
    if "USERNAME_INVALID" in text:
        return "bad_format"
    if "USERNAME_NOT_OCCUPIED" in text or "PEER_ID_INVALID" in text or "USER_ID_INVALID" in text:
        return "not_found"
    if "PRIVACY" in text or "USER_DISALLOWED" in text or "GIFT" in text and "DISALLOW" in text:
        return "cannot_receive"
    if "AUTH" in text or "SESSION" in text:
        return "auth"
    return "unknown"


class TelegramGifts:
    """Обёртка над Telethon. Сессия хранится строкой (StringSession) в файле рядом с базой."""

    def __init__(self, api_id: int, api_hash: str, session_file: str | Path, *, dry_run: bool = False,
                 proxy: dict | None = None):
        self.api_id = int(api_id or 0)
        self.api_hash = str(api_hash or "")
        self.session_file = Path(session_file)
        self.dry_run = dry_run
        self.proxy = proxy
        self.client = None
        self.me: TgUser | None = None
        self._lock = asyncio.Lock()
        self._login_hash: dict[str, str] = {}
        self._catalog: tuple[float, list[Gift]] = (0.0, [])

    # ------------------------------------------------------------------ сессия
    def _session_str(self) -> str:
        try:
            return self.session_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _save_session(self) -> None:
        if self.client is None:
            return
        s = self.client.session.save()
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.session_file.with_suffix(".tmp")
        tmp.write_text(s, encoding="utf-8")
        tmp.replace(self.session_file)

    @property
    def has_session(self) -> bool:
        return bool(self._session_str())

    async def _client(self):
        if not self.api_id or not self.api_hash:
            raise GiftError("укажите api_id и api_hash (my.telegram.org → API development tools)", "config")
        if self.client is None:
            try:
                from telethon import TelegramClient
                from telethon.sessions import StringSession
            except ImportError as e:  # pragma: no cover
                raise GiftError("не установлен пакет telethon (pip install telethon)", "config") from e
            self.client = TelegramClient(StringSession(self._session_str() or None), self.api_id, self.api_hash,
                                         device_model="StarsBot", system_version="1.0", app_version="1.0",
                                         lang_code="ru", system_lang_code="ru", proxy=self.proxy)
        if not self.client.is_connected():
            try:
                await self.client.connect()
            except (OSError, ConnectionError) as e:
                raise GiftError(f"нет связи с Telegram: {e}", "network") from e
        return self.client

    async def close(self) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def logout_local(self) -> None:
        await self.close()
        self.client = None
        self.me = None
        try:
            self.session_file.unlink()
        except OSError:
            pass

    # ------------------------------------------------------------------ вход по номеру
    async def send_code(self, phone: str) -> str:
        c = await self._client()
        try:
            sent = await c.send_code_request(phone)
        except Exception as e:  # noqa: BLE001
            raise GiftError(f"Telegram не отправил код: {e}", _rpc_code(e)) from e
        self._login_hash[phone] = sent.phone_code_hash
        return "Код отправлен в Telegram (сообщение от «Telegram» на других устройствах) или по SMS"

    async def sign_in(self, phone: str, code: str = "", password: str = "") -> TgUser:
        c = await self._client()
        try:
            from telethon.errors import SessionPasswordNeededError
        except ImportError:  # pragma: no cover
            SessionPasswordNeededError = Exception  # noqa: N806
        try:
            if code:
                try:
                    await c.sign_in(phone=phone, code=code.strip().replace(" ", ""),
                                    phone_code_hash=self._login_hash.get(phone))
                except SessionPasswordNeededError:
                    if not password:
                        raise GiftError("на аккаунте включена двухэтапная проверка — введите облачный пароль",
                                        "password_needed")
                    await c.sign_in(password=password)
            elif password:
                await c.sign_in(password=password)
        except GiftError:
            raise
        except Exception as e:  # noqa: BLE001
            raise GiftError(f"вход не выполнен: {e}", _rpc_code(e)) from e
        self._save_session()
        return await self.whoami()

    async def whoami(self) -> TgUser:
        c = await self._client()
        if not await c.is_user_authorized():
            raise GiftError("Telegram-аккаунт не подключён: войдите по номеру в разделе «Подарки»", "auth")
        u = await c.get_me()
        self.me = TgUser(u.id, u.access_hash or 0, u.username or "", " ".join(x for x in (u.first_name, u.last_name) if x))
        return self.me

    # ------------------------------------------------------------------ звёзды и каталог
    async def stars_balance(self) -> int:
        from telethon.tl import functions, types
        c = await self._client()
        try:
            st = await c(functions.payments.GetStarsStatusRequest(peer=types.InputPeerSelf()))
        except Exception as e:  # noqa: BLE001
            raise GiftError(f"баланс звёзд недоступен: {e}", _rpc_code(e)) from e
        bal = st.balance
        return int(getattr(bal, "amount", bal) or 0)

    async def catalog(self, max_age: float = 3600) -> list[Gift]:
        if self._catalog[1] and time.time() - self._catalog[0] < max_age:
            return self._catalog[1]
        from telethon.tl import functions
        c = await self._client()
        try:
            res = await c(functions.payments.GetStarGiftsRequest(hash=0))
        except Exception as e:  # noqa: BLE001
            raise GiftError(f"каталог подарков недоступен: {e}", _rpc_code(e)) from e
        out = []
        for g in getattr(res, "gifts", None) or []:
            if type(g).__name__ != "StarGift":
                continue
            emoji = ""
            for a in getattr(getattr(g, "sticker", None), "attributes", []) or []:
                emoji = getattr(a, "alt", "") or emoji
            out.append(Gift(int(g.id), int(g.stars), int(g.convert_stars or 0), str(getattr(g, "title", "") or ""),
                            limited=bool(g.limited), sold_out=bool(g.sold_out),
                            require_premium=bool(getattr(g, "require_premium", False)), emoji=emoji))
        if out:
            self._catalog = (time.time(), out)
        return out

    # ------------------------------------------------------------------ получатель
    async def resolve(self, username: str) -> TgUser:
        from telethon.tl import functions
        c = await self._client()
        try:
            res = await c(functions.contacts.ResolveUsernameRequest(username=username.lstrip("@")))
        except Exception as e:  # noqa: BLE001
            code = _rpc_code(e)
            raise GiftError(f"@{username}: {e}", "not_found" if code == "unknown" else code) from e
        users = [u for u in (res.users or []) if getattr(u, "id", None) == getattr(res.peer, "user_id", None)]
        if not users:
            raise GiftError(f"@{username} — это канал или группа, а не аккаунт", "cannot_receive")
        u = users[0]
        if getattr(u, "deleted", False):
            raise GiftError(f"@{username}: аккаунт удалён", "not_found")
        return TgUser(u.id, u.access_hash or 0, u.username or username,
                      " ".join(x for x in (u.first_name, u.last_name) if x), bool(u.bot))

    # ------------------------------------------------------------------ отправка
    async def send_gift(self, user: TgUser, gift_id: int, text: str = "", hide_name: bool = False) -> dict:
        """Отправляет один подарок. Возвращает {'ok': True, 'ts': …}. GiftError.code == 'unknown' или 'network' —
        исход неизвестен (запрос мог дойти)."""
        if gift_id <= 0:
            raise GiftError("каталог подарков не загружен — отправка невозможна", "config")
        if self.dry_run:
            log.info("[DRY-RUN] подарок %s → @%s не отправлен", gift_id, user.username)
            return {"ok": True, "ts": time.time(), "dry": True}
        from telethon.tl import functions, types
        c = await self._client()
        peer = types.InputPeerUser(user_id=int(user.user_id), access_hash=int(user.access_hash))
        msg = types.TextWithEntities(text=text[:120], entities=[]) if text else None
        invoice = types.InputInvoiceStarGift(peer=peer, gift_id=int(gift_id), hide_name=hide_name or None,
                                             message=msg)
        async with self._lock:
            try:
                form = await c(functions.payments.GetPaymentFormRequest(invoice=invoice))
            except Exception as e:  # noqa: BLE001  — до оплаты: ничего не списано
                code = _rpc_code(e)
                raise GiftError(f"подарок не оформлен: {e}", "rejected" if code == "unknown" else code) from e
            try:
                await c(functions.payments.SendStarsFormRequest(form_id=form.form_id, invoice=invoice))
            except (OSError, ConnectionError, asyncio.TimeoutError) as e:
                raise GiftError(f"обрыв связи во время оплаты подарка: {e}", "unknown") from e
            except Exception as e:  # noqa: BLE001
                code = _rpc_code(e)
                # RPC-ошибка — Telegram ответил отказом, звёзды не списаны
                raise GiftError(f"подарок не отправлен: {e}", "rejected" if code == "unknown" else code) from e
        return {"ok": True, "ts": time.time()}

    async def find_sent(self, user_id: int, stars: int, since: float) -> bool:
        """Есть ли в истории звёзд аккаунта исходящая оплата подарка этому пользователю после since."""
        from telethon.tl import functions, types
        c = await self._client()
        res = await c(functions.payments.GetStarsTransactionsRequest(peer=types.InputPeerSelf(), offset="",
                                                                     limit=50, outbound=True))
        for t in getattr(res, "history", None) or []:
            ts = getattr(t, "date", None)
            ts = ts.timestamp() if ts else 0
            amount = abs(int(getattr(getattr(t, "amount", None), "amount", getattr(t, "stars", 0)) or 0))
            peer = getattr(getattr(t, "peer", None), "peer", None)
            if ts >= since - 60 and amount == int(stars) and getattr(peer, "user_id", None) == int(user_id):
                return True
        return False


class FakeTelegramGifts:
    """Эмулятор для демо и тестов."""

    def __init__(self, balance: int = 3000):
        self.balance = balance
        self.sent: list[tuple[str, int, int]] = []
        self.fail_next: str | None = None
        self.dry_run = False
        self.me = TgUser(1, 1, "my_shop_account", "Продавец")
        from starsbot.gifts.planner import Gift as G
        self._catalog = [G(1001, 15, 13, "Мишка", emoji="🧸"), G(1002, 25, 21, "Роза", emoji="🌹"),
                         G(1003, 50, 43, "Торт", emoji="🎂"), G(1004, 100, 85, "Кубок", emoji="🏆"),
                         G(1999, 2500, 2000, "Лимитка", limited=True)]
        self.has_session = True

    async def whoami(self):
        return self.me

    async def stars_balance(self) -> int:
        return self.balance

    async def catalog(self, max_age: float = 3600):
        return self._catalog

    async def resolve(self, username: str) -> TgUser:
        u = username.lstrip("@").lower()
        if u.startswith("nobody") or u == "unknownuser":
            raise GiftError(f"@{username}: не найден", "not_found")
        if u.endswith("channel"):
            raise GiftError(f"@{username} — канал", "cannot_receive")
        return TgUser(abs(hash(u)) % 10 ** 9, 42, username.lstrip("@"), username.title())

    async def send_gift(self, user: TgUser, gift_id: int, text: str = "", hide_name: bool = False) -> dict:
        if self.fail_next:
            code, self.fail_next = self.fail_next, None
            raise GiftError("эмуляция сбоя", code)
        g = next(x for x in self._catalog if x.id == gift_id)
        if self.balance < g.stars:
            raise GiftError("BALANCE_TOO_LOW", "no_stars")
        self.balance -= g.stars
        self.sent.append((user.username, gift_id, g.stars))
        return {"ok": True, "ts": time.time()}

    async def find_sent(self, user_id: int, stars: int, since: float) -> bool:
        return False

    async def send_code(self, phone: str) -> str:
        return "демо: код 12345"

    async def sign_in(self, phone: str, code: str = "", password: str = ""):
        return self.me

    async def logout_local(self) -> None:
        return None

    async def close(self) -> None:
        return None
