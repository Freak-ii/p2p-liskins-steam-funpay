"""Режим «Только поставщик»: FunPay выключен (funpay.enabled: false).

Бот снабжает Telegram-магазин (ShopBot), GGSEL и другие площадки, но на FunPay не заходит: не логинится,
не читает чаты, не двигает цены и не поднимает лоты. Единственное исключение — разовое выключение своих лотов
(park_funpay в движке), чтобы на FunPay не остались видимые лоты, которые никто не обслуживает.

FunPayOff стоит на месте клиента: любой случайный вызов FunPay заканчивается FunPayError, а не запросом в сеть.
"""
from __future__ import annotations

from typing import Callable

from starsbot.funpay.client import FunPayError

OFF_TEXT = "FunPay выключен: режим «Только поставщик»"


class FunPayOff:
    enabled = False

    def __init__(self, factory: Callable[[], object] | None = None):
        self.user_id = 0
        self.username = ""
        self.dry_run = False
        self._factory = factory       # настоящий клиент — только для разового выключения лотов

    async def login(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def park_client(self):
        """Настоящий клиент FunPay для разового выключения лотов (None — golden_key нет)."""
        return self._factory() if self._factory else None

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        async def _off(*_a, **_k):
            raise FunPayError(OFF_TEXT)
        return _off
