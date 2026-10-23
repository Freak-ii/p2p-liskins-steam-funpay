"""Общие модели данных и статусы заказа."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class OrderStatus(str, Enum):
    NEW = "new"                             # заказ обнаружен, ещё не обработан
    AWAITING_USERNAME = "awaiting_username"  # ждём от покупателя корректный @username
    PENDING_APPROVAL = "pending_approval"   # риск-система или дорогой курс: ждём решения продавца
    READY = "ready"                         # username проверен на Fragment, заказ в очереди покупки
    PURCHASING = "purchasing"               # транзакция подписана и отправляется в сеть TON
    SENT = "sent"                           # транзакция в сети, ждём подтверждения блокчейна
    DELIVERED = "delivered"                 # звёзды оплачены на Fragment, ждём подтверждения на FunPay
    COMPLETED = "completed"                 # покупатель подтвердил заказ на FunPay
    REFUNDED = "refunded"                   # возврат средств
    MANUAL = "manual"                       # нужна ручная обработка продавцом
    BLOCKED = "blocked"                     # заблокирован риск-системой

    @property
    def is_final(self) -> bool:
        return self in (OrderStatus.COMPLETED, OrderStatus.REFUNDED, OrderStatus.BLOCKED)

    @property
    def is_active(self) -> bool:
        return not self.is_final

    @property
    def money_in_flight(self) -> bool:
        """Транзакция уже могла уйти в сеть: повторять покупку нельзя без сверки с блокчейном."""
        return self in (OrderStatus.PURCHASING, OrderStatus.SENT)


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
    fields: dict = field(default_factory=dict)   # data-f-* атрибуты строки (quantity, method, ...)


@dataclass
class Recipient:
    """Получатель звёзд по данным Fragment."""
    username: str
    recipient: str                # внутренний идентификатор Fragment
    name: str = ""


@dataclass
class TonMessage:
    """Одно сообщение транзакции (формат TON Connect: address / amount в нанотонах / payload BOC base64)."""
    address: str
    amount: int
    payload: str = ""
    state_init: str = ""


@dataclass
class FragmentTx:
    req_id: str
    valid_until: int
    messages: list[TonMessage]
    amount_ton: float
    comment: str = ""             # расшифрованный комментарий платежа (Fragment кладёт туда Ref#…)
    confirm_method: str = ""      # метод, которым сайт Fragment подтверждает отправку транзакции
    confirm_params: dict = field(default_factory=dict)


@dataclass
class SentTx:
    seqno: int
    valid_until: int
    msg_hash: str
    boc: str
