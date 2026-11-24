"""Агент канала магазина (DPB Fastchange): готовит посты и кладёт их в очередь черновиков на одобрение.

Что делает за проход (раз в agents.channel.interval_minutes, пока агент запущен кнопкой):
- новости: читает публичные каналы из agents.channel.sources (t.me/s/…), берёт свежие посты по ключевым словам
  и пересказывает своими словами со ссылкой на источник;
- сезонные поводы: Новый год, Хэллоуин, Чёрная пятница, распродажи Steam и т.д. (center/agents/seasons.py) —
  черновик за season_lead_days до даты;
- акции: анонс каждой акции из agents.channel.promos (заводит владелец) и напоминание в последний день;
- развитие проекта: пункты из agents.channel.achievements (пишет владелец) и круглые числа выполненных заказов
  магазина (share_milestones) — только «1000 заказов», без денег;
- обычный чаттинг раз в chatter_every_days: советы, безопасность, вопросы подписчикам.

Публикация в боевой канал — только после «Одобрить» и только если включено publish_enabled. Одобренный пост
уходит сразу или в выбранное время. Каждый черновик проходит фильтр конфиденциального (guard.py): с проблемами
его нельзя одобрить, пока текст не исправлен.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import re
import sqlite3
import time
from pathlib import Path

import aiohttp
import yaml

from center.agents import Agent, register
from center.agents import guard, news, seasons
from center.agents.writer import TIPS, Writer, WriterClaude, SYSTEM as WRITER_SYSTEM
from center.coordinator import BackendError

KINDS = {"news": "Новость", "season": "Сезонный повод", "promo": "Акция", "milestone": "Достижение",
         "achievement": "Развитие проекта", "chatter": "Обычный пост", "manual": "Свой пост"}
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря")


def date_text(d: dt.date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]}"


def parse_date(v) -> dt.date | None:
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v).strip()[:10]) if v else None
    except ValueError:
        return None


@register
class ChannelAgent(Agent):
    id = "channel"
    title = "Агент канала"
    description = "Новости, акции, сезонные поводы и обычные посты для Telegram-канала магазина — через одобрение"
    default_interval_minutes = 60

    def __init__(self, center, store):
        super().__init__(center, store)
        self.writer_override: Writer | None = None      # тесты
        self.fetch_override = None                      # тесты: async (channel) -> list[post]
        self.send_override = None                       # тесты: async (method, payload) -> dict | None
        self.today_override: dt.date | None = None

    # ------------------------------------------------------------ откуда брать магазин, бота и канал
    def shop_cfg(self) -> dict:
        """Настройки ShopBot (только чтение): токен бота, @канал, имя магазина. Свои значения агента сильнее."""
        w = self.center.workers.get("shopbot")
        path: Path | None = getattr(w, "_cfg_path", None) if w is not None else None
        if path is None or not path.exists():
            return {}
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return {}

    def target(self) -> dict:
        c, s = self.cfg, self.shop_cfg()
        tg, ch, gen = s.get("telegram") or {}, s.get("channel") or {}, s.get("general") or {}
        token = str(c.get("bot_token") or tg.get("bot_token") or "")
        return {"token": token, "chat_id": str(c.get("chat_id") or ch.get("chat_id") or ""),
                "bot_username": str(c.get("bot_username") or tg.get("bot_username") or "").lstrip("@"),
                "shop": str(c.get("shop_name") or gen.get("shop_name") or "магазин")}

    def writer(self) -> Writer:
        if self.writer_override is not None:
            return self.writer_override
        t = self.target()
        co = self.center.coordinator
        ai = co.cfg.get("ai") or {}
        key = co.api_key()
        backend = None
        if self.cfg.get("ai", True) and key and not self.center.demo and ai.get("enabled", True):
            try:
                system = WRITER_SYSTEM.format(shop=t["shop"], bot="@" + (t["bot_username"] or "бота"))
                backend = WriterClaude(key, str(ai.get("model") or "claude-opus-5-5"), "low", system)
            except BackendError:
                backend = None
        return Writer(backend, t["shop"])

    def today(self) -> dt.date:
        return self.today_override or dt.date.today()

    # ------------------------------------------------------------ фильтр
    def promos(self) -> list[dict]:
        return [p for p in (self.cfg.get("promos") or []) if isinstance(p, dict) and (p.get("id") or p.get("title"))]

    def allowed_percents(self) -> set[float]:
        out = set()
        for p in self.promos():
            try:
                out.add(float(p.get("discount_pct")))
            except (TypeError, ValueError):
                pass
        return out

    def check(self, text: str) -> list[str]:
        return guard.check(text, allow_prices=bool(self.cfg.get("allow_prices", False)),
                           allowed_percents=self.allowed_percents(), extra_words=self.cfg.get("forbidden_words") or [])

    async def draft(self, kind: str, key: str, task: dict, source_url: str = "") -> int | None:
        if self.has_key(key):
            return None
        data, meta = await self.writer().write(kind, task)
        text = guard.safe_html(str(data.get("text") or ""))
        title = str(data.get("title") or KINDS.get(kind, kind))
        return self.add_item(kind, title, text, key=key, source_url=source_url, issues=self.check(text),
                             meta={"model": meta.get("model"), **({"fallback": meta["fallback"]} if meta.get("fallback") else {}),
                                   **({"for_date": task["date"]} if task.get("date") else {})})

    # ------------------------------------------------------------ один проход
    async def run_once(self) -> str:
        made: dict[str, int] = {}

        def count(kind: str, iid: int | None) -> None:
            if iid:
                made[kind] = made.get(kind, 0) + 1

        today = self.today()
        c = self.cfg
        for s, d in seasons.due(today, int(c.get("season_lead_days", 3))):
            if s.id in (c.get("seasons_off") or []):
                continue
            count("season", await self.draft("season", f"season:{s.id}:{d.year}",
                                             {"title": s.title, "date": d.isoformat(), "date_text": date_text(d),
                                              "idea": s.idea, "approx": s.approx}))
        for p in self.promos():
            pid = str(p.get("id") or p.get("title"))
            start, end = parse_date(p.get("start")), parse_date(p.get("end"))
            if end and today > end:
                continue
            lead = int(c.get("promo_lead_days", 1))
            if start and (start - today).days > lead:
                continue
            period = ""
            if start and end:
                period = f"С {date_text(start)} по {date_text(end)}."
            elif end:
                period = f"До {date_text(end)} включительно."
            count("promo", await self.draft("promo", f"promo:{pid}", {"promo": _public_promo(p), "period": period}))
            if end and today == end and end != start:
                count("promo", await self.draft("promo", f"promo:{pid}:last", {"promo": _public_promo(p),
                                                                              "period": "Последний день акции!"}))
        for a in c.get("achievements") or []:
            text = str(a.get("text") if isinstance(a, dict) else a).strip()
            if text:
                count("achievement", await self.draft("achievement", "ach:" + _slug(text), {"text": text}))
        if c.get("share_milestones", True):
            for what, key in self.milestones():
                count("milestone", await self.draft("milestone", key, {"what": what}))
        count_news = await self.collect_news()
        if count_news:
            made["news"] = count_news
        if await self.maybe_chatter():
            made["chatter"] = 1
        published = await self.publish_due()
        parts = [f"{KINDS[k].lower()}: {n}" for k, n in made.items()]
        res = ("новых черновиков — " + ", ".join(parts)) if parts else "новых черновиков нет"
        if published:
            res += f"; опубликовано по расписанию: {published}"
        if parts:
            self.log("info", f"{self.title}: {res}")
            if c.get("notify_drafts", True):
                await self._notify(f"📝 Агент канала: {res}. Одобрить — раздел «Агенты» в панели центра.")
        return res

    async def _notify(self, text: str) -> None:
        mon = getattr(self.center, "monitor", None)
        if mon is None:
            return
        try:
            await mon.notify("agent.channel.drafts", "info", text)
        except Exception:  # noqa: BLE001 — уведомление не обязательно
            pass

    # ------------------------------------------------------------ новости
    async def collect_news(self) -> int:
        c = self.cfg
        sources = [news.channel_name(s) for s in (c.get("sources") or []) if str(s).strip()]
        if not sources:
            return 0
        keywords = [str(k).lower() for k in (c.get("keywords") or []) if str(k).strip()]
        max_age = float(c.get("news_max_age_hours", 24)) * 3600
        per_run, per_day = int(c.get("news_per_run", 2)), int(c.get("news_per_day", 4))
        day_ago = time.time() - 86400
        today_n = self.store.q("SELECT COUNT(*) n FROM agent_items WHERE agent=? AND kind='news' AND created_at>?",
                               (self.id, day_ago))[0]["n"]
        budget = max(0, min(per_run, per_day - today_n))
        made, errors = 0, []
        fetch = self.fetch_override or news.fetch
        candidates = []
        for src in sources:
            try:
                posts = await fetch(src)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, RuntimeError, ValueError) as e:
                errors.append(f"{src}: {e}")
                continue
            for p in posts:
                if p.get("ts") and time.time() - p["ts"] > max_age:
                    continue
                if keywords and not any(k in p["text"].lower() for k in keywords):
                    continue
                if len(p["text"]) < 40 or self.has_key("news:" + p["post"]):
                    continue
                candidates.append(p)
        candidates.sort(key=lambda p: p.get("ts") or 0, reverse=True)
        for p in candidates[:budget]:
            iid = await self.draft("news", "news:" + p["post"],
                                   {"source_text": p["text"][:3000], "source_url": p["url"]}, source_url=p["url"])
            made += 1 if iid else 0
        if errors:
            self.kv_set("news_errors", {"ts": time.time(), "errors": errors[:10]})
        return made

    # ------------------------------------------------------------ достижения магазина
    def shop_orders(self) -> int | None:
        """Число выполненных заказов магазина (база ShopBot, только чтение). None — базы нет."""
        w = self.center.workers.get("shopbot")
        if w is None:
            return None
        db = Path(w.home) / ("demo" if getattr(w, "demo", False) else "") / "data" / "shopbot.sqlite3"
        if not db.exists():
            return None
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            try:
                return int(con.execute("SELECT COUNT(*) FROM orders WHERE status='delivered'").fetchone()[0])
            finally:
                con.close()
        except sqlite3.Error:
            return None

    def milestones(self) -> list[tuple[str, str]]:
        n = self.shop_orders()
        if n is None:
            return []
        tiers = sorted(int(x) for x in (self.cfg.get("milestones") or []) if str(x).isdigit())
        reached = [t for t in tiers if n >= t]
        if not reached:
            return []
        top = reached[-1]                      # только последний достигнутый порог, без «догоняющих» постов
        return [(f"{_tier(top)} выполненных заказов!", f"milestone:orders:{top}")]

    # ------------------------------------------------------------ обычный чаттинг
    async def maybe_chatter(self) -> bool:
        every = float(self.cfg.get("chatter_every_days", 3))
        if every <= 0:
            return False
        last = self.store.q("SELECT MAX(created_at) t FROM agent_items WHERE agent=? AND kind IN ('chatter','news',"
                            "'achievement','milestone','manual')", (self.id,))[0]["t"]
        if last and time.time() - last < every * 86400:
            return False
        idx = int(self.kv_get("chatter_index", 0))
        rubric = TIPS[idx % len(TIPS)][0]
        iid = await self.draft("chatter", f"chatter:{self.today().isoformat()}", {"rubric": rubric, "index": idx})
        self.kv_set("chatter_index", idx + 1)
        return bool(iid)

    # ------------------------------------------------------------ одобрение и публикация
    def edit(self, iid: int, text: str | None = None, title: str | None = None) -> dict:
        it = self.item(iid)
        if it is None:
            raise LookupError("нет такого черновика")
        if it["status"] not in ("draft", "approved", "failed", "rejected"):
            raise ValueError("опубликованный пост здесь не меняется")
        fields: dict = {}
        if text is not None:
            text = guard.safe_html(text)
            fields.update(text=text, issues=self.check(text))
            if it["status"] in ("approved", "rejected", "failed"):
                fields["status"] = "draft"               # после правки — снова на одобрение
        if title is not None:
            fields["title"] = title.strip()[:200] or it["title"]
        self.update_item(iid, **fields)
        return self.item(iid) or {}

    def manual(self, text: str, title: str = "") -> int:
        text = guard.safe_html(text)
        iid = self.add_item("manual", title.strip() or "Свой пост", text, key=f"manual:{time.time_ns()}",
                            issues=self.check(text))
        assert iid is not None
        return iid

    async def approve(self, iid: int, at: float | None = None) -> str:
        it = self.item(iid)
        if it is None:
            raise LookupError("нет такого черновика")
        if it["status"] not in ("draft", "failed"):
            raise ValueError(f"черновик в статусе «{it['status']}»")
        issues = self.check(it["text"])                   # заново: настройки и акции могли поменяться
        if issues:
            self.update_item(iid, issues=issues)
            raise ValueError("сначала уберите из текста: " + "; ".join(issues))
        if not it["text"].strip():
            raise ValueError("пустой пост")
        self.update_item(iid, status="approved", decided_at=time.time(), scheduled_at=at, issues=[], error="")
        self.log("info", f"{self.title}: одобрен пост #{iid} «{it['title']}»")
        if at and at > time.time():
            return "одобрено, уйдёт в канал " + time.strftime("%d.%m %H:%M", time.localtime(at)) + \
                   ("" if self.cfg.get("publish_enabled") else " (когда включите публикацию)")
        return await self.publish(iid)

    def reject(self, iid: int) -> str:
        it = self.item(iid)
        if it is None:
            raise LookupError("нет такого черновика")
        if it["status"] == "published":
            raise ValueError("пост уже опубликован")
        self.update_item(iid, status="rejected", decided_at=time.time())
        return "отклонено"

    async def publish(self, iid: int) -> str:
        it = self.item(iid)
        if it is None:
            raise LookupError("нет такого черновика")
        if it["status"] != "approved":
            raise ValueError("публикуются только одобренные посты")
        if not self.cfg.get("publish_enabled", False):
            return "одобрено; публикация в канал выключена (agents.channel.publish_enabled) — включите её в настройках агента"
        if self.check(it["text"]):
            self.update_item(iid, status="draft", issues=self.check(it["text"]))
            raise ValueError("текст не прошёл фильтр — вернул в черновики")
        t = self.target()
        if not t["token"] or not t["chat_id"]:
            return "одобрено; нет токена бота или @канала (настройки ShopBot или agents.channel)"
        payload: dict = {"chat_id": t["chat_id"], "text": it["text"], "parse_mode": "HTML",
                         "disable_web_page_preview": it["kind"] != "news"}
        if self.cfg.get("button", True) and t["bot_username"]:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": "🛒 Открыть магазин",
                                                             "url": f"https://t.me/{t['bot_username']}?startapp"}]]}
        res, err = await self._send(t["token"], "sendMessage", payload)
        if not res:
            self.update_item(iid, status="failed", error=err)
            self.log("warn", f"{self.title}: пост #{iid} не опубликован — {err}")
            return "не опубликовано: " + err
        self.update_item(iid, status="published", published_at=time.time(), message_id=res.get("message_id"), error="")
        self.log("info", f"{self.title}: опубликован пост #{iid} «{it['title']}» (message_id {res.get('message_id')})")
        return f"опубликовано в {t['chat_id']}"

    async def publish_due(self) -> int:
        if not self.cfg.get("publish_enabled", False):
            return 0
        n = 0
        for it in self.store.q("SELECT id FROM agent_items WHERE agent=? AND status='approved' AND scheduled_at IS NOT NULL "
                               "AND scheduled_at<=? ORDER BY scheduled_at", (self.id, time.time())):
            try:
                if (await self.publish(it["id"])).startswith("опубликовано"):
                    n += 1
            except ValueError:
                continue
        return n

    async def _send(self, token: str, method: str, payload: dict) -> tuple[dict | None, str]:
        if self.send_override is not None:
            return await self.send_override(method, payload)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
                async with s.post(f"https://api.telegram.org/bot{token}/{method}", json=payload) as r:
                    data = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as e:
            return None, f"Telegram недоступен: {e}"
        if not data.get("ok"):
            return None, f"Telegram: {data.get('description') or 'ошибка'} (бот — админ канала?)"
        return data.get("result") or {}, ""

    # ------------------------------------------------------------ партнёры (метки трафика ShopBot)
    def partner_links(self, tag: str) -> dict:
        """Ссылки для партнёра: бот с меткой src_<tag> (ShopBot считает запуски и заказы по ней)."""
        tag = re.sub(r"[^a-z0-9_]", "", tag.lower())[:32]
        if not tag:
            raise ValueError("метка: латиница, цифры и _ (до 32 символов)")
        bot = self.target()["bot_username"] or "your_shop_bot"
        return {"tag": tag, "bot": f"https://t.me/{bot}?start=src_{tag}",
                "bot_stars": f"https://t.me/{bot}?start=src_{tag}__stars"}

    async def sources(self, days: int = 30) -> dict:
        """Сводка ShopBot по меткам: [{source, starts, buyers, orders, revenue_rub}] (без маржи)."""
        w = self.center.workers.get("shopbot")
        if w is None or getattr(w, "state", "") != "running":
            return {"ok": False, "message": "ShopBot не запущен — сводка по партнёрам появится, когда он работает",
                    "rows": []}
        token = str((self.shop_cfg().get("dashboard") or {}).get("token") or "")
        url = f"http://127.0.0.1:{w.port}/api/sources?days={int(days)}"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.get(url, headers={"X-Dashboard-Token": token}) as r:
                    if r.status == 404:
                        return {"ok": False, "message": "в этой версии ShopBot ещё нет учёта меток (нужна 1.6)", "rows": []}
                    if r.status != 200:
                        return {"ok": False, "message": f"ShopBot ответил {r.status}", "rows": []}
                    data = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as e:
            return {"ok": False, "message": f"ShopBot не ответил: {e}", "rows": []}
        rows = data if isinstance(data, list) else data.get("rows") or data.get("sources") or []
        keep = ("source", "starts", "buyers", "orders", "revenue_rub")
        return {"ok": True, "rows": [{k: r.get(k) for k in keep} for r in rows if isinstance(r, dict)]}

    # ------------------------------------------------------------ панель
    def status_extra(self) -> dict:
        t = self.target()
        c = self.cfg
        return {"publish_enabled": bool(c.get("publish_enabled", False)), "channel": t["chat_id"],
                "bot_username": t["bot_username"], "token_set": bool(t["token"]), "shop": t["shop"],
                "writer": self.writer_label(), "upcoming": seasons.upcoming(self.today(), 45),
                "news_errors": self.kv_get("news_errors"), "shop_orders": self.shop_orders()}

    def writer_label(self) -> str:
        if self.writer_override is not None:
            return self.writer_override.model
        ai = self.center.coordinator.cfg.get("ai") or {}
        if self.cfg.get("ai", True) and not self.center.demo and ai.get("enabled", True) and self.center.coordinator.api_key():
            return f"Claude ({ai.get('model') or 'claude-opus-5-5'})"
        return "шаблоны (нет ключа Claude API у координатора)"

    def settings(self) -> dict:
        c = self.cfg
        keys = ("enabled", "interval_minutes", "publish_enabled", "button", "sources", "keywords", "news_max_age_hours",
                "news_per_run", "news_per_day", "season_lead_days", "seasons_off", "promo_lead_days", "promos",
                "achievements", "share_milestones", "milestones", "chatter_every_days", "allow_prices",
                "forbidden_words", "notify_drafts", "ai", "chat_id", "shop_name")
        return {k: c.get(k) for k in keys}

    def save_settings(self, data: dict) -> None:
        allowed = set(self.settings())
        out: dict = {}
        for k, v in (data or {}).items():
            if k not in allowed:
                raise ValueError(f"неизвестная настройка {k}")
            if k in ("sources", "keywords", "seasons_off", "forbidden_words", "achievements", "milestones"):
                if isinstance(v, str):
                    v = [x.strip() for x in v.splitlines() if x.strip()]
                if not isinstance(v, list):
                    raise ValueError(f"{k}: нужен список")
                if k == "milestones":
                    v = [int(x) for x in v if str(x).strip().isdigit()]
            elif k == "promos":
                if not isinstance(v, list) or not all(isinstance(p, dict) for p in v):
                    raise ValueError("promos: нужен список акций")
                for p in v:
                    for dk in ("start", "end"):
                        if p.get(dk) and parse_date(p[dk]) is None:
                            raise ValueError(f"акция {p.get('title') or p.get('id')}: дата {dk} в формате ГГГГ-ММ-ДД")
                    if not p.get("id"):
                        p["id"] = _slug(str(p.get("title") or p.get("code") or time.time_ns()))
            elif k in ("enabled", "publish_enabled", "button", "share_milestones", "allow_prices", "notify_drafts", "ai"):
                v = bool(v)
            elif k in ("interval_minutes", "news_max_age_hours", "news_per_run", "news_per_day", "season_lead_days",
                       "promo_lead_days", "chatter_every_days"):
                try:
                    v = float(v)
                except (TypeError, ValueError) as e:
                    raise ValueError(f"{k}: нужно число") from e
                if v < 0:
                    raise ValueError(f"{k}: не меньше 0")
                v = int(v) if v == int(v) else v
            else:
                v = str(v or "").strip()
            out[k] = v
        if "interval_minutes" in out and out["interval_minutes"] < 5:
            raise ValueError("interval_minutes: не чаще раза в 5 минут")
        self.save_cfg(out)
        # список акций и порогов заменяется целиком (save сливает словари, а списки и так заменяются)


def _public_promo(p: dict) -> dict:
    """В текст идут только поля для покупателей."""
    return {k: p[k] for k in ("title", "text", "code", "discount_pct") if p.get(k) not in (None, "")}


def _slug(text: str) -> str:
    s = re.sub(r"[^0-9a-zа-яё]+", "-", text.lower()).strip("-")
    return s[:60] or str(abs(hash(text)))


def _tier(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def preview_html(text: str) -> str:
    """Для журнала: текст поста без разметки."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))
