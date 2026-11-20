"""Один аккаунт Steam с LisBot: перед отправкой ключей записываем обмен в реестр центра.

LisBot на том же аккаунте считает взломом любой исходящий обмен, который создал не он. Центр
передаёт MannBot адрес и токен реестра (CENTER_STEAM_URL, CENTER_STEAM_TOKEN); MannBot сначала
записывает, кому и какие ключи уйдут, и только потом отправляет обмен. Если оба бота на одном
аккаунте (CENTER_STEAM_SHARED=1), а центр не ответил — обмен не отправляется: иначе LisBot
отменит его и остановится. Без центра (MannBot запущен отдельно) реестр не используется.
"""
from __future__ import annotations

import logging
import os

import aiohttp

log = logging.getLogger("mannbot.steam.shared")
TIMEOUT = aiohttp.ClientTimeout(total=8)


class ShareUnavailable(Exception):
    """Центр не записал обмен, а отправлять без записи нельзя."""


class CenterShare:
    def __init__(self, url: str = "", token: str = "", shared: bool = False):
        self.url = url.rstrip("/")
        self.token = token
        self.shared = bool(shared and self.url and self.token)

    @classmethod
    def from_env(cls) -> "CenterShare":
        return cls(os.environ.get("CENTER_STEAM_URL", ""), os.environ.get("CENTER_STEAM_TOKEN", ""),
                   os.environ.get("CENTER_STEAM_SHARED", "") == "1")

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.token)

    async def _post(self, path: str, body: dict) -> dict:
        headers = {"Authorization": f"Bearer {self.token}", "X-Bot": "mannbot"}
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.post(f"{self.url}/{path}", json=body, headers=headers) as r:
                data = await r.json(content_type=None)
                if r.status != 200 or not data.get("ok"):
                    raise ShareUnavailable(data.get("error") or f"центр ответил {r.status}")
                return data

    async def intent(self, order_id: str, partner: str, appid: int, asset_ids: list[str]) -> int | None:
        """Записать будущий обмен. None — реестр не нужен или центр недоступен в режиме двух аккаунтов."""
        if not self.enabled:
            return None
        try:
            data = await self._post("intent", {"ref": order_id, "partner": partner, "appid": int(appid),
                                               "assets": list(asset_ids)})
            return int(data["id"])
        except (aiohttp.ClientError, TimeoutError, ShareUnavailable, ValueError, KeyError) as e:
            if self.shared:
                raise ShareUnavailable(f"центр не записал обмен ({e}): без записи LisBot на этом же аккаунте "
                                       "отменит его как чужой") from e
            log.warning("реестр обменов центра недоступен: %s", e)
            return None

    async def bind(self, intent_id: int | None, offer_id: str) -> None:
        """Сообщить номер созданного обмена. Не обязательно: центр узнает обмен по получателю и ключам."""
        if not intent_id or not self.enabled or not str(offer_id).isdigit():
            return
        try:
            await self._post("bind", {"id": intent_id, "offer_id": str(offer_id)})
        except (aiohttp.ClientError, TimeoutError, ShareUnavailable, ValueError) as e:
            log.warning("реестр обменов: номер обмена %s не записан: %s", offer_id, e)
