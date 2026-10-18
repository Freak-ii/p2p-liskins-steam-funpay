"""Steam-клиент через веб (steamcommunity.com) + мобильные подтверждения."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import re
import secrets
import time
from urllib.parse import quote, unquote

import aiohttp
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from yarl import URL

from mannbot.core.models import SentOffer, TradeLinkCheck, TradeOfferState
from mannbot.steam import guard
from mannbot.steam.tradelink import TradeLink, parse_new_offer_page

log = logging.getLogger("steam")
COMMUNITY = "https://steamcommunity.com"
API = "https://api.steampowered.com"

# EResult при отправке трейда → понятная причина
SEND_ERRORS = {
    15: ("cannot_trade", "Steam не позволяет отправить обмен этому аккаунту (ограничения, переполненный "
                         "инвентарь или ссылка устарела)"),
    16: ("timeout", "Steam не ответил вовремя"),
    25: ("inventory_full", "инвентарь покупателя переполнен"),
    26: ("invalid_token", "ссылка на обмен устарела или предметы недоступны"),
    50: ("too_many_offers", "слишком много активных предложений обмена"),
}


class SteamError(Exception):
    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.code = code


class SteamAuthError(SteamError):
    pass


class SteamClient:
    def __init__(self, cfg, *, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.steam_id: str = str(cfg.steam_id or "")
        self.session: aiohttp.ClientSession | None = None
        self.access_token: str = ""
        self.refresh_token: str = ""
        self.session_id: str = secrets.token_hex(12)
        self.persona_name: str = ""
        self.level: int | None = None
        self._last_offer_at = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ http
    async def _sess(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                       "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"})
        return self.session

    async def _req(self, method: str, url: str, *, retries: int = 3, expect_json: bool = False, **kw):
        sess = await self._sess()
        for attempt in range(1, retries + 1):
            try:
                async with sess.request(method, url, **kw) as r:
                    text = await r.text()
                    if r.status == 429:
                        raise SteamError("Steam rate limit (429)", "rate_limit")
                    if r.status >= 500 and attempt < retries:
                        raise SteamError(f"HTTP {r.status}", "server")
                    if expect_json:
                        try:
                            return r.status, json.loads(text) if text else {}
                        except json.JSONDecodeError:
                            return r.status, {"_raw": text[:500]}
                    return r.status, text
            except (aiohttp.ClientError, asyncio.TimeoutError, SteamError) as e:
                if attempt == retries:
                    if isinstance(e, SteamError):
                        raise
                    raise SteamError(f"сеть: {e}", "network") from e
                delay = (20 if isinstance(e, SteamError) and e.code == "rate_limit" else 2) * attempt
                await asyncio.sleep(delay + random.random())

    def _set_cookie(self, name: str, value: str, domains=("steamcommunity.com", "store.steampowered.com",
                                                          "help.steampowered.com")) -> None:
        assert self.session is not None
        for d in domains:
            self.session.cookie_jar.update_cookies({name: value}, response_url=URL(f"https://{d}/"))

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    # ------------------------------------------------------------------ login
    async def login(self) -> None:
        await self._sess()
        st = self.cfg
        if st.login_secure_cookie:
            raw = unquote(st.login_secure_cookie)
            sid, _, token = raw.partition("||")
            self.steam_id = self.steam_id or sid
            self.access_token = token
            self._set_cookie("steamLoginSecure", quote(raw, safe=""))
        else:
            await self._login_with_password()
        self._set_cookie("sessionid", self.session_id)
        if not await self.is_logged_in():
            raise SteamAuthError("Steam: вход не выполнен (cookie недействительна или неверные данные)")
        await self._load_profile()
        log.info("Steam: вошли как %s (%s), уровень %s", self.persona_name, self.steam_id, self.level)

    def _encrypt_password(self, mod_hex: str, exp_hex: str) -> str:
        pub = rsa.RSAPublicNumbers(int(exp_hex, 16), int(mod_hex, 16)).public_key()
        return base64.b64encode(pub.encrypt(self.cfg.password.encode(), padding.PKCS1v15())).decode()

    async def _login_with_password(self) -> None:
        st = self.cfg
        hdr = {"Origin": COMMUNITY, "Referer": COMMUNITY + "/"}
        _, rsa_data = await self._req("GET", f"{API}/IAuthenticationService/GetPasswordRSAPublicKey/v1",
                                      params={"account_name": st.username}, expect_json=True, headers=hdr)
        r = rsa_data.get("response") or {}
        if not r.get("publickey_mod"):
            raise SteamAuthError("Steam: не получен RSA-ключ для входа")
        enc = self._encrypt_password(r["publickey_mod"], r["publickey_exp"])
        _, begin = await self._req("POST", f"{API}/IAuthenticationService/BeginAuthSessionViaCredentials/v1",
                                   data={"account_name": st.username, "encrypted_password": enc,
                                         "encryption_timestamp": r["timestamp"], "remember_login": "true",
                                         "platform_type": "2", "persistence": "1",
                                         "website_id": "Community"}, expect_json=True, headers=hdr)
        b = begin.get("response") or {}
        if not b.get("client_id"):
            raise SteamAuthError("Steam: неверный логин/пароль или требуется капча")
        self.steam_id = str(b.get("steamid") or self.steam_id)
        if any(c.get("confirmation_type") == 3 for c in b.get("allowed_confirmations", [])):
            await self._req("POST", f"{API}/IAuthenticationService/UpdateAuthSessionWithSteamGuardCode/v1",
                            data={"client_id": b["client_id"], "steamid": self.steam_id,
                                  "code": guard.generate_auth_code(st.shared_secret), "code_type": "3"},
                            expect_json=True, headers=hdr)
        for _ in range(10):
            _, poll = await self._req("POST", f"{API}/IAuthenticationService/PollAuthSessionStatus/v1",
                                      data={"client_id": b["client_id"], "request_id": b["request_id"]},
                                      expect_json=True, headers=hdr)
            p = poll.get("response") or {}
            if p.get("refresh_token"):
                self.refresh_token = p["refresh_token"]
                self.access_token = p.get("access_token", "")
                break
            await asyncio.sleep(float(b.get("interval", 2)))
        else:
            raise SteamAuthError("Steam: код Steam Guard не принят (проверьте shared_secret и время)")
        _, fin = await self._req("POST", "https://login.steampowered.com/jwt/finalizelogin",
                                 data={"nonce": self.refresh_token, "sessionid": self.session_id,
                                       "redirect_uri": f"{COMMUNITY}/login/home/?goto="},
                                 expect_json=True, headers=hdr)
        for t in fin.get("transfer_info", []):
            params = dict(t.get("params", {}))
            params["steamID"] = fin.get("steamID", self.steam_id)
            await self._req("POST", t["url"], data=params, headers=hdr)
        # подстраховка: cookie на основе access_token
        if self.access_token:
            self._set_cookie("steamLoginSecure", quote(f"{self.steam_id}||{self.access_token}", safe=""))

    async def refresh_access_token(self) -> None:
        if not self.refresh_token:
            await self.login()
            return
        _, data = await self._req("POST", f"{API}/IAuthenticationService/GenerateAccessTokenForApp/v1",
                                  data={"refresh_token": self.refresh_token, "steamid": self.steam_id},
                                  expect_json=True)
        token = (data.get("response") or {}).get("access_token")
        if not token:
            await self.login()
            return
        self.access_token = token
        self._set_cookie("steamLoginSecure", quote(f"{self.steam_id}||{token}", safe=""))

    async def is_logged_in(self) -> bool:
        status, page = await self._req("GET", f"{COMMUNITY}/my/tradeoffers/privacy", allow_redirects=True)
        return status == 200 and "g_steamID = false" not in page and "/login/home" not in page[:3000] \
            and ("tradeoffer" in page or "trade_offer_access_url" in page)

    async def ensure_session(self) -> None:
        if not await self.is_logged_in():
            log.warning("Steam-сессия истекла, перелогин")
            if self.refresh_token:
                await self.refresh_access_token()
            if not await self.is_logged_in():
                await self.login()

    async def _load_profile(self) -> None:
        _, page = await self._req("GET", f"{COMMUNITY}/profiles/{self.steam_id}/")
        m = re.search(r'class="actual_persona_name">([^<]+)<', page)
        self.persona_name = m.group(1).strip() if m else self.cfg.username or self.steam_id
        m = re.search(r'class="friendPlayerLevelNum">(\d+)<', page)
        self.level = int(m.group(1)) if m else None

    def _auth_params(self) -> dict:
        return {"key": self.cfg.api_key} if self.cfg.api_key else {"access_token": self.access_token}

    # ------------------------------------------------------------------ inventory
    async def get_tradable_keys(self) -> list[str]:
        """assetid всех обмениваемых ключей Mann Co. в инвентаре TF2."""
        st = self.cfg
        assets, descs, start = [], {}, None
        while True:
            params = {"l": "english", "count": 2000}
            if start:
                params["start_assetid"] = start
            status, data = await self._req(
                "GET", f"{COMMUNITY}/inventory/{self.steam_id}/{st.appid}/{st.contextid}",
                params=params, expect_json=True)
            if status != 200 or not data.get("success"):
                raise SteamError(f"инвентарь недоступен (HTTP {status})", "inventory")
            assets += data.get("assets", [])
            for d in data.get("descriptions", []):
                descs[(d["classid"], d.get("instanceid", "0"))] = d
            if not data.get("more_items"):
                break
            start = data.get("last_assetid")
        keys = []
        for a in assets:
            d = descs.get((a["classid"], a.get("instanceid", "0")))
            if d and d.get("market_hash_name") == st.key_market_hash_name and d.get("tradable") == 1:
                keys.append(str(a["assetid"]))
        return keys

    # ------------------------------------------------------------------ trade link check
    async def check_trade_link(self, link: TradeLink) -> TradeLinkCheck:
        """Открываем страницу нового обмена, как в браузере: Steam сам скажет, валидна ли ссылка,
        может ли аккаунт торговать и есть ли задержка обмена (trade hold)."""
        if link.steam_id64 == self.steam_id:
            return TradeLinkCheck(False, link.steam_id64, reason_code="self", reason_text="это ссылка бота")
        status, page = await self._req("GET", f"{COMMUNITY}/tradeoffer/new/",
                                       params={"partner": link.partner, "token": link.token})
        if status != 200:
            return TradeLinkCheck(False, link.steam_id64, reason_code="network",
                                  reason_text=f"Steam вернул HTTP {status}")
        p = parse_new_offer_page(page)
        if not p.ok:
            if p.error_code == "not_logged_in":
                raise SteamAuthError(p.error_text)
            return TradeLinkCheck(False, link.steam_id64, reason_code=p.error_code, reason_text=p.error_text)
        return TradeLinkCheck(True, p.partner_steam_id or link.steam_id64, partner_name=p.partner_name,
                              their_escrow_days=p.their_escrow_days,
                              extra={"my_escrow_days": p.my_escrow_days})

    # ------------------------------------------------------------------ offers
    async def send_offer(self, link: TradeLink, asset_ids: list[str], message: str) -> SentOffer:
        st = self.cfg
        async with self._lock:  # пауза между трейдами, чтобы не словить лимиты Steam
            wait = float(st.min_seconds_between_offers) - (time.monotonic() - self._last_offer_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_offer_at = time.monotonic()
        if self.dry_run:
            log.info("[DRY-RUN] трейд %s ключей → %s", len(asset_ids), link.steam_id64)
            return SentOffer(offer_id=f"dry{int(time.time())}", needs_confirmation=False, asset_ids=asset_ids)
        offer = {"newversion": True, "version": len(asset_ids) + 1,
                 "me": {"assets": [{"appid": int(st.appid), "contextid": str(st.contextid), "amount": 1,
                                    "assetid": a} for a in asset_ids], "currency": [], "ready": False},
                 "them": {"assets": [], "currency": [], "ready": False}}
        data = {"sessionid": self.session_id, "serverid": "1", "partner": link.steam_id64,
                "tradeoffermessage": message[:128], "json_tradeoffer": json.dumps(offer), "captcha": "",
                "trade_offer_create_params": json.dumps({"trade_offer_access_token": link.token})}
        headers = {"Referer": f"{COMMUNITY}/tradeoffer/new/?partner={link.partner}&token={link.token}",
                   "Origin": COMMUNITY}
        status, resp = await self._req("POST", f"{COMMUNITY}/tradeoffer/new/send", data=data, headers=headers,
                                       expect_json=True, retries=1)
        if status != 200 or not resp.get("tradeofferid"):
            err = str(resp.get("strError") or resp.get("_raw") or f"HTTP {status}")
            m = re.search(r"\((\d+)\)", err)
            code, human = SEND_ERRORS.get(int(m.group(1)) if m else -1, ("send_failed", err))
            raise SteamError(human, code)
        return SentOffer(offer_id=str(resp["tradeofferid"]),
                         needs_confirmation=bool(resp.get("needs_mobile_confirmation")), asset_ids=asset_ids)

    async def get_offer(self, offer_id: str) -> dict:
        _, data = await self._req("GET", f"{API}/IEconService/GetTradeOffer/v1",
                                  params={**self._auth_params(), "tradeofferid": offer_id, "language": "english"},
                                  expect_json=True)
        offer = (data.get("response") or {}).get("offer")
        if not offer:
            raise SteamError(f"не удалось получить трейд {offer_id}", "api")
        return offer

    async def get_offer_item_names(self, offer_id: str) -> list[str]:
        """market_hash_name каждого получаемого предмета входящего трейда (для проверки подарков)."""
        _, data = await self._req("GET", f"{API}/IEconService/GetTradeOffer/v1",
                                  params={**self._auth_params(), "tradeofferid": offer_id, "language": "english",
                                          "get_descriptions": 1}, expect_json=True)
        r = data.get("response") or {}
        desc = {(str(d.get("classid")), str(d.get("instanceid", "0"))): d.get("market_hash_name", "")
                for d in r.get("descriptions", [])}
        return [desc.get((str(i.get("classid")), str(i.get("instanceid", "0"))), "")
                for i in (r.get("offer") or {}).get("items_to_receive", [])]

    async def accept_offer(self, offer_id: str, partner_steam_id: str) -> bool:
        """Принять входящий трейд. Только для подарков, где мы ничего не отдаём: подтверждение не нужно."""
        if self.dry_run:
            log.info("[DRY-RUN] принял бы трейд %s", offer_id)
            return True
        status, resp = await self._req(
            "POST", f"{COMMUNITY}/tradeoffer/{offer_id}/accept",
            data={"sessionid": self.session_id, "serverid": "1", "tradeofferid": offer_id,
                  "partner": partner_steam_id, "captcha": ""},
            headers={"Referer": f"{COMMUNITY}/tradeoffer/{offer_id}/", "Origin": COMMUNITY},
            expect_json=True, retries=1)
        return status == 200 and not resp.get("strError")

    async def get_offer_state(self, offer_id: str) -> TradeOfferState:
        if offer_id.startswith("dry"):
            return TradeOfferState.ACCEPTED
        return TradeOfferState.parse((await self.get_offer(offer_id)).get("trade_offer_state", 1))

    async def get_active_offers(self) -> tuple[list[dict], list[dict]]:
        """(исходящие, входящие) активные трейды — для контроля чужих трейдов на аккаунте."""
        _, data = await self._req("GET", f"{API}/IEconService/GetTradeOffers/v1",
                                  params={**self._auth_params(), "get_sent_offers": 1, "get_received_offers": 1,
                                          "active_only": 1, "get_descriptions": 0}, expect_json=True)
        r = data.get("response") or {}
        return r.get("trade_offers_sent", []), r.get("trade_offers_received", [])

    async def cancel_offer(self, offer_id: str) -> bool:
        if self.dry_run or offer_id.startswith("dry"):
            return True
        status, _ = await self._req("POST", f"{COMMUNITY}/tradeoffer/{offer_id}/cancel",
                                    data={"sessionid": self.session_id}, headers={"Referer": COMMUNITY})
        return status == 200

    async def decline_offer(self, offer_id: str) -> bool:
        if self.dry_run:
            return True
        status, _ = await self._req("POST", f"{COMMUNITY}/tradeoffer/{offer_id}/decline",
                                    data={"sessionid": self.session_id}, headers={"Referer": COMMUNITY})
        return status == 200

    # ------------------------------------------------------------------ confirmations
    def _conf_params(self, tag: str) -> dict:
        ts = int(time.time())
        return {"p": guard.generate_device_id(self.steam_id), "a": self.steam_id,
                "k": guard.generate_confirmation_key(self.cfg.identity_secret, tag, ts), "t": ts,
                "m": "react", "tag": tag}

    async def confirm_offer(self, offer_id: str, attempts: int = 6) -> bool:
        """Подтверждает ТОЛЬКО подтверждение, созданное для нашего offer_id (никаких «подтвердить всё»)."""
        if self.dry_run or offer_id.startswith("dry"):
            return True
        for i in range(attempts):
            _, data = await self._req("GET", f"{COMMUNITY}/mobileconf/getlist", params=self._conf_params("conf"),
                                      expect_json=True)
            if data.get("needauth"):
                raise SteamAuthError("Steam: подтверждения требуют повторного входа")
            for conf in data.get("conf", []):
                if str(conf.get("creator_id")) == str(offer_id):
                    params = {**self._conf_params("allow"), "op": "allow", "cid": conf["id"], "ck": conf["nonce"]}
                    _, res = await self._req("GET", f"{COMMUNITY}/mobileconf/ajaxop", params=params,
                                             expect_json=True)
                    return bool(res.get("success"))
            await asyncio.sleep(float(self.cfg.confirm_poll_seconds) * (1 + i * 0.5))
        return False

    async def pending_confirmations(self) -> int:
        """Проверка identity_secret: число ожидающих мобильных подтверждений."""
        _, data = await self._req("GET", f"{COMMUNITY}/mobileconf/getlist", params=self._conf_params("conf"),
                                  expect_json=True)
        if data.get("needauth") or not data.get("success", False):
            raise SteamError(data.get("message") or "Steam отклонил запрос подтверждений (неверный identity_secret?)",
                             "confirmations")
        return len(data.get("conf", []))

    async def market_price(self, currency: int = 5) -> float | None:
        """Цена ключа на Торговой площадке Steam (currency 5 = RUB) — для аналитики."""
        _, data = await self._req("GET", f"{COMMUNITY}/market/priceoverview/",
                                  params={"appid": self.cfg.appid, "currency": currency,
                                          "market_hash_name": self.cfg.key_market_hash_name}, expect_json=True)
        raw = data.get("lowest_price") or data.get("median_price")
        if not raw:
            return None
        num = re.sub(r"[^\d,\.]", "", raw).replace(",", ".")
        try:
            return float(num.rstrip("."))
        except ValueError:
            return None
