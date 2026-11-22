import pytest

from mannbot.steam import guard
from mannbot.steam.tradelink import (TradeLinkError, find_trade_url, looks_like_wrong_link, parse_new_offer_page,
                                     parse_trade_url)

SECRET = "zvIayp3JPvtTgjpAHdNWVeiyKLE="


def test_auth_code_format_and_window():
    c1 = guard.generate_auth_code(SECRET, 1_700_000_000)
    assert len(c1) == 5 and set(c1) <= set("23456789BCDFGHJKMNPQRTVWXY")
    assert c1 == guard.generate_auth_code(SECRET, 1_700_000_000 + 1)       # то же 30-сек окно
    assert c1 != guard.generate_auth_code(SECRET, 1_700_000_000 + 600)


def test_confirmation_key_and_device():
    k = guard.generate_confirmation_key(SECRET, "conf", 1_700_000_000)
    assert len(k) == 28 and k.endswith("=")
    assert guard.generate_device_id("76561198000000001").startswith("android:")


def test_trade_url_parse():
    link = parse_trade_url("https://steamcommunity.com/tradeoffer/new/?partner=111222333&token=AbCd_123")
    assert link.steam_id64 == str(111222333 + 76561197960265728)
    assert link.canonical.endswith("partner=111222333&token=AbCd_123")


@pytest.mark.parametrize("url,code", [
    ("https://steamcommunity.com/tradeoffer/new/?partner=111222333", "no_token"),
    ("https://steamcommunity.com/tradeoffer/new/?partner=111222333&token=abc", "bad_token"),
    ("https://steamcommunity.com/profiles/765/", "bad_format"),
    ("https://steamcommunity.ru.evil.com/tradeoffer/new/?partner=1&token=AbCdEfGh", "bad_format"),
])
def test_trade_url_errors(url, code):
    with pytest.raises(TradeLinkError) as e:
        parse_trade_url(url)
    assert e.value.code == code


def test_find_trade_url_variants():
    t = "держи steamcommunity.com/tradeoffer/new/?partner=123&amp;token=AbCdEfGh."
    assert find_trade_url(t) == "https://steamcommunity.com/tradeoffer/new/?partner=123&token=AbCdEfGh"
    assert find_trade_url("нет ссылки") is None
    assert looks_like_wrong_link("https://steamcommunity.com/id/nick") == "profile_link"
    assert looks_like_wrong_link("76561198011111111") == "steamid_only"
    assert looks_like_wrong_link("https://disk.yandex.ru/x") == "foreign_link"
    assert looks_like_wrong_link("привет") is None


def test_new_offer_page_ok_and_errors():
    ok = parse_new_offer_page("<script>var g_ulTradePartnerSteamID = '76561198071487061';"
                              "var g_strTradePartnerPersonaName = \"Buyer\\u0020X\"; var g_daysMyEscrow = 0;"
                              "var g_daysTheirEscrow = 15;</script>")
    assert ok.ok and ok.their_escrow_days == 15 and ok.partner_steam_id == "76561198071487061"
    assert ok.partner_name == "Buyer X"
    bad = parse_new_offer_page('<div id="error_msg">This Trade URL is no longer valid for sending a trade offer'
                               ' to X.</div>')
    assert not bad.ok and bad.error_code == "invalid_token"
    ban = parse_new_offer_page('<div id="error_msg">You cannot trade with X because they have a trade ban.</div>')
    assert ban.error_code == "trade_banned"
