"""Парсеры HTML-страниц FunPay (веб-режим, без официального API).

Разметка взята с живых страниц funpay.com (body[data-app-data], .chat[data-id],
.chat-msg-item, a.tc-item ...). Если FunPay поменяет вёрстку — правки нужны только здесь.
"""
from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Tag

from starsbot.core.models import ChatMessage, CompetitorLot, FunPayOrder

USER_ID_RE = re.compile(r"/users/(\d+)/?")
ORDER_ID_RE = re.compile(r"#([A-Z0-9]{8})\b")
NUM_RE = re.compile(r"[-+]?\d[\d\s ]*(?:[.,]\d+)?")

# Системные сообщения FunPay в чате заказа
SYS_PAID_RE = re.compile(r"оплатил заказ\s*#([A-Z0-9]{8})|has paid for order\s*#([A-Z0-9]{8})", re.I)
SYS_CONFIRMED_RE = re.compile(r"подтвердил успешное выполнение заказа\s*#([A-Z0-9]{8})|"
                              r"confirmed that order\s*#([A-Z0-9]{8})", re.I)
SYS_REFUND_RE = re.compile(r"вернул деньги покупателю.*?#([A-Z0-9]{8})|refunded.*?#([A-Z0-9]{8})", re.I | re.S)
SYS_REVIEW_RE = re.compile(r"(?:написал|изменил) отзыв к заказу\s*#([A-Z0-9]{8})|"
                           r"(?:left|edited) a review.*?#([A-Z0-9]{8})", re.I | re.S)
SYS_REOPEN_RE = re.compile(r"(?:открыт повторно|reopened).*?#([A-Z0-9]{8})", re.I | re.S)


def soup(markup: str) -> BeautifulSoup:
    return BeautifulSoup(markup, "lxml")


def parse_number(text: str) -> float | None:
    m = NUM_RE.search(text or "")
    if not m:
        return None
    raw = m.group(0).replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def text_of(node: Tag | None) -> str:
    if node is None:
        return ""
    for br in node.find_all("br"):
        br.replace_with("\n")
    return re.sub(r"[ \t ]+", " ", node.get_text()).strip()


# ---------------------------------------------------------------------------- app data
def parse_app_data(page: str) -> dict:
    """body[data-app-data] → {'locale','csrf-token','userId', ...}."""
    m = re.search(r'<body[^>]*data-app-data="([^"]+)"', page)
    if not m:
        return {}
    try:
        return json.loads(html_lib.unescape(m.group(1)))
    except json.JSONDecodeError:
        return {}


def parse_username(page: str) -> str | None:
    s = soup(page)
    node = s.select_one(".user-link-name")
    return node.get_text(strip=True) if node else None


def parse_live_counters(page: str) -> dict:
    s = soup(page)
    node = s.select_one("#live-counters")
    if not node:
        return {}
    return {"orders": node.get("data-orders"), "chat": node.get("data-chat")}


# ---------------------------------------------------------------------------- chat
def classify_system(text: str) -> tuple[str, str] | None:
    for kind, rx in (("paid", SYS_PAID_RE), ("confirmed", SYS_CONFIRMED_RE), ("refund", SYS_REFUND_RE),
                     ("review", SYS_REVIEW_RE), ("reopened", SYS_REOPEN_RE)):
        m = rx.search(text)
        if m:
            return kind, next(g for g in m.groups() if g)
    return None


def parse_chat_messages(markup: str, chat_id: str, prev_author: tuple[int, str] | None = None
                        ) -> list[ChatMessage]:
    """Разбор .chat-msg-item. У «компактных» сообщений нет автора — он берётся из предыдущего."""
    s = soup(markup)
    out: list[ChatMessage] = []
    author_id, author_name = prev_author or (0, "")
    for item in s.select("div.chat-msg-item"):
        mid = re.sub(r"\D", "", item.get("id", "") or "")
        if not mid:
            continue
        link = item.select_one("a.chat-msg-author-link")
        if link is not None:
            m = USER_ID_RE.search(link.get("href", ""))
            author_id = int(m.group(1)) if m else 0
            author_name = link.get_text(strip=True)
        elif item.select_one(".media-user-name") is not None:
            name_node = item.select_one(".media-user-name")
            author_name = name_node.get_text(" ", strip=True).split(" ")[0]
            author_id = 0
        body = item.select_one(".chat-msg-text") or item.select_one(".chat-msg-body")
        text = text_of(body)
        # картинки FunPay присылает ссылкой в .chat-img-link
        img = item.select_one("a.chat-img-link")
        if img and not text:
            text = f"[изображение] {img.get('href', '')}"
        is_system = (author_id == 0 or item.select_one(".chat-msg-author-label") is not None
                     or item.select_one(".alert") is not None)
        out.append(ChatMessage(message_id=int(mid), chat_id=str(chat_id), author_id=author_id,
                               author_name=author_name, text=text, is_system=is_system))
    return out


