import asyncio
import time

from starsbot.coord import Coordinator
from starsbot.core import messages as M
from starsbot.core.stars import (looks_like_username_attempt, normalize_username, stars_from_text,
                                 username_from_params, username_from_text)


def test_stars_from_text():
    assert stars_from_text("50 звёзд, По username") == 50
    assert stars_from_text("Telegram Stars 250") == 250
    assert stars_from_text("1 000 звезд") == 1000
    assert stars_from_text("Mann Co. Supply Crate Key") is None


def test_usernames():
    assert normalize_username("@Alice_TG") == "Alice_TG"
    assert normalize_username("https://t.me/alice_tg") == "alice_tg"
    assert normalize_username("ab") is None                 # слишком короткий
    assert normalize_username("1abc") is None
    assert normalize_username("a__b_c") is None
    assert normalize_username("привет") is None
    assert username_from_text("мой ник @bob_real спасибо") == "bob_real"
    assert username_from_text("вот t.me/carol_star") == "carol_star"
    assert username_from_text("carol_star") == "carol_star"
    assert username_from_text("Боб Иванов") is None
    assert username_from_text("здравствуйте") is None
    assert username_from_params({"Ваш username в Telegram": "@dave_d"}) == "dave_d"
    assert username_from_params({"Количество": "2"}) is None
    assert looks_like_username_attempt("@боб")
    assert not looks_like_username_attempt("12345")


def test_templates_follow_funpay_rules():
    for lang, items in M.TEMPLATES.items():
        for key, text in items.items():
            assert M.validate_template(text) is None, (lang, key)
            assert not M.contains_forbidden_link(text), (lang, key)
    # просьба подтвердить заказ — только в шаблонах после выдачи (п. 2.1.2)
    for key in ("greeting_need_username", "greeting_with_username", "username_ok", "reminder", "delay"):
        assert "подтверд" not in M.TEMPLATES["ru"][key].lower(), key
    assert M.validate_template("пишите на https://evil.example") is not None


def test_coordinator_claims_and_gate(tmp_path):
    path = tmp_path / "coord.sqlite3"
    stars, mann = Coordinator(path, "starsbot", min_interval=0.2), Coordinator(path, "mannbot", min_interval=0.2)
    assert stars.claim_order("A1") and not mann.claim_order("A1") and mann.claim_order("B2")
    assert mann.order_owner("A1") == "starsbot"
    stars.claim_chat("100", "A1")
    assert mann.chat_is_foreign("100") and not stars.chat_is_foreign("100")
    stars.release_chat("100", "A1")
    assert mann.chat_owner("100") is None
    off = Coordinator(None, "x")
    assert not off.enabled and off.claim_order("Z") and not off.chat_is_foreign("1")

    async def burst():
        t = time.monotonic()
        for c in (stars, mann, stars, mann):
            await c.wait_turn("funpay")
        return time.monotonic() - t
    assert asyncio.run(burst()) >= 0.55          # 4 запроса двух ботов → не чаще 0.2 с суммарно
