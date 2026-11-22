"""Публичная страница активации кода Ozon: форма «код + @username» и страница статуса заказа.

Отдельный веб-сервер (не панель управления!): панель слушает 127.0.0.1 и закрыта токеном, а эту страницу
открывают покупатели, поэтому здесь только активация и статус по коду, без доступа к CRM.
"""
from __future__ import annotations

import asyncio
import time

import jinja2
from aiohttp import web

from starsbot.core import messages as M
from starsbot.core.models import OrderStatus as S
from starsbot.ozon import texts as T

MAX_BAD_PER_HOUR = 10

PAGE = jinja2.Environment(autoescape=True).from_string("""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{% if refresh %}<meta http-equiv="refresh" content="8">{% endif %}
<title>Активация Telegram Stars</title>
<style>
:root{--bg:#f4f6fb;--card:#fff;--fg:#1d2433;--muted:#6b7385;--acc:#2b7cff;--ok:#1f9d55;--bad:#d64545}
@media (prefers-color-scheme:dark){:root{--bg:#12151c;--card:#1b2029;--fg:#e8ebf2;--muted:#9aa3b5}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:460px;margin:0 auto;padding:24px 16px}
.card{background:var(--card);border-radius:14px;padding:20px;box-shadow:0 2px 12px rgba(0,0,0,.06)}
h1{font-size:22px;margin:0 0 6px} p{margin:8px 0} .muted{color:var(--muted);font-size:14px}
label{display:block;margin:14px 0 4px;font-weight:600}
input{width:100%;box-sizing:border-box;padding:12px;border:1px solid #c9cfdb;border-radius:10px;font-size:16px;
background:transparent;color:inherit}
button{margin-top:16px;width:100%;padding:13px;border:0;border-radius:10px;background:var(--acc);color:#fff;
font-size:16px;font-weight:600;cursor:pointer}
.msg{white-space:pre-wrap;border-radius:10px;padding:12px;margin-top:14px;background:rgba(43,124,255,.08)}
.err{background:rgba(214,69,69,.1)} .ok{background:rgba(31,157,85,.1)}
</style></head><body><main><div class="card">
<h1>⭐ Активация Telegram Stars</h1>
{% if order %}
  <p><b>{{ order.stars }} ⭐</b>{% if order.tg_username %} → @{{ order.tg_username }}{% endif %}</p>
  <p>Статус: <b>{{ status_text }}</b></p>
  {% for m in msgs %}<div class="msg {% if final_ok %}ok{% endif %}">{{ m }}</div>{% endfor %}
  {% if need_username %}
  <form method="post" action="/"><input type="hidden" name="code" value="{{ code }}">
    <label for="u">Username получателя в Telegram</label>
    <input id="u" name="username" placeholder="@username" autocomplete="off" required>
    <button>Отправить звёзды</button></form>
  {% endif %}
{% else %}
  <p class="muted">Введите код активации из заказа Ozon (он есть на странице заказа и в письме) и username
  аккаунта, на который зачислить звёзды.</p>
  {% if error %}<div class="msg err">{{ error }}</div>{% endif %}
  <form method="post" action="/">
    <label for="c">Код активации</label>
    <input id="c" name="code" value="{{ code }}" placeholder="{{ example }}" autocomplete="off" required>
    <label for="u">Username получателя в Telegram</label>
    <input id="u" name="username" value="{{ username }}" placeholder="@username" autocomplete="off" required>
    <button>Активировать</button></form>
  <p class="muted">Нужен только username — никакие пароли и коды из Telegram не вводите и никому не сообщайте.</p>
{% endif %}
</div></main></body></html>""")


class ActivationWeb:
    def __init__(self, channel, host: str = "0.0.0.0", port: int = 8084):
        self.channel = channel
        self.host = host
        self.port = port
        self.bad: dict[str, list[float]] = {}
        prefix = T.normalize_code(channel.c.code_prefix)[:6]
        self.example = f"{prefix}-XXXX-XXXX-XXXX" if prefix else "XXXX-XXXX-XXXX"

    def _ip(self, request: web.Request) -> str:
        return request.headers.get("X-Forwarded-For", request.remote or "?").split(",")[0].strip()

    def _limited(self, ip: str) -> bool:
        now = time.time()
        self.bad[ip] = [t for t in self.bad.get(ip, []) if now - t < 3600]
        return len(self.bad[ip]) >= MAX_BAD_PER_HOUR

    def _page(self, **ctx) -> web.Response:
        ctx.setdefault("example", self.example)
        ctx.setdefault("code", "")
        ctx.setdefault("username", "")
        return web.Response(text=PAGE.render(**ctx), content_type="text/html",
                            headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                     "Referrer-Policy": "no-referrer"})

    def _status_page(self, code: str, extra: list[str] | None = None) -> web.Response:
        c = self.channel.code_row(code)
        o = self.channel.db.get_order(c["order_id"]) if c else None
        if o is None:
            return self._page(error=T.BOT["bad_code"], code=code)
        st = S(o["status"])
        msgs = extra if extra is not None else [r["text"] for r in self.channel.db.q(
            "SELECT text FROM messages WHERE order_id=? AND direction='out' ORDER BY id DESC LIMIT 1", (o["order_id"],))]
        return self._page(order=o, code=c["display"], msgs=msgs, status_text=M.status_text("ru", st.value),
                          need_username=st == S.AWAITING_USERNAME, final_ok=st in (S.DELIVERED, S.COMPLETED),
                          refresh=st in (S.READY, S.PURCHASING, S.SENT, S.PENDING_APPROVAL) or
                          (st == S.AWAITING_USERNAME and bool(o["tg_username"])))

    async def index(self, request: web.Request) -> web.Response:
        code = request.query.get("code", "")
        if code and self.channel.code_row(code) is not None and self.channel.code_row(code)["redeemed_at"]:
            return self._status_page(code)
        return self._page(code=code)

    async def submit(self, request: web.Request) -> web.Response:
        data = await request.post()
        code, username = str(data.get("code", "")).strip(), str(data.get("username", "")).strip()
        ip = self._ip(request)
        if self._limited(ip):
            return self._page(error=T.BOT["too_many"], code=code, username=username)
        chk = self.channel.check_code(code)
        if not chk.ok:
            if chk.text == T.BOT["bad_code"]:
                self.bad.setdefault(ip, []).append(time.time())
            return self._page(error=chk.text, code=code, username=username)
        if chk.need != "username":
            return self._status_page(code)
        res = await self.channel.redeem(code, username, source="web")
        if res.need == "username" and not res.order_id:
            return self._page(error=res.text, code=code, username=username)
        if res.text == T.BOT["bad_format"]:
            return self._status_page(code, [res.text])
        raise web.HTTPFound(f"/s/{T.normalize_code(code)}")

    async def status(self, request: web.Request) -> web.Response:
        code = request.match_info["code"]
        c = self.channel.code_row(code)
        if c is None or not c["redeemed_at"]:
            # статус открывается только после активации: иначе по ссылке можно было бы перебирать коды
            return self._page(code="")
        return self._status_page(code)

    def app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024)
        app.add_routes([web.get("/", self.index), web.post("/", self.submit), web.get("/s/{code}", self.status)])
        return app

    async def run(self) -> None:
        runner = web.AppRunner(self.app(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, self.host, self.port).start()
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