def parse_chat_header(page: str) -> dict:
    """div.chat[data-id][data-name][data-tag][data-user] на странице чата/заказа."""
    s = soup(page)
    node = s.select_one("div.chat[data-id]")
    if not node:
        return {}
    return {"id": node.get("data-id"), "name": node.get("data-name"), "tag": node.get("data-tag"),
            "user": node.get("data-user")}


@dataclass
class ChatBookmark:
    chat_id: str
    username: str
    last_message_id: int
    last_text: str
    unread: bool


def parse_chat_bookmarks(markup: str) -> list[ChatBookmark]:
    """Список диалогов из runner (chat_bookmarks) или со страницы /chat/."""
    s = soup(markup)
    out = []
    for a in s.select("a.contact-item"):
        cid = a.get("data-id")
        if not cid:
            m = re.search(r"node=(\d+)", a.get("href", ""))
            cid = m.group(1) if m else None
        if not cid:
            continue
        out.append(ChatBookmark(
            chat_id=str(cid),
            username=text_of(a.select_one(".media-user-name")),
            last_message_id=int(a.get("data-node-msg") or 0),
            last_text=text_of(a.select_one(".contact-item-message")),
            unread="unread" in (a.get("class") or []),
        ))
    return out


# ---------------------------------------------------------------------------- orders
def _order_status_from_classes(classes: list[str], status_text: str) -> str:
    t = status_text.lower()
    if "warning" in classes or "возврат" in t or "refund" in t:
        return "refunded"
    if "info" in classes or "оплачен" in t or "paid" in t:
        return "paid"
    return "closed"


def parse_orders_list(page: str) -> list[FunPayOrder]:
    """Страница https://funpay.com/orders/trade (список продаж)."""
    s = soup(page)
    out = []
    for row in s.select("a.tc-item"):
        oid_text = text_of(row.select_one(".tc-order"))
        m = ORDER_ID_RE.search(oid_text) or re.search(r"/orders/([A-Z0-9]{8})/", row.get("href", ""))
        if not m:
            continue
        desc_node = row.select_one(".order-desc div") or row.select_one(".order-desc")
        desc = text_of(desc_node)
        sub_node = row.select_one(".order-desc .text-muted")
        price_node = row.select_one(".tc-price")
        price = parse_number(price_node.get("data-s") or text_of(price_node)) if price_node else 0.0
        currency = "₽"
        if price_node is not None:
            unit = price_node.select_one(".unit")
            if unit:
                currency = unit.get_text(strip=True)
        buyer_node = row.select_one(".media-user-name span") or row.select_one(".media-user-name")
        buyer = text_of(buyer_node)
        buyer_id = 0
        for cand in (row.select_one(".avatar-photo"), buyer_node):
            if cand is not None:
                um = USER_ID_RE.search(cand.get("data-href", "") or "")
                if um:
                    buyer_id = int(um.group(1))
                    break
        status_text = text_of(row.select_one(".tc-status"))
        out.append(FunPayOrder(
            order_id=m.group(1), buyer_username=buyer, buyer_id=buyer_id, description=desc,
            price=price or 0.0, quantity=_quantity_from_text(desc),
            status=_order_status_from_classes(row.get("class") or [], status_text),
            currency=currency, subcategory=text_of(sub_node),
        ))
    return out


_QTY_RES = [
    re.compile(r"(\d+)\s*(?:шт|pcs|ед)\.?", re.I),
    re.compile(r"[x×х]\s*(\d+)\b", re.I),
    re.compile(r"\b(\d+)\s*[x×х]\b", re.I),
]


def _quantity_from_text(text: str) -> int:
    for rx in _QTY_RES:
        m = rx.search(text or "")
        if m:
            return max(1, int(m.group(1)))
    return 1


@dataclass
class OrderPage:
    order_id: str
    status: str
    params: dict[str, str] = field(default_factory=dict)
    quantity: int = 1
    chat_id: str | None = None
    chat_name: str | None = None
    buyer_id: int | None = None
    buyer_username: str | None = None
    review_stars: int | None = None
    review_text: str = ""
    review_answered: bool = False        # продавец уже ответил под отзывом
    total: float | None = None


