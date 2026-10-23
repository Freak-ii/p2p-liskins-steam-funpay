from conftest import FIX

from starsbot.funpay import parsers as P


def read(name):
    return (FIX / name).read_text(encoding="utf-8")


def test_app_data_and_header():
    page = read("chat.html")
    data = P.parse_app_data(page)
    assert data["csrf-token"] == "abc123token" and data["userId"] == 777
    assert P.parse_username(page) == "SellerNick"
    assert P.parse_chat_header(page) == {"id": "98765", "name": "users-777-555", "tag": "oez2ypsr", "user": "777"}
    assert P.parse_live_counters(page)["orders"] == "lxkgki6p"


def test_chat_messages_compact_and_system():
    msgs = P.parse_chat_messages(read("chat.html"), "98765")
    assert [m.message_id for m in msgs] == [100, 101, 102]
    assert msgs[0].author_id == 555 and msgs[0].author_name == "BuyerNick" and not msgs[0].is_system
    assert "token=AbCd_123" in msgs[0].text
    # компактное сообщение без шапки наследует автора
    assert msgs[1].author_id == 555 and msgs[1].text == "и ещё вопрос"
    assert msgs[2].is_system
    assert P.classify_system(msgs[2].text) == ("paid", "ABCD1234")


def test_system_classifier():
    assert P.classify_system("Покупатель X подтвердил успешное выполнение заказа #QWER1234 и отправил деньги") == \
        ("confirmed", "QWER1234")
    assert P.classify_system("Продавец Y вернул деньги покупателю X по заказу #QWER1234.")[0] == "refund"
    assert P.classify_system("Покупатель X написал отзыв к заказу #QWER1234.")[0] == "review"
    assert P.classify_system("просто текст") is None


def test_orders_list():
    orders = P.parse_orders_list(read("orders_trade.html"))
    assert [o.order_id for o in orders] == ["ABCD1234", "ZZZZ9999", "RRRR1111"]
    a, b, c = orders
    assert a.status == "paid" and a.price == 555.0 and a.buyer_id == 555 and a.buyer_username == "BuyerNick"
    assert a.quantity == 3 and a.currency == "₽"
    assert b.status == "closed" and b.buyer_id == 556
    assert c.status == "refunded" and c.quantity == 2


def test_lots_page_filter_and_numbers():
    lots = P.parse_lots_page(read("lots.html"), r"(?i)mann\s*co")
    assert [l.offer_id for l in lots] == ["11", "13"]
    assert lots[0].price == 189.5 and lots[0].amount == 40 and lots[0].online and lots[0].rating == 5.0
    assert lots[0].reviews == 1234
    assert lots[1].price == 192.3 and lots[1].amount == 1500


def test_offer_edit_form():
    f = P.parse_offer_edit_form(read("offer_edit.html"))
    assert f["offer_id"] == "42" and f["price"] == "190.00" and f["amount"] == "25"
    assert f["active"] == "on" and "deleted" not in f
    assert f["fields[type]"] == "key" and "лота" in f["fields[desc][ru]"]


def test_order_page():
    page = """<html><body><h1 class="page-header">Заказ #ABCD1234 <span class="text-primary">Оплачен</span></h1>
    <div class="param-item"><h5>Краткое описание</h5><div>Mann Co. Supply Crate Key</div></div>
    <div class="param-item"><h5>Количество</h5><div>5 шт.</div></div>
    <div class="param-item"><h5>Сумма</h5><div>925 ₽</div></div>
    <div class="param-item"><h5>Покупатель</h5><a href="https://funpay.com/users/555/">BuyerNick</a></div>
    <div class="chat" data-id="98765" data-name="users-555-777"></div></body></html>"""
    o = P.parse_order_page(page, "ABCD1234")
    assert o.quantity == 5 and o.total == 925 and o.chat_id == "98765" and o.status == "paid"
    assert o.buyer_id == 555 and o.buyer_username == "BuyerNick"
