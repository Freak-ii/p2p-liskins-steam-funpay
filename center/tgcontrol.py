"""Управление центром из Telegram: команды и кнопки одобрения предложений координатора.

Работает только со своим Telegram-ботом центра (токен в «Настройках уведомлений»): токен LisBot уже
читает сам LisBot, а два читателя одного бота мешают друг другу (Telegram отдаёт обновление только одному).
Команды принимаются только из chat id, указанных в настройках уведомлений; остальные молча игнорируются.

Команды: /status, /report, /stock, /proposals, /pause и /resume (lisbot | mannbot), /buypause, /buyresume,
/backup, /help. Кнопки под предложением координатора: «Одобрить» / «Отклонить»; действия уровня
«нужно подтверждение» требуют второго нажатия, а боевой режим и ручные действия одобряются только в панели.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import time

import aiohttp

log = logging.getLogger("center.telegram")

BOT_NAMES = {"lisbot": "lisbot", "lis": "lisbot", "mannbot": "mannbot", "mann": "mannbot"}
PANEL_ONLY = {"set_live_mode", "withdraw"}          # деньги и боевой режим — только из панели
HELP = ("<b>Центр перепродажи</b>\n"
        "/status — состояние ботов и тревоги\n"
        "/report — отчёт за сутки, /week — за неделю\n"
        "/stock — склад LisBot и ключи MannBot, /goal — цель месяца\n"
        "/proposals — предложения координатора с кнопками\n"
        "/pause lisbot|mannbot — пауза торговли, /resume — продолжить\n"
        "/buypause, /buyresume — закупки LisBot\n"
        "/backup — резервная копия сейчас")


class TelegramControl:
    def __init__(self, center):
        self.center = center
        self._task: asyncio.Task | None = None
        self.state = {"active": False, "info": "не запущено"}

    # ------------------------------------------------------------------ настройки
    def target(self) -> tuple[str, list[str]]:
        """Токен и разрешённые чаты, если команды доступны: свой токен центра и включённые команды."""
        m = self.center.monitor
        tg = m.cfg.get("telegram") or {}
        token, ids, src = m.telegram_target()
        if not tg.get("commands", True):
            self.state = {"active": False, "info": "выключено в настройках"}
            return "", []
        if not token:
            self.state = {"active": False, "info": "Telegram не настроен"}
            return "", []
        if src != "центр":
            self.state = {"active": False, "info": "нужен свой Telegram-бот центра (токен LisBot читает сам LisBot)"}
            return "", []
        return token, ids

    async def _api(self, token: str, method: str, payload: dict, timeout: float = 15) -> dict:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.post(f"https://api.telegram.org/bot{token}/{method}", json=payload) as r:
                return await r.json(content_type=None)

    # ------------------------------------------------------------------ кнопки
    @staticmethod
    def keyboard(d: dict) -> dict | None:
        if not d or d.get("kind") != "proposal" or d.get("status") != "proposed":
            return None
        if d.get("tool") in PANEL_ONLY or d.get("tier") == "manual":
            return None
        return {"inline_keyboard": [[{"text": "✅ Одобрить", "callback_data": f"co:a:{d['id']}"},
                                     {"text": "❌ Отклонить", "callback_data": f"co:r:{d['id']}"}]]}

    # ------------------------------------------------------------------ команды
    def _status_text(self) -> str:
        c = self.center
        lines = ["<b>Центр перепродажи</b>" + (" · демо" if c.demo else "")]
        for name, w in c.workers.items():
            s = c.summaries.get(name) or {}
            mode = "" if not s else " · демо" if s.get("demo") else " · тестовый режим" if s.get("dry_run") else " · <b>боевой</b>"
            trade = {"running": "торгует", "paused": "на паузе", "halted": "аварийная остановка"}.get(s.get("bot_state"), "")
            lines.append(f"\n<b>{html.escape(w.title)}</b>: {html.escape(w.info().get('state_text') or w.state)}"
                         + (f", {trade}" if trade else "") + mode)
            kpi = [f"{html.escape(str(k))}: {v}" for k, v in (s.get("kpi") or {}).items() if v is not None]
            if kpi:
                lines.append(" · ".join(kpi[:6]))
        open_alerts = [a for a in c.alerts(30) if not a["acked"] and a["level"] not in ("info", "ok")]
        if open_alerts:
            lines.append(f"\n⚠ Неподтверждённых тревог: {len(open_alerts)}")
            lines += [f"• {html.escape(a['source'])}: {html.escape(a['message'][:150])}" for a in open_alerts[:5]]
        props = c.coordinator.decisions(50, kind="proposal", status="proposed")
        if props:
            lines.append(f"\n🤖 Ждут решения: {len(props)} (/proposals)")
        return "\n".join(lines)

    async def handle_command(self, text: str) -> list[tuple[str, dict | None]]:
        """Ответы на команду: [(текст, клавиатура)]."""
        c = self.center
        parts = text.strip().split()
        cmd = parts[0].split("@")[0].lower() if parts else ""
        arg = parts[1].lower() if len(parts) > 1 else ""
        if cmd in ("/start", "/help"):
            return [(HELP, None)]
        if cmd == "/status":
            return [(self._status_text(), None)]
        if cmd == "/report":
            return [(c.monitor.daily_report(), None)]
        if cmd == "/goal":
            g = await c.analytics.goal()
            if not g["goal"]:
                return [(f"Прибыль с начала месяца {g['profit']:,.0f} ₽, прогноз {g['forecast']:,.0f} ₽. "
                         "Цель задаётся в панели: «Аналитика» → «Цель месяца».".replace(",", " "), None)]
            return [((f"🎯 {g['profit']:,.0f} из {g['goal']:,.0f} ₽ ({g['progress']}%), прогноз {g['forecast']:,.0f} ₽"
                      + (f", нужно {g['need_per_day']:,.0f} ₽ в день" if g["need_per_day"] else "")).replace(",", " "), None)]
        if cmd == "/week":
            return [(await c.monitor.weekly_report(), None)]
        if cmd == "/stock":
            lines = c.inventory.report_lines()
            return [("\n".join(html.escape(x) for x in lines) or "Склады пусты или нет данных", None)]
        if cmd == "/proposals":
            props = c.coordinator.decisions(20, kind="proposal", status="proposed")
            if not props:
                return [("Предложений координатора нет", None)]
            out = []
            for d in props[:10]:
                kb = self.keyboard(d)
                note = "" if kb else "\n<i>Одобряется только в панели центра</i>"
                out.append((f"🤖 <b>{html.escape(d['title'])}</b>\n{html.escape(d.get('reason') or '')}{note}", kb))
            return out
        if cmd in ("/pause", "/resume"):
            bot = BOT_NAMES.get(arg)
            if not bot:
                return [(f"Укажите бота: {cmd} lisbot или {cmd} mannbot", None)]
            return [(await self._tool("pause_bot" if cmd == "/pause" else "resume_bot", {"bot": bot}), None)]
        if cmd in ("/buypause", "/buyresume"):
            return [(await self._tool("pause_buying" if cmd == "/buypause" else "resume_buying", {"bot": "lisbot"}), None)]
        if cmd == "/backup":
            r = await c.backup.run(manual=True)
            return [(f"💾 Копия {html.escape(r['name'])} готова ({r['size'] / 1e6:.1f} МБ)" if r["ok"] else
                     f"✗ Копия не удалась: {html.escape(r.get('error') or '; '.join(r.get('problems', [])))}", None)]
        return [("Не знаю такой команды. /help — список команд", None)]

    async def _tool(self, tool: str, args: dict) -> str:
        co = self.center.coordinator
        title = co.describe(tool, co.validate(tool, args))
        try:
            result = await co.execute(tool, args)
        except Exception as e:  # noqa: BLE001
            self.center.events.add("center", "warn", "telegram.command", f"Telegram: {title} — не удалось: {e}")
            return f"✗ {html.escape(title)}: {html.escape(str(e))}"
        self.center.events.add("center", "info", "telegram.command", f"Telegram: {title} — {result}")
        return f"✓ {html.escape(title)}: {html.escape(str(result))}"

    async def handle_callback(self, data: str) -> tuple[str, dict | None]:
        """Нажатие кнопки → (новый текст сообщения, новая клавиатура)."""
        try:
            prefix, verb, did = data.split(":")
            did = int(did)
        except ValueError:
            return "Кнопка устарела", None
        if prefix != "co":
            return "Кнопка устарела", None
        co = self.center.coordinator
        d = co.get(did)
        if d is None:
            return "Предложение не найдено", None
        head = f"🤖 <b>{html.escape(d['title'])}</b>\n"
        if verb == "r":
            ok, msg = await co.decide(did, False)
            return head + ("❌ Отклонено" if ok else html.escape(msg)), None
        if d.get("tool") in PANEL_ONLY or d.get("tier") == "manual":
            return head + "Это действие одобряется только в панели центра", None
        tier = co.tier(d["tool"]) if d.get("tool") else "propose"
        if verb == "a" and "confirm" in (tier, d.get("tier")):
            return head + "Это меняет лимиты или режим. Точно выполнить?", {"inline_keyboard": [[
                {"text": "✅ Да, выполнить", "callback_data": f"co:c:{did}"},
                {"text": "❌ Отклонить", "callback_data": f"co:r:{did}"}]]}
        if verb not in ("a", "c"):
            return "Кнопка устарела", None
        ok, msg = await co.decide(did, True, confirmed=verb == "c")
        return head + ("✅ " if ok else "✗ ") + html.escape(msg), None

    # ------------------------------------------------------------------ чтение обновлений
    async def process(self, token: str, allowed: list[str], upd: dict) -> None:
        if "callback_query" in upd:
            q = upd["callback_query"]
            msg = q.get("message") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            if chat not in allowed:
                return
            text, kb = await self.handle_callback(str(q.get("data") or ""))
            await self._api(token, "answerCallbackQuery", {"callback_query_id": q.get("id")})
            payload = {"chat_id": chat, "message_id": msg.get("message_id"), "text": text[:4000], "parse_mode": "HTML"}
            if kb:
                payload["reply_markup"] = kb
            await self._api(token, "editMessageText", payload)
            return
        msg = upd.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id", ""))
        text = str(msg.get("text") or "")
        if chat not in allowed or not text.startswith("/"):
            return
        for body, kb in await self.handle_command(text):
            payload = {"chat_id": chat, "text": body[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
            if kb:
                payload["reply_markup"] = kb
            await self._api(token, "sendMessage", payload)

    async def poll_once(self) -> int:
        token, allowed = self.target()
        if not token:
            return -1
        m = self.center.monitor
        offset = int(m.kv_get("tg_offset") or 0)
        r = await self._api(token, "getUpdates", {"offset": offset, "timeout": 25,
                                                  "allowed_updates": ["message", "callback_query"]}, timeout=40)
        if not r.get("ok"):
            self.state = {"active": False, "info": f"Telegram: {str(r.get('description') or r)[:120]}"}
            return -1
        self.state = {"active": True, "info": "команды работают", "at": time.time()}
        n = 0
        for upd in r.get("result") or []:
            m.kv_set("tg_offset", int(upd["update_id"]) + 1)      # до обработки: сбойное обновление не повторяется
            try:
                await self.process(token, allowed, upd)
            except Exception:  # noqa: BLE001
                log.exception("команда Telegram")
            n += 1
        return n

    async def _loop(self) -> None:
        while True:
            try:
                if await self.poll_once() < 0:
                    await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, json.JSONDecodeError) as e:
                self.state = {"active": False, "info": f"нет связи с Telegram: {e}"}
                await asyncio.sleep(30)
            except Exception:  # noqa: BLE001
                log.exception("Telegram-команды")
                await asyncio.sleep(30)

    async def start(self) -> None:
        if not self.center.demo:
            self._task = asyncio.create_task(self._loop(), name="telegram-control")
        else:
            self.state = {"active": False, "info": "в демо выключено"}

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