def parse_order_page(page: str, order_id: str) -> OrderPage:
    """Страница https://funpay.com/orders/<ID>/ — параметры заказа, чат, отзыв."""
    s = soup(page)
    params: dict[str, str] = {}
    for item in s.select(".param-item"):
        h = item.select_one("h5")
        if not h:
            continue
        key = h.get_text(strip=True)
        h.extract()
        params[key] = text_of(item)
    status_text = ""
    head = s.select_one(".page-header")
    if head:
        status_text = text_of(head)
    st_node = s.select_one(".order-status") or s.select_one("span.text-success, span.text-warning, span.text-primary")
    if st_node:
        status_text += " " + text_of(st_node)
    tl = status_text.lower()
    status = "refunded" if ("возврат" in tl or "refund" in tl) else \
        "closed" if ("закрыт" in tl or "closed" in tl) else "paid"

    qty = 1
    for k, v in params.items():
        if k.lower() in ("количество", "amount", "кол-во", "quantity"):
            n = parse_number(v)
            qty = int(n) if n else 1
            break
    else:
        for k in ("Краткое описание", "Short description"):
            if k in params:
                qty = _quantity_from_text(params[k])
    total = None
    for k, v in params.items():
        if k.lower() in ("сумма", "total", "цена"):
            total = parse_number(v)
    chat = s.select_one("div.chat[data-id]")
    buyer_id = buyer_name = None
    # блок «Покупатель» на странице заказа (в правой колонке)
    for card in s.select(".param-item, .media-user-info, .media-user"):
        link = card.select_one("a[href*='/users/']")
        if link and ("Покупатель" in card.get_text() or "Buyer" in card.get_text()):
            m = USER_ID_RE.search(link.get("href", ""))
            buyer_id = int(m.group(1)) if m else None
            buyer_name = link.get_text(strip=True)
            break
    stars = None
    review_text = ""
    rv = s.select_one(".review-item-rating")
    if rv:
        rating = rv.select_one("[class*='rating']") or rv
        cls = " ".join(rating.get("class") or [])
        m = re.search(r"rating(\d)", cls)
        stars = int(m.group(1)) if m else None
        review_text = text_of(s.select_one(".review-item-text"))
    answered = s.select_one(".review-item-answer, .review-compiled-reply") is not None
    return OrderPage(order_id=order_id, status=status, params=params, quantity=qty,
                     chat_id=chat.get("data-id") if chat else None,
                     chat_name=chat.get("data-name") if chat else None,
                     buyer_id=buyer_id, buyer_username=buyer_name,
                     review_stars=stars, review_text=review_text, review_answered=answered, total=total)


# ---------------------------------------------------------------------------- lots (RTBA)
def parse_lots_page(page: str, filter_regex: str | None = None) -> list[CompetitorLot]:
    """Публичная таблица предложений https://funpay.com/lots/<node>/ ."""
    s = soup(page)
    rx = re.compile(filter_regex) if filter_regex else None
    out = []
    for row in s.select("a.tc-item"):
        m = re.search(r"[?&]id=(\d+)", row.get("href", ""))
        if not m:
            continue
        desc = text_of(row.select_one(".tc-desc-text") or row.select_one(".tc-desc"))
        if rx and not rx.search(desc):
            continue
        price_node = row.select_one(".tc-price")
        price = None
        if price_node is not None:
            price = parse_number(price_node.get("data-s") or "") or parse_number(text_of(price_node))
        if price is None:
            continue
        amount_node = row.select_one(".tc-amount")
        amount = 0
        if amount_node is not None:
            n = parse_number(amount_node.get("data-s") or text_of(amount_node))
            amount = int(n) if n else 0
        seller = text_of(row.select_one(".media-user-name"))
        online = row.get("data-online") == "1" or row.select_one(".media-user.online") is not None
        reviews = row.select_one(".rating-mini-count")
        stars = row.select_one(".rating-stars")
        rating = None
        if stars is not None:
            m2 = re.search(r"rating-(\d)", " ".join(stars.get("class") or []))
            rating = float(m2.group(1)) if m2 else None
        fields = {k[7:]: str(v).lower() for k, v in row.attrs.items() if k.startswith("data-f-")}
        out.append(CompetitorLot(offer_id=m.group(1), seller=seller, price=float(price), amount=amount,
                                 online=online, rating=rating,
                                 reviews=int(parse_number(text_of(reviews)) or 0) if reviews else 0,
                                 description=desc, fields=fields))
    return out


