"""Общие модели данных и статусы заказа."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class OrderStatus(str, Enum):
    NEW = "new"                         # заказ обнаружен, инструкция ещё не отправлена
    AWAITING_LINK = "awaiting_link"     # ждём ссылку на обмен от покупателя
    PENDING_APPROVAL = "pending_approval"  # риск-система: ждём решения админа
    READY_TO_SEND = "ready_to_send"     # данные проверены, ставим в очередь отправки
    OFFER_SENT = "offer_sent"           # трейд отправлен и подтверждён, ждём принятия
    ESCROW = "escrow"                   # покупатель принял, но Steam держит обмен (trade hold)
    DELIVERED = "delivered"             # ключи получены покупателем, ждём подтверждения на FunPay
    COMPLETED = "completed"             # покупатель подтвердил заказ на FunPay
    REFUNDED = "refunded"               # возврат средств
    MANUAL = "manual"                   # нужна ручная обработка админом
    BLOCKED = "blocked"                 # заблокирован риск-системой

    @property
    def is_final(self) -> bool:
        return self in (OrderStatus.COMPLETED, OrderStatus.REFUNDED, OrderStatus.BLOCKED)

    @property
    def is_active(self) -> bool:
        return not self.is_final


# Статусы торгового предложения Steam (ETradeOfferState)
class TradeOfferState(int, Enum):
    INVALID = 1
    ACTIVE = 2
    ACCEPTED = 3
    COUNTERED = 4
    EXPIRED = 5
    CANCELED = 6
    DECLINED = 7
    INVALID_ITEMS = 8
    NEEDS_CONFIRMATION = 9
    CANCELED_BY_SECOND_FACTOR = 10
    IN_ESCROW = 11

    @classmethod
    def parse(cls, value: int) -> "TradeOfferState":
        try:
            return cls(int(value))
        except ValueError:
            return cls.INVALID


@dataclass
class FunPayOrder:
    order_id: str
    buyer_username: str
    buyer_id: int
    description: str
    price: float
    quantity: int = 1
    status: str = "paid"          # paid | closed | refunded
    chat_id: str | None = None
    currency: str = "₽"
    subcategory: str = ""


@dataclass
class ChatMessage:
    message_id: int
    chat_id: str
    author_id: int
    author_name: str
    text: str
    is_system: bool = False       # системные сообщения FunPay (оплата, подтверждение...)


@dataclass
class CompetitorLot:
    offer_id: str
    seller: str
    price: float
    amount: int
    online: bool = True
    rating: float | None = None
    reviews: int = 0
    description: str = ""


@dataclass
class TradeLinkCheck:
    ok: bool
    steam_id64: str | None = None
    partner_name: str | None = None
    their_escrow_days: int = 0
    reason_code: str = ""           # bad_format | invalid_token | trade_banned | cannot_trade | self | blacklisted | network
    reason_text: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class SentOffer:
    offer_id: str
    needs_confirmation: bool
    asset_ids: list[str]
