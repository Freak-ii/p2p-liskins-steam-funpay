"""Разбор и валидация ссылки на обмен Steam, разбор страницы /tradeoffer/new/."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

STEAM64_BASE = 76561197960265728

TRADE_URL_RE = re.compile(
    r"https?://(?:www\.)?steamcommunity\.com/tradeoffer/new/?\?[^\s<>\"']*partner=\d+[^\s<>\"']*",
    re.IGNORECASE,
)
# Покупатели часто присылают ссылку без https:// или с переносами
LOOSE_TRADE_URL_RE = re.compile(
    r"steamcommunity\.com/tradeoffer/new/?\?[^\s<>\"']+", re.IGNORECASE)
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8}$")


@dataclass
class TradeLink:
    url: str
    partner: int          # account id (32 бит)
    token: str

    @property
    def steam_id64(self) -> str:
        return str(self.partner + STEAM64_BASE)

    @property
    def canonical(self) -> str:
        return f"https://steamcommunity.com/tradeoffer/new/?partner={self.partner}&token={self.token}"


class TradeLinkError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def find_trade_url(text: str) -> str | None:
    """Ищет в сообщении что-то похожее на ссылку на обмен."""
    text = html.unescape(text or "").replace("&amp;", "&")
    m = TRADE_URL_RE.search(text) or LOOSE_TRADE_URL_RE.search(text)
    if not m:
        return None
    url = m.group(0).rstrip(".,;)!»")
    if not url.lower().startswith("http"):
        url = "https://" + url
    return url


def looks_like_wrong_link(text: str) -> str | None:
    """Распознаёт частые ошибки покупателей, чтобы дать точную подсказку."""
    t = (text or "").lower()
    if "steamcommunity.com/profiles/" in t or "steamcommunity.com/id/" in t:
        return "profile_link"
    if "tradeoffer/new" in t and "token=" not in t:
        return "no_token"
    if re.search(r"\b7656119\d{10}\b", t):
        return "steamid_only"
    if "steamcommunity" in t or "steampowered" in t or "s.team" in t:
        return "other_steam_link"
    if "http" in t:
        return "foreign_link"
    return None


def parse_trade_url(url: str) -> TradeLink:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if host not in ("steamcommunity.com", "www.steamcommunity.com"):
        raise TradeLinkError("bad_format", "ссылка ведёт не на steamcommunity.com")
    if not parsed.path.rstrip("/").endswith("/tradeoffer/new"):
        raise TradeLinkError("bad_format", "это не ссылка на обмен (нужен адрес /tradeoffer/new/)")
    qs = parse_qs(parsed.query)
    partner_raw = (qs.get("partner") or [""])[0]
    token = (qs.get("token") or [""])[0]
    if not partner_raw.isdigit():
        raise TradeLinkError("bad_format", "в ссылке нет корректного параметра partner")
    partner = int(partner_raw)
    if partner <= 0 or partner >= 2 ** 32:
        raise TradeLinkError("bad_format", "некорректный partner в ссылке")
    if not token:
        raise TradeLinkError("no_token", "в ссылке нет параметра token — скопируйте ссылку целиком")
    if not TOKEN_RE.match(token):
        raise TradeLinkError("bad_token", "token в ссылке повреждён (должно быть 8 символов)")
    return TradeLink(url=url, partner=partner, token=token)


# ---------------------------------------------------------------------- страница нового обмена
@dataclass
class NewOfferPage:
    ok: bool
    error_code: str = ""
    error_text: str = ""
    partner_name: str | None = None
    partner_steam_id: str | None = None
    my_escrow_days: int = 0
    their_escrow_days: int = 0


_ERROR_PATTERNS: list[tuple[str, str]] = [
    ("invalid_token", r"no longer valid|больше не действительна|недействительна"),
    ("trade_banned", r"trade ban|запрет на обмен|заблокирован.*обмен"),
    ("cannot_trade", r"is not available to trade|not available for trade|недоступен для обмена|"
                     r"cannot trade with|не можете обмениваться|limited|ограничен"),
    ("inventory_private", r"inventory privacy|инвентарь скрыт|private inventory"),
]


def parse_new_offer_page(page: str) -> NewOfferPage:
    """Разбор HTML https://steamcommunity.com/tradeoffer/new/?partner=..&token=.. .

    Steam показывает ошибку в блоке #error_msg (или .error_page_content), а на рабочей
    странице выставляет JS-переменные g_daysMyEscrow / g_daysTheirEscrow.
    """
    err = re.search(r'id="error_msg"[^>]*>(.*?)</div>', page, re.S) or \
        re.search(r'class="error_page_content"[^>]*>(.*?)</div>', page, re.S)
    if err:
        text = re.sub(r"<[^>]+>", " ", err.group(1))
        text = re.sub(r"\s+", " ", html.unescape(text)).strip()
        code = "cannot_trade"
        for c, pat in _ERROR_PATTERNS:
            if re.search(pat, text, re.I):
                code = c
                break
        return NewOfferPage(ok=False, error_code=code, error_text=text)
    if "g_daysTheirEscrow" not in page and "g_ulTradePartnerSteamID" not in page:
        if "g_steamID = false" in page or "login/home" in page:
            return NewOfferPage(ok=False, error_code="not_logged_in", error_text="Steam-сессия бота не активна")
        return NewOfferPage(ok=False, error_code="unknown", error_text="не удалось разобрать страницу обмена")

    def _int(name: str) -> int:
        m = re.search(rf"{name}\s*=\s*(\d+)", page)
        return int(m.group(1)) if m else 0

    name = re.search(r"g_strTradePartnerPersonaName\s*=\s*\"((?:[^\"\\]|\\.)*)\"", page)
    sid = re.search(r"g_ulTradePartnerSteamID\s*=\s*'(\d+)'", page)
    partner_name = None
    if name:
        try:
            partner_name = name.group(1).encode().decode("unicode_escape").encode("latin-1").decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            partner_name = name.group(1)
    return NewOfferPage(ok=True, partner_name=partner_name, partner_steam_id=sid.group(1) if sid else None,
                        my_escrow_days=_int("g_daysMyEscrow"), their_escrow_days=_int("g_daysTheirEscrow"))