def parse_offer_edit_form(page: str) -> dict[str, str]:
    """Форма редактирования лота (lots/offerEdit) → все поля для lots/offerSave."""
    s = soup(page)
    form = s.select_one("form.form-offer-editor") or s.select_one("form")
    if form is None:
        return {}
    fields: dict[str, str] = {}
    for inp in form.select("input[name]"):
        typ = (inp.get("type") or "text").lower()
        if typ in ("checkbox", "radio"):
            if inp.has_attr("checked"):
                fields[inp["name"]] = inp.get("value", "on")
            continue
        fields[inp["name"]] = inp.get("value", "")
    for ta in form.select("textarea[name]"):
        fields[ta["name"]] = ta.get_text()
    for sel in form.select("select[name]"):
        opt = sel.select_one("option[selected]") or sel.select_one("option")
        fields[sel["name"]] = opt.get("value", "") if opt else ""
    return fields


def parse_offer_selects(page: str) -> dict[str, list[tuple[str, str]]]:
    """Выпадающие списки формы лота: {name: [(value, текст)]} — чтобы выставить «количество» и «способ» по тексту."""
    s = soup(page)
    form = s.select_one("form.form-offer-editor") or s.select_one("form")
    out: dict[str, list[tuple[str, str]]] = {}
    if form is None:
        return out
    for sel in form.select("select[name]"):
        out[sel["name"]] = [(o.get("value", ""), text_of(o)) for o in sel.select("option")]
    return out


@dataclass
class OwnLot:
    offer_id: str
    description: str
    price: float | None = None
    amount: int | None = None
    active: bool = True


def parse_own_lots(page: str) -> list[OwnLot]:
    """Свои лоты раздела (funpay.com/lots/<node>/trade): ссылки на offerEdit?offer=ID или offer?id=ID."""
    s = soup(page)
    out: list[OwnLot] = []
    seen: set[str] = set()
    for row in s.select("a.tc-item, div.tc-item"):
        href = row.get("href", "") or row.get("data-href", "")
        m = re.search(r"(?:offer=|[?&]id=)(\d+)", href)
        if not m:
            a = row.select_one("a[href*='offer']")
            m = re.search(r"(?:offer=|[?&]id=)(\d+)", a.get("href", "")) if a is not None else None
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        desc = text_of(row.select_one(".tc-desc-text") or row.select_one(".tc-desc"))
        price_node = row.select_one(".tc-price")
        price = parse_number(price_node.get("data-s") or text_of(price_node)) if price_node is not None else None
        amount_node = row.select_one(".tc-amount")
        amount = parse_number(amount_node.get("data-s") or text_of(amount_node)) if amount_node is not None else None
        classes = " ".join(row.get("class") or [])
        out.append(OwnLot(m.group(1), desc, price, int(amount) if amount is not None else None,
                          "warning" not in classes and "inactive" not in classes))
    return out


@dataclass
class BalanceInfo:
    total_rub: float | None = None
    available_rub: float | None = None
    form_action: str = ""
    fields: dict = field(default_factory=dict)
    methods: list = field(default_factory=list)        # [(value, текст)] — способы вывода
    method_field: str = ""
    amount_field: str = ""
    wallet_field: str = ""


def parse_balance_page(page: str) -> BalanceInfo:
    """Страница funpay.com/account/balance: баланс и форма вывода (поля ищутся по смыслу имени)."""
    s = soup(page)
    info = BalanceInfo()
    vals = [parse_number(text_of(n)) for n in s.select(".balances-value, .balance-value, .badge-balance")]
    vals = [v for v in vals if v is not None]
    if vals:
        info.total_rub = vals[0]
        info.available_rub = vals[1] if len(vals) > 1 else vals[0]
    form = None
    for f in s.select("form"):
        names = " ".join(i.get("name", "") for i in f.select("[name]"))
        if re.search(r"amount|sum", names, re.I) and re.search(r"wallet|account|requisite|ext|currency", names, re.I):
            form = f
            break
    if form is None:
        return info
    info.form_action = form.get("action") or "/withdraw/withdraw"
    for inp in form.select("input[name]"):
        typ = (inp.get("type") or "text").lower()
        if typ in ("checkbox", "radio") and not inp.has_attr("checked"):
            continue
        info.fields[inp["name"]] = inp.get("value", "")
        if not info.amount_field and re.search(r"amount|sum", inp["name"], re.I):
            info.amount_field = inp["name"]
        if not info.wallet_field and re.search(r"wallet|account|requisite|address", inp["name"], re.I):
            info.wallet_field = inp["name"]
    for sel in form.select("select[name]"):
        opts = [(o.get("value", ""), text_of(o)) for o in sel.select("option") if o.get("value")]
        if not info.method_field and re.search(r"ext|currency|method|system", sel["name"], re.I):
            info.method_field, info.methods = sel["name"], opts
        opt = sel.select_one("option[selected]") or sel.select_one("option")
        info.fields[sel["name"]] = opt.get("value", "") if opt else ""
    return info
