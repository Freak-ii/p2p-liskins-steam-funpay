"""Мониторинг центра: уведомления в Telegram, история состояния ботов, сторожевой пинг наружу,
подтверждение тревог и ежедневный отчёт.

Как это устроено:
- Monitor подписан на EventStore: падения воркеров, «нет зависимостей», «бот не отвечает» и любые
  critical-события превращаются в уведомления. Новые тревоги самих ботов (их таблицы alerts, только
  чтение) пересылаются по уровням из monitor.bot_alert_levels.
- Уведомления проходят дедупликацию (одинаковый ключ не чаще dedup_minutes) и тихие часы
  (некритичное копится и уходит одним сообщением, когда тихие часы кончаются). Каждое уведомление
  записывается в таблицу notifications — его видно в панели, даже если Telegram не настроен.
- Раз в sample_seconds центр проверяет каждого бота (тот же пинг, что и при запуске) и пишет
  в таблицу health состояние и время ответа. Отсюда аптайм и график в панели.
- Сторожевой пинг (heartbeat.url) — внешний сервис вроде healthchecks.io: если пинги прекратились,
  значит упал сам центр или компьютер, и сервис сообщит об этом своими средствами.
- Если прошлый запуск центра закончился без «Центр остановлен», при старте приходит предупреждение.

Настройки — раздел monitor в center.yaml (см. center/config.py), их же меняет панель центра.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import shutil
import time
from collections import defaultdict
from pathlib import Path

import aiohttp
from aiohttp import web

from center.analytics import item_type
from center.workers import BACKOFF, CRASHED, DISABLED, RUNNING, STATE_TEXT

log = logging.getLogger("center.monitor")

SCHEMA = """
CREATE TABLE IF NOT EXISTS health (
    ts REAL NOT NULL,
    bot TEXT NOT NULL,
    state TEXT NOT NULL,         -- состояние воркера (running, backoff, crashed, ...)
    want INTEGER NOT NULL,       -- центр хочет, чтобы бот работал (не остановлен вручную и не выключен)
    ok INTEGER,                  -- 1 ответил, 0 не ответил, NULL — проверка не делалась (процесс не запущен)
    latency_ms REAL,
    restarts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_health_ts ON health(ts);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    key TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL         -- sent | failed | no_channel | held | duplicate
);
CREATE TABLE IF NOT EXISTS monitor_kv (k TEXT PRIMARY KEY, v TEXT);
"""

# события журнала центра, о которых сообщаем всегда (плюс любые critical)
NOTIFY_KINDS = {"worker.exit", "worker.crashed", "worker.spawn_failed", "worker.unhealthy", "worker.missing_deps",
                "worker.not_configured", "monitor.unresponsive", "monitor.recovered", "center.unclean_stop",
                "backup.failed", "backup.restored", "inventory.low_stock",
                "inventory.low_balance", "monitor.low_disk", "monitor.auto_restart", "center.config",
                "monitor.error_spike", "center.update"}
ICONS = {"critical": "🔴", "error": "🔴", "warn": "🟠", "warning": "🟠", "info": "🟢", "ok": "🟢"}
BOT_ALERT_SCAN = 100
# строки вывода ботов, которые считаются ошибками: уровни логгеров LisBot и MannBot, трейсбеки Python и Node
ERROR_LINE = re.compile(r"\b(ERROR|CRIT|CRITICAL|FATAL)\b|Traceback \(most recent call last\)|Unhandled(Promise)?Rejection")
_NOISE = re.compile(r"^\S*\d{4}-\d\d-\d\d[T ][\d:.,Z]+\s*|\d+")


def error_signature(text: str) -> str:
    """Ошибка без времени и чисел: одинаковые ошибки с разными id и суммами считаются одной."""
    s = _NOISE.sub(lambda m: "" if m.start() == 0 and not m.group(0).isdigit() else "#", text.strip())
    return re.sub(r"\s+", " ", s)[:160]


def parse_hhmm(s: str) -> int | None:
    try:
        h, m = str(s).strip().split(":")
        return int(h) % 24 * 60 + int(m) % 60
    except (ValueError, AttributeError):
        return None


def in_quiet_hours(spec: str, now: float) -> bool:
    """spec "23:00-08:00" (через полночь можно). Пустая строка — тихих часов нет."""
    if not spec or "-" not in spec:
        return False
    a, b = (parse_hhmm(x) for x in spec.split("-", 1))
    if a is None or b is None or a == b:
        return False
    lt = time.localtime(now)
    cur = lt.tm_hour * 60 + lt.tm_min
    return a <= cur < b if a < b else cur >= a or cur < b


def _num(v) -> str:
    if isinstance(v, float):
        v = round(v, 2)
        if v.is_integer():
            v = int(v)
    return f"{v:,}".replace(",", " ") if isinstance(v, (int, float)) else str(v)


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


class Monitor:
    def __init__(self, center, clock=time.time):
        self.center = center
        self.events = center.events
        self.clock = clock
        self.db = self.events.db
        self._lock = self.events._lock
        with self._lock:
            self.db.executescript(SCHEMA)
            self.db.commit()
        self.unclean = self._detect_unclean_stop()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: list[asyncio.Task] = []
        self._queue: asyncio.Queue | None = None
        self._last_sent: dict[str, float] = {}
        self._suppressed: dict[str, int] = defaultdict(int)
        self._held: list[tuple[str, str]] = []
        self._fails: dict[str, int] = defaultdict(int)
        self._reported_down: set[str] = set()
        self.heartbeat_state: dict = {"at": None, "ok": None, "info": "не настроен"}
        self.events.listeners.append(self._on_event)

    # ------------------------------------------------------------------ настройки
    @property
    def cfg(self) -> dict:
        return self.center.cfg["monitor"]

    def telegram_target(self) -> tuple[str, list[str], str]:
        """(токен, chat id, откуда взяты). Если в центре не задано — берём из подключений LisBot."""
        tg = self.cfg.get("telegram") or {}
        if not tg.get("enabled", True):
            return "", [], "выключен"
        token, chats, src = self._token(), str(tg.get("chat_id") or ""), "центр"
        if not (token and chats):
            lis = self.center.workers.get("lisbot")
            try:
                s = lis.secrets() if lis is not None and hasattr(lis, "secrets") else {}
            except Exception:  # noqa: BLE001 — хранилище не расшифровано
                s = {}
            token, chats, src = token or s.get("TELEGRAM_BOT_TOKEN", ""), chats or s.get("TELEGRAM_CHAT_ID", ""), "LisBot"
        ids = [c.strip() for c in chats.split(",") if c.strip()]
        return (token, ids, src) if token and ids else ("", [], "не настроен")

    def _token(self) -> str:
        """Токен Telegram центра: в зашифрованном хранилище (раньше лежал открытым текстом в center.yaml)."""
        tg = self.cfg.get("telegram") or {}
        vault = getattr(self.center, "vault", None)
        if vault is None:
            return str(tg.get("token") or "")
        if tg.get("token"):                                # перенос старого значения из center.yaml
            vault.set("center.monitor.telegram_token", str(tg["token"]))
            self.center.cfg.save({"monitor": {"telegram": {"token": ""}}})
        try:
            return vault.get("center.monitor.telegram_token")
        except Exception:  # noqa: BLE001
            return ""

    def settings(self) -> dict:
        c = self.cfg
        tg = c.get("telegram") or {}
        token, ids, src = self.telegram_target()
        return {
            "telegram_enabled": bool(tg.get("enabled", True)), "telegram_token": "",
            "telegram_token_set": bool(self._token()), "telegram_chat_id": str(tg.get("chat_id") or ""),
            "telegram_source": src, "telegram_commands": bool(tg.get("commands", True)),
            "telegram_commands_state": getattr(getattr(self.center, "tg", None), "state", {}), "dedup_minutes": c.get("dedup_minutes"), "quiet_hours": c.get("quiet_hours") or "",
            "quiet_allow_critical": bool(c.get("quiet_allow_critical", True)),
            "bot_alert_levels": list(c.get("bot_alert_levels") or []),
            "heartbeat_url": (c.get("heartbeat") or {}).get("url") or "",
            "heartbeat_seconds": (c.get("heartbeat") or {}).get("seconds"),
            "fail_when_bot_down": bool((c.get("heartbeat") or {}).get("fail_when_bot_down")),
            "daily_report_enabled": bool((c.get("daily_report") or {}).get("enabled", True)),
            "daily_report_time": (c.get("daily_report") or {}).get("time") or "09:00",
            "sample_seconds": c.get("sample_seconds"), "unresponsive_after": c.get("unresponsive_after"),
        }

    def save_settings(self, d: dict) -> None:
        tg: dict = {"enabled": bool(d.get("telegram_enabled", True)), "chat_id": str(d.get("telegram_chat_id") or "").strip(),
                    "commands": bool(d.get("telegram_commands", True))}
        vault = getattr(self.center, "vault", None)
        new_token = None
        if d.get("telegram_token"):                       # пустое поле = «не менять сохранённый токен»
            new_token = str(d["telegram_token"]).strip()
        if d.get("telegram_token_clear"):
            new_token = ""
        if new_token is not None:
            if vault is not None:
                vault.set("center.monitor.telegram_token", new_token or None)
                tg["token"] = ""
            else:
                tg["token"] = new_token
        quiet = str(d.get("quiet_hours") or "").strip()
        if quiet and not all(parse_hhmm(x) is not None for x in quiet.split("-", 1)) or (quiet and "-" not in quiet):
            raise ValueError("тихие часы пишутся так: 23:00-08:00")
        if parse_hhmm(d.get("daily_report_time") or "09:00") is None:
            raise ValueError("время отчёта пишется так: 09:00")
        levels = d.get("bot_alert_levels")
        if isinstance(levels, str):
            levels = [x.strip().lower() for x in levels.split(",") if x.strip()]
        self.center.cfg.save({"monitor": {
            "telegram": tg, "dedup_minutes": max(0, float(d.get("dedup_minutes") or 0)), "quiet_hours": quiet,
            "quiet_allow_critical": bool(d.get("quiet_allow_critical", True)),
            "bot_alert_levels": levels if levels is not None else self.cfg.get("bot_alert_levels"),
            "heartbeat": {"url": str(d.get("heartbeat_url") or "").strip(),
                          "seconds": max(10, int(d.get("heartbeat_seconds") or 60)),
                          "fail_when_bot_down": bool(d.get("fail_when_bot_down"))},
            "daily_report": {"enabled": bool(d.get("daily_report_enabled", True)),
                             "time": str(d.get("daily_report_time") or "09:00").strip()},
        }})

    # ------------------------------------------------------------------ хранилище
    def kv_get(self, k: str, default: str | None = None) -> str | None:
        with self._lock:
            r = self.db.execute("SELECT v FROM monitor_kv WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def kv_set(self, k: str, v) -> None:
        with self._lock:
            self.db.execute("INSERT INTO monitor_kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
            self.db.commit()

    def _detect_unclean_stop(self) -> float | None:
        """Вызывается до записи «Центр запущен»: если последним было «запущен», центр выключился аварийно."""
        with self._lock:
            r = self.db.execute("SELECT kind, ts FROM events WHERE source='center' AND kind IN ('center.start','center.stop') "
                                "ORDER BY id DESC LIMIT 1").fetchone()
        if not r or r[0] != "center.start":
            return None
        last = self.kv_get("last_alive")
        return float(last) if last else float(r[1])

    # ------------------------------------------------------------------ жизненный цикл
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        if self.unclean:
            self.events.add("center", "warn", "center.unclean_stop",
                            "Прошлый запуск центра завершился аварийно (выключение компьютера или сбой). "
                            f"Последний признак жизни: {time.strftime('%d.%m %H:%M', time.localtime(self.unclean))}")
        self._init_alert_marks()
        self._tasks = [asyncio.create_task(f(), name=f"monitor-{f.__name__}") for f in
                       (self._dispatch_loop, self._sample_loop, self._heartbeat_loop, self._report_loop, self._alerts_loop)]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._on_event in self.events.listeners:
            self.events.listeners.remove(self._on_event)

    # ------------------------------------------------------------------ уведомления
    def _on_event(self, ev: dict) -> None:
        if not (ev["kind"] in NOTIFY_KINDS or ev["level"] == "critical"):
            return
        if ev["kind"].startswith("coordinator."):      # координатор шлёт свои уведомления сам — без дубля
            return
        title = self.center.workers[ev["source"]].title if ev["source"] in self.center.workers else "Центр"
        item = (f"{ev['source']}:{ev['kind']}", ev["level"], f"<b>{html.escape(title)}</b>\n{html.escape(ev['message'])}")
        if self._loop is None or self._queue is None:
            return
        try:
            if asyncio.get_running_loop() is self._loop:
                self._queue.put_nowait(item)
                return
        except RuntimeError:
            pass
        self._loop.call_soon_threadsafe(self._queue.put_nowait, item)

    def _record(self, level: str, key: str, text: str, status: str) -> None:
        with self._lock:
            self.db.execute("INSERT INTO notifications(ts, level, key, text, status) VALUES(?,?,?,?,?)",
                            (self.clock(), level, key, text, status))
            self.db.execute("DELETE FROM notifications WHERE id <= (SELECT MAX(id) FROM notifications) - 2000")
            self.db.commit()

    def notifications(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,))]

    async def send_telegram(self, text: str, buttons: dict | None = None) -> tuple[bool, str]:
        token, ids, _ = self.telegram_target()
        if not token:
            return False, "no_channel"
        ok_all, info = True, "sent"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                for chat in ids:
                    payload = {"chat_id": chat, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
                    if buttons:
                        payload["reply_markup"] = buttons
                    async with s.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload) as r:
                        if r.status != 200:
                            ok_all, info = False, f"failed: HTTP {r.status} {(await r.text())[:200]}"
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            return False, f"failed: {e}"
        return ok_all, info

    async def notify(self, key: str, level: str, text: str, *, force: bool = False, buttons: dict | None = None) -> str:
        """Уведомить с дедупликацией и тихими часами. Возвращает статус записи."""
        now = self.clock()
        level = level.lower()
        if not force:
            window = float(self.cfg.get("dedup_minutes") or 0) * 60
            if window and now - self._last_sent.get(key, 0) < window:
                self._suppressed[key] += 1
                self._record(level, key, text, "duplicate")
                return "duplicate"
            if in_quiet_hours(self.cfg.get("quiet_hours") or "", now) and not (
                    level == "critical" and self.cfg.get("quiet_allow_critical", True)):
                self._held.append((level, text))
                self._last_sent[key] = now
                self._record(level, key, text, "held")
                return "held"
        self._last_sent[key] = now
        extra = self._suppressed.pop(key, 0)
        body = f"{ICONS.get(level, '')} {text}" + (f"\n<i>(ещё {extra} похожих скрыто)</i>" if extra else "")
        ok, info = await (self.send_telegram(body, buttons) if buttons else self.send_telegram(body))
        status = "sent" if ok else ("no_channel" if info == "no_channel" else "failed")
        if status == "failed":
            log.warning("Telegram: %s", info)
        self._record(level, key, text, status)
        return status

    async def _flush_held(self) -> None:
        if not self._held or in_quiet_hours(self.cfg.get("quiet_hours") or "", self.clock()):
            return
        held, self._held = self._held, []
        lines = [f"{ICONS.get(lv, '')} {t}" for lv, t in held[-30:]]
        more = f"\n…и ещё {len(held) - 30}" if len(held) > 30 else ""
        await self.notify("monitor:quiet_digest", "info", f"<b>За тихие часы ({len(held)})</b>\n\n" + "\n\n".join(lines) + more,
                          force=True)

    async def _dispatch_loop(self) -> None:
        assert self._queue is not None
        while True:
            try:
                key, level, text = await asyncio.wait_for(self._queue.get(), 30)
                await self.notify(key, level, text)
            except asyncio.TimeoutError:
                pass
            except Exception:  # noqa: BLE001
                log.exception("уведомление не отправлено")
            await self._flush_held()

    # ------------------------------------------------------------------ тревоги ботов
    def _init_alert_marks(self) -> None:
        """При первом запуске мониторинга старые тревоги не пересылаем — только новые."""
        for name, w in self.center.workers.items():
            if self.kv_get(f"alert_mark:{name}") is None:
                ids = [a["id"] for a in self._bot_alerts(w)]
                self.kv_set(f"alert_mark:{name}", max(ids, default=0))

    @staticmethod
    def _bot_alerts(w) -> list[dict]:
        try:
            return w.alerts(BOT_ALERT_SCAN)
        except Exception:  # noqa: BLE001
            return []

    async def check_bot_alerts(self) -> int:
        levels = {str(x).lower() for x in (self.cfg.get("bot_alert_levels") or [])}
        sent = 0
        for name, w in self.center.workers.items():
            mark = int(self.kv_get(f"alert_mark:{name}", "0") or 0)
            alerts = self._bot_alerts(w)
            top = max((int(a["id"]) for a in alerts), default=mark)
            if top < mark:                                 # база бота пересоздана — начинаем отсчёт заново
                self.kv_set(f"alert_mark:{name}", top)
                continue
            new = sorted((a for a in alerts if int(a["id"]) > mark), key=lambda a: a["id"])
            if not new:
                continue
            self.kv_set(f"alert_mark:{name}", top)
            for a in new:
                if a["level"] in levels and not a["acked"]:
                    await self.notify(f"{name}:alert:{a.get('code')}", a["level"],
                                      f"<b>{html.escape(w.title)}</b> · тревога <code>{html.escape(str(a.get('code') or ''))}</code>\n"
                                      f"{html.escape(str(a['message'] or ''))}")
                    sent += 1
        return sent

    async def _alerts_loop(self) -> None:
        while True:
            try:
                await self.check_bot_alerts()
            except Exception:  # noqa: BLE001
                log.exception("проверка тревог ботов")
            await asyncio.sleep(15)

    async def ack(self, source: str, alert_id: int | None) -> tuple[bool, str]:
        w = self.center.workers.get(source)
        if w is None:
            return False, "нет такого бота"
        if w.state != RUNNING:
            return False, f"{w.title} не запущен: подтвердить тревогу можно, когда бот работает"
        try:
            ok = await w.ack_alert(alert_id)
        except Exception as e:  # noqa: BLE001
            return False, f"бот не ответил: {e}"
        what = "все тревоги" if alert_id is None else f"тревога #{alert_id}"
        if ok:
            self.events.add(source, "info", "alert.ack", f"{w.title}: {what} подтверждена из центра")
        return ok, (f"{what} подтверждена" if ok else "бот отказал в подтверждении")

    # ------------------------------------------------------------------ история состояния
    async def sample(self) -> None:
        now = self.clock()
        rows = []
        for name, w in self.center.workers.items():
            want = bool(getattr(w, "_want_running", False)) and w.state != DISABLED
            ok, latency = None, None
            if w.alive and w.state == RUNNING:
                t0 = time.perf_counter()
                try:
                    ok = bool(await asyncio.wait_for(w.health(), 10))
                except Exception:  # noqa: BLE001
                    ok = False
                latency = round((time.perf_counter() - t0) * 1000, 1)
                if not ok:
                    latency = None
            rows.append((now, name, w.state, int(want), None if ok is None else int(ok), latency, w.restarts))
            await self._track_responsive(name, w, ok)
        with self._lock:
            self.db.executemany("INSERT INTO health VALUES (?,?,?,?,?,?,?)", rows)
            self.db.execute("DELETE FROM health WHERE ts < ?", (now - float(self.cfg.get("history_days") or 30) * 86400,))
            self.db.commit()
        self.kv_set("last_alive", now)
        try:
            await asyncio.to_thread(self.check_disk)
        except Exception:  # noqa: BLE001
            log.exception("проверка места на диске")
        try:
            self.check_errors()
        except Exception:  # noqa: BLE001
            log.exception("проверка ошибок в выводе ботов")

    # ------------------------------------------------------------------ всплеск ошибок в выводе ботов
    def errors(self, hours: float = 1) -> dict:
        """Ошибки в выводе каждого бота за последние hours часов и самые частые из них."""
        since = self.clock() - hours * 3600
        out = {}
        for name, w in self.center.workers.items():
            by: dict[str, list] = {}
            for ln in list(getattr(w, "lines", ())):
                if ln["ts"] >= since and ln.get("stream") != "center" and ERROR_LINE.search(ln["text"]):
                    by.setdefault(error_signature(ln["text"]), []).append(ln["text"])
            top = sorted(by.items(), key=lambda kv: -len(kv[1]))[:3]
            out[name] = {"n": sum(len(v) for v in by.values()),
                         "top": [{"n": len(v), "text": v[-1][:300]} for _, v in top]}
        return out

    def check_errors(self) -> list[str]:
        """Бот работает, но сыплет ошибками (нет сети, сменился API площадки, кончился лимит запросов) —
        это видно раньше, чем пропадут сделки. Не чаще раза в 6 часов на бота."""
        cfg = self.cfg.get("errors") or {}
        limit = int(cfg.get("per_hour", 20) or 0)
        if not limit:
            return []
        sent = []
        for name, e in self.errors(1).items():
            if e["n"] < limit:
                continue
            last = float(self.kv_get(f"error_spike:{name}") or 0)
            if self.clock() - last < 6 * 3600:
                continue
            self.kv_set(f"error_spike:{name}", self.clock())
            w = self.center.workers[name]
            top = e["top"][0]
            msg = (f"{w.title}: {e['n']} ошибок в выводе за последний час. Чаще всего ({top['n']} раз): «{top['text']}». "
                   "Бот работает, но что-то идёт не так: посмотрите «Журнал работы ботов» в панели.")
            self.events.add(name, "warn", "monitor.error_spike", msg)
            sent.append(msg)
        return sent

    # ------------------------------------------------------------------ место на диске
    def disk(self) -> dict:
        """Свободное место там, где лежат базы ботов и центра, и в папке резервных копий (если она отдельно)."""
        cfg = self.cfg.get("disk") or {}
        warn = float(cfg.get("warn_gb", 2) or 0)
        paths = [("данные центра", Path(self.center.home))]
        b = getattr(self.center, "backup", None)
        if b is not None:
            paths.append(("резервные копии", b.dir))
        out, seen = [], set()
        for label, p in paths:
            q = p
            while not q.exists() and q != q.parent:      # папки копий ещё нет — меряем диск, где она появится
                q = q.parent
            try:
                u = shutil.disk_usage(q)
                key = q.stat().st_dev                    # тот же диск — не повторяем
            except OSError:
                continue
            if key in seen:
                continue
            seen.add(key)
            free = u.free / (1 << 30)
            out.append({"label": label, "path": str(p), "free_gb": round(free, 2), "total_gb": round(u.total / (1 << 30), 1),
                        "free_pct": round(u.free / u.total * 100, 1) if u.total else None, "low": bool(warn and free < warn)})
        return {"warn_gb": warn, "disks": out, "low": any(d["low"] for d in out)}

    def check_disk(self) -> str | None:
        """Мало места — базы ботов перестанут записываться. Событие в журнал не чаще раза в сутки."""
        d = self.disk()
        low = [x for x in d["disks"] if x["low"]]
        if not low:
            return None
        today = time.strftime("%Y-%m-%d", time.localtime(self.clock()))
        if self.kv_get("low_disk_date") == today:
            return None
        self.kv_set("low_disk_date", today)
        crit = any(x["free_gb"] < min(0.5, d["warn_gb"] / 2) for x in low)
        msg = ("Мало места на диске: " + "; ".join(f"{x['label']} — свободно {x['free_gb']:.1f} ГБ из {x['total_gb']:.0f}"
                                                  for x in low)
               + ". Когда место кончится, боты не смогут записывать сделки. Освободите место или перенесите резервные копии.")
        self.events.add("center", "critical" if crit else "warn", "monitor.low_disk", msg)
        return msg

    async def _track_responsive(self, name: str, w, ok: bool | None) -> None:
        if ok is False:
            self._fails[name] += 1
            if self._fails[name] == int(self.cfg.get("unresponsive_after") or 3) and name not in self._reported_down:
                self._reported_down.add(name)
                self.events.add(name, "critical", "monitor.unresponsive",
                                f"{w.title} не отвечает {self._fails[name]} проверки подряд (процесс жив)")
            limit = int(self.cfg.get("restart_unresponsive_after") or 0)
            if limit and self._fails[name] >= limit:
                last = float(self.kv_get(f"auto_restart:{name}") or 0)
                if self.clock() - last >= 3600:          # не чаще раза в час: если не помогло — решает человек
                    self.kv_set(f"auto_restart:{name}", self.clock())
                    self._fails[name] = 0
                    self.events.add(name, "critical", "monitor.auto_restart",
                                    f"{w.title} завис: не отвечает {limit} проверок подряд — центр перезапускает его")
                    try:
                        await w.restart("бот завис")
                    except Exception as e:  # noqa: BLE001
                        log.exception("перезапуск зависшего бота")
                        self.events.add(name, "critical", "monitor.auto_restart_failed",
                                        f"{w.title}: перезапустить не удалось: {e}")
            return
        if ok:
            self._fails[name] = 0
            if name in self._reported_down:
                self._reported_down.discard(name)
                self.events.add(name, "info", "monitor.recovered", f"{w.title} снова отвечает")
        else:                                            # процесс не запущен — это уже отражают события воркера
            self._fails[name] = 0
            self._reported_down.discard(name)

    async def _sample_loop(self) -> None:
        while True:
            try:
                await self.sample()
            except Exception:  # noqa: BLE001
                log.exception("проверка состояния ботов")
            await asyncio.sleep(max(5.0, float(self.cfg.get("sample_seconds") or 60)))

    def history(self, hours: float = 24, buckets: int = 96) -> dict:
        now = self.clock()
        since = now - hours * 3600
        step = hours * 3600 / buckets
        with self._lock:
            rows = self.db.execute("SELECT ts, bot, state, want, ok, latency_ms, restarts FROM health WHERE ts >= ? ORDER BY ts",
                                   (since,)).fetchall()
            ev = self.db.execute("SELECT source, kind, COUNT(*) FROM events WHERE ts >= ? AND kind IN "
                                 "('worker.exit','worker.crashed','monitor.unresponsive') GROUP BY source, kind",
                                 (since,)).fetchall()
        counts = {(r[0], r[1]): r[2] for r in ev}
        out = {}
        for name, w in self.center.workers.items():
            bs = [{"t": since + i * step, "n": 0, "up": 0, "lat": [], "states": set()} for i in range(buckets)]
            want_n = up_n = 0
            lats = []
            for r in rows:
                if r["bot"] != name:
                    continue
                i = min(buckets - 1, int((r["ts"] - since) / step))
                b = bs[i]
                b["states"].add(r["state"])
                # простой — это когда бот должен работать, но упал или не отвечает; запуск, ручная остановка
                # и «нужна настройка» в аптайм не входят (их видно по состоянию бота)
                if (r["want"] and r["state"] in (RUNNING, BACKOFF)) or r["state"] == CRASHED:
                    want_n += 1
                    b["n"] += 1
                    if r["ok"] == 1:
                        up_n += 1
                        b["up"] += 1
                if r["latency_ms"] is not None:
                    b["lat"].append(r["latency_ms"])
                    lats.append(r["latency_ms"])
            lats.sort()
            out[name] = {
                "title": w.title,
                "uptime": up_n / want_n if want_n else None,
                "latency_avg": round(sum(lats) / len(lats), 1) if lats else None,
                "latency_p95": lats[min(len(lats) - 1, int(len(lats) * 0.95))] if lats else None,
                "restarts": counts.get((name, "worker.exit"), 0), "crashes": counts.get((name, "worker.crashed"), 0),
                "unresponsive": counts.get((name, "monitor.unresponsive"), 0),
                "buckets": [{"t": b["t"], "avail": (b["up"] / b["n"]) if b["n"] else None,
                             "lat": round(sum(b["lat"]) / len(b["lat"]), 1) if b["lat"] else None,
                             "states": sorted(b["states"])} for b in bs],
            }
        return {"hours": hours, "step": step, "since": since, "now": now, "bots": out}

    # ------------------------------------------------------------------ сторожевой пинг
    async def heartbeat(self) -> None:
        hb = self.cfg.get("heartbeat") or {}
        url = str(hb.get("url") or "").strip()
        if not url:
            self.heartbeat_state = {"at": None, "ok": None, "info": "не настроен"}
            return
        down = [w.title for n, w in self.center.workers.items()
                if self.center.enabled(n) and getattr(w, "_want_running", False) and w.state != RUNNING]
        target = url.rstrip("/") + "/fail" if hb.get("fail_when_bot_down") and down else url
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.get(target) as r:
                    ok = r.status < 400
                    info = f"HTTP {r.status}" + (f" · /fail: не работает {', '.join(down)}" if target != url else "")
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as e:
            ok, info = False, f"ошибка: {e}"
        if not ok and self.heartbeat_state.get("ok") is not False:
            log.warning("сторожевой пинг не прошёл: %s", info)
        self.heartbeat_state = {"at": self.clock(), "ok": ok, "info": info}

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                await self.heartbeat()
            except Exception:  # noqa: BLE001
                log.exception("сторожевой пинг")
            await asyncio.sleep(max(10.0, float((self.cfg.get("heartbeat") or {}).get("seconds") or 60)))

    # ------------------------------------------------------------------ ежедневный отчёт
    def daily_report(self) -> str:
        now = self.clock()
        since = now - 86400
        h = self.history(24)
        errors = self.errors(24)
        lines = [f"<b>📊 Отчёт центра за сутки</b> · {time.strftime('%d.%m.%Y %H:%M', time.localtime(now))}"]
        for name, w in self.center.workers.items():
            b = h["bots"][name]
            alerts = [a for a in self._bot_alerts(w) if a["ts"] >= since]
            by_level: dict[str, int] = defaultdict(int)
            for a in alerts:
                by_level[a["level"]] += 1
            open_n = sum(1 for a in alerts if not a["acked"] and a["level"] not in ("info", "ok"))
            lines.append("")
            lines.append(f"<b>{html.escape(w.title)}</b> — {STATE_TEXT.get(w.state, w.state)}")
            lines.append(f"Аптайм {_pct(b['uptime'])} · перезапусков {b['restarts']} · падений {b['crashes']}"
                         + (f" · не отвечал {b['unresponsive']}" if b["unresponsive"] else "")
                         + (f" · ответ {b['latency_avg']:.0f} мс" if b["latency_avg"] is not None else ""))
            errs = errors.get(name) or {}
            if errs.get("n"):
                lines.append(f"Ошибок в выводе: {errs['n']}, чаще всего: «{html.escape(errs['top'][0]['text'][:120])}» (/errors)")
            if alerts:
                lines.append("Тревог: " + ", ".join(f"{k} {v}" for k, v in sorted(by_level.items()))
                             + (f" (не подтверждено {open_n})" if open_n else ""))
            s = self.center.summaries.get(name)
            if s and s.get("kpi"):
                kpi = [f"{html.escape(str(k))}: {_num(v)}" for k, v in s["kpi"].items() if v is not None]
                if kpi:
                    lines.append(" · ".join(kpi))
                lines.append("Режим: " + ("демо" if s.get("demo") else "тестовый" if s.get("dry_run") else "боевой"))
        with self._lock:
            crit = self.db.execute("SELECT COUNT(*) FROM events WHERE ts >= ? AND level='critical'", (since,)).fetchone()[0]
            sent = self.db.execute("SELECT COUNT(*) FROM notifications WHERE ts >= ? AND status != 'duplicate'",
                                   (since,)).fetchone()[0]
        try:
            low = [x for x in self.disk()["disks"] if x["low"]]
        except Exception:  # noqa: BLE001
            low = []
        if low:
            lines += ["", "⚠ Мало места: " + "; ".join(f"{html.escape(x['label'])} {x['free_gb']:.1f} ГБ" for x in low)]
        inv = getattr(self.center, "inventory", None)
        if inv:
            try:
                stock = inv.report_lines()
            except Exception:  # noqa: BLE001
                stock = []
            if stock:
                lines.append("")
                lines += [html.escape(x) for x in stock]
        lines.append("")
        lines.append(f"Центр работает {self._dur(now - self.center.started_at)} · критических событий {crit} · уведомлений {sent}")
        bk = getattr(getattr(self.center, "backup", None), "last", None)
        if bk:
            lines.append(("Резервная копия " + time.strftime("%d.%m %H:%M", time.localtime(bk["at"]))) if bk.get("ok")
                         else "⚠ Последняя резервная копия не удалась")
        return "\n".join(lines)

    # ------------------------------------------------------------------ недельный отчёт
    async def weekly_report(self) -> str:
        """Неделя против прошлой: сделки, выручка, прибыль по ботам в ₽, лучшие и худшие предметы LisBot,
        аптайм за 7 дней, склад."""
        now = self.clock()
        return await self._compare_report(
            f"<b>📅 Неделя центра</b> · {time.strftime('%d.%m', time.localtime(now - 7 * 86400))}–"
            f"{time.strftime('%d.%m.%Y', time.localtime(now))}",
            now - 7 * 86400, now, now - 14 * 86400, "к прошлой", "недели", now)

    @staticmethod
    def _month_bounds(now: float) -> tuple[float, float, float]:
        """Начало прошлого календарного месяца, начало текущего и начало позапрошлого (местное время)."""
        lt = time.localtime(now)
        y, m = lt.tm_year, lt.tm_mon
        cur = time.mktime((y, m, 1, 0, 0, 0, 0, 0, -1))
        py, pm = (y, m - 1) if m > 1 else (y - 1, 12)
        ppy, ppm = (py, pm - 1) if pm > 1 else (py - 1, 12)
        return time.mktime((py, pm, 1, 0, 0, 0, 0, 0, -1)), cur, time.mktime((ppy, ppm, 1, 0, 0, 0, 0, 0, -1))

    async def monthly_report(self) -> str:
        """Прошлый календарный месяц против позапрошлого и итог по цели месяца."""
        now = self.clock()
        start, end, prev = self._month_bounds(now)
        months = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
                  "ноябрь", "декабрь")
        lt = time.localtime(start)
        return await self._compare_report(f"<b>🗓 Итоги месяца</b> · {months[lt.tm_mon - 1]} {lt.tm_year}",
                                          start, end, prev, "к прошлому месяцу", "месяца", now, month=True)

    async def _compare_report(self, title: str, start: float, end: float, prev_start: float, vs: str, of: str,
                              now: float, *, month: bool = False) -> str:
        a = self.center.analytics
        rate = (await a.usd_rub())["rate"]
        deals = await asyncio.to_thread(a.deals, prev_start)
        rub = lambda v, d: a._conv(v, d["currency"], "RUB", rate) or 0  # noqa: E731

        def stats(sel):
            known = [d for d in sel if d["profit"] is not None]
            return {"n": len(sel), "rev": sum(rub(d["revenue"], d) for d in sel), "profit": sum(rub(d["profit"], d) for d in known)}

        def delta(cur, prev):
            if not prev:
                return ""
            ch = (cur - prev) / abs(prev) * 100
            return f" ({'+' if ch >= 0 else '−'}{abs(ch):.0f}% {vs})"

        cur = [d for d in deals if start <= d["sold_at"] < end]
        prev = [d for d in deals if prev_start <= d["sold_at"] < start]
        lines = [title]
        tw, tp = stats(cur), stats(prev)
        lines.append(f"Сделок {tw['n']}{delta(tw['n'], tp['n'])} · выручка {_num(round(tw['rev']))} ₽"
                     f"{delta(tw['rev'], tp['rev'])}")
        lines.append(f"<b>Прибыль {_num(round(tw['profit']))} ₽</b>{delta(tw['profit'], tp['profit'])}")
        for name, w in self.center.workers.items():
            bw, bp = stats([d for d in cur if d["bot"] == name]), stats([d for d in prev if d["bot"] == name])
            lines.append(f"• {html.escape(w.title)}: сделок {bw['n']}, прибыль {_num(round(bw['profit']))} ₽"
                         f"{delta(bw['profit'], bp['profit'])}")
        items: dict[str, list[float]] = defaultdict(list)
        for d in cur:
            if d["bot"] == "lisbot" and d["profit"] is not None:
                items[d["name"]].append(d["profit"])
        ranked = sorted(((sum(v), len(v), k) for k, v in items.items()), reverse=True)
        if ranked:
            lines += ["", f"<b>LisBot, лучшие предметы {of}</b>"]
            lines += [f"{i + 1}. {html.escape(k)} — {'+' if p >= 0 else '−'}${abs(p):.2f} за {n} шт" for i, (p, n, k) in enumerate(ranked[:3])]
            losers = [(p, n, k) for p, n, k in ranked if p < 0][-3:]
            if losers:
                lines += ["<b>В минус</b>"] + [f"• {html.escape(k)} — −${abs(p):.2f} за {n} шт" for p, n, k in reversed(losers)]
            types: dict[str, list[float]] = defaultdict(list)
            for k, v in items.items():
                types[item_type(k)] += v
            if len(types) > 1:
                tr = sorted(((sum(v), len(v), k) for k, v in types.items()), reverse=True)
                lines.append("По типам: " + " · ".join(f"{k} {'+' if p >= 0 else '−'}${abs(p):.2f} ({n} шт)" for p, n, k in tr))
        target = float((self.center.cfg.data.get("analytics") or {}).get("month_goal_rub") or 0)
        if month:
            if target:
                ok = tw["profit"] >= target
                lines.append(f"{'✅' if ok else '❌'} Цель месяца {_num(round(target))} ₽: "
                             f"{'выполнена' if ok else 'не выполнена'}, {_pct(tw['profit'] / target)}")
        else:
            g = await a.goal(now)
            if g["goal"]:
                lines.append(f"Цель месяца: {_num(round(g['profit']))} из {_num(round(g['goal']))} ₽ ({_pct(g['progress'] / 100)}), "
                             f"прогноз {_num(round(g['forecast']))} ₽")
        days = 30 if month else 7
        h = self.history(24 * days, buckets=30 if month else 28)
        lines += ["", f"Аптайм за {days} дней: " + " · ".join(
            f"{html.escape(self.center.workers[n].title.split(' ·')[0])} {_pct(b['uptime'])}, падений {b['crashes']}"
            for n, b in h["bots"].items())]
        inv = getattr(self.center, "inventory", None)
        if inv:
            try:
                lines += [html.escape(x) for x in inv.report_lines()]
            except Exception:  # noqa: BLE001
                pass
        crm = getattr(self.center, "crm", None)
        if crm:
            try:
                seg = await asyncio.to_thread(crm.segment_lines, now)
                if seg:
                    lines += [""] + [html.escape(x) for x in seg]
            except Exception:  # noqa: BLE001
                log.exception("сегменты покупателей для отчёта")
        return "\n".join(lines)

    async def send_weekly_report(self) -> str:
        text = await self.weekly_report()
        self.events.add("center", "info", "monitor.weekly_report", "Недельный отчёт сформирован")
        return await self.notify("monitor:weekly_report", "info", text, force=True)

    async def send_monthly_report(self) -> str:
        text = await self.monthly_report()
        self.events.add("center", "info", "monitor.monthly_report", "Отчёт за месяц сформирован")
        return await self.notify("monitor:monthly_report", "info", text, force=True)

    def monthly_due(self) -> bool:
        mr = self.cfg.get("monthly_report") or {}
        if not mr.get("enabled", True):
            return False
        at = parse_hhmm(mr.get("time") or "09:00")
        lt = time.localtime(self.clock())
        if at is None or lt.tm_mday != 1 or lt.tm_hour * 60 + lt.tm_min < at:
            return False
        return self.kv_get("monthly_report_month") != time.strftime("%Y-%m", lt)

    def weekly_due(self) -> bool:
        wr = self.cfg.get("weekly_report") or {}
        if not wr.get("enabled", True):
            return False
        at = parse_hhmm(wr.get("time") or "09:00")
        lt = time.localtime(self.clock())
        if at is None or lt.tm_wday != int(wr.get("weekday", 0)) % 7 or lt.tm_hour * 60 + lt.tm_min < at:
            return False
        return self.kv_get("weekly_report_date") != time.strftime("%Y-%m-%d", lt)

    @staticmethod
    def _dur(s: float) -> str:
        return f"{s / 60:.0f} мин" if s < 3600 else f"{s / 3600:.1f} ч" if s < 86400 else f"{s / 86400:.1f} д"

    async def send_daily_report(self) -> str:
        text = self.daily_report()
        self.events.add("center", "info", "monitor.daily_report", "Ежедневный отчёт сформирован")
        return await self.notify("monitor:daily_report", "info", text, force=True)

    async def _report_loop(self) -> None:
        while True:
            try:
                dr = self.cfg.get("daily_report") or {}
                at = parse_hhmm(dr.get("time") or "09:00")
                lt = time.localtime(self.clock())
                today = time.strftime("%Y-%m-%d", lt)
                if dr.get("enabled", True) and at is not None and lt.tm_hour * 60 + lt.tm_min >= at \
                        and self.kv_get("daily_report_date") != today:
                    self.kv_set("daily_report_date", today)
                    # центр запущен уже после времени отчёта — отчёт за сегодня всё равно уходит, но не сразу
                    # после старта (иначе он почти пустой): ждём хотя бы один круг проверок
                    if self.clock() - self.center.started_at < 120:
                        await asyncio.sleep(120)
                    await self.send_daily_report()
                if self.weekly_due():
                    self.kv_set("weekly_report_date", time.strftime("%Y-%m-%d", time.localtime(self.clock())))
                    await self.send_weekly_report()
                today = time.strftime("%Y-%m-%d", time.localtime(self.clock()))
                if self.kv_get("events_pruned") != today:
                    self.kv_set("events_pruned", today)
                    days = float(self.cfg.get("events_days") or 0)
                    if days:
                        n = await asyncio.to_thread(self.events.prune, days, self.clock())
                        if n:
                            log.info("журнал центра: удалено %s записей старше %s дн", n, days)
                if self.monthly_due():
                    self.kv_set("monthly_report_month", time.strftime("%Y-%m", time.localtime(self.clock())))
                    await self.send_monthly_report()
            except Exception:  # noqa: BLE001
                log.exception("ежедневный отчёт")
            await asyncio.sleep(30)

    # ------------------------------------------------------------------ для панели
    def status(self) -> dict:
        token, ids, src = self.telegram_target()
        dr = self.cfg.get("daily_report") or {}
        return {
            "telegram": {"ok": bool(token), "source": src, "chats": len(ids)},
            "heartbeat": self.heartbeat_state,
            "quiet_hours": self.cfg.get("quiet_hours") or "",
            "quiet_now": in_quiet_hours(self.cfg.get("quiet_hours") or "", self.clock()), "held": len(self._held),
            "daily_report": {"enabled": bool(dr.get("enabled", True)), "time": dr.get("time") or "09:00",
                             "last": self.kv_get("daily_report_date")},
            "disk": self.disk(),
            "errors": {k: v["n"] for k, v in self.errors(1).items()},
        }


def routes(center) -> list:
    """Маршруты панели для мониторинга; подключаются в center/web.py."""
    m: Monitor = center.monitor

    async def status(request: web.Request):
        return web.json_response({**m.status(), "notifications": m.notifications(30)})

    async def history(request: web.Request):
        hours = min(24 * 30, max(1.0, float(request.query.get("hours", 24))))
        return web.json_response(m.history(hours))

    async def settings(request: web.Request):
        if request.method == "POST":
            try:
                m.save_settings(await request.json())
            except (ValueError, TypeError) as e:
                return web.json_response({"ok": False, "message": str(e)}, status=400)
            center.events.add("center", "info", "settings", "Настройки мониторинга изменены из панели центра")
            return web.json_response({"ok": True, "message": "Настройки мониторинга сохранены"})
        return web.json_response(m.settings())

    async def test(request: web.Request):
        status_ = await m.notify("monitor:test", "info", "<b>Центр перепродажи</b>\nПроверка уведомлений: всё работает.",
                                 force=True)
        msg = {"sent": "Тестовое уведомление отправлено", "no_channel": "Telegram не настроен: укажите токен и chat id"}
        return web.json_response({"ok": status_ == "sent", "message": msg.get(status_, "Telegram не принял сообщение, "
                                                                                          "подробности в журнале центра")})

    async def report(request: web.Request):
        period = request.query.get("period")
        if period == "month":
            if request.method == "POST":
                st = await m.send_monthly_report()
                return web.json_response({"ok": st == "sent", "message": "Отчёт за месяц отправлен в Telegram" if st == "sent"
                                          else "Отчёт сохранён в уведомлениях центра (Telegram не настроен или недоступен)"})
            return web.json_response({"text": await m.monthly_report()})
        week = period == "week"
        if week:
            if request.method == "POST":
                st = await m.send_weekly_report()
                return web.json_response({"ok": st == "sent", "message": "Отчёт за неделю отправлен в Telegram" if st == "sent"
                                          else "Отчёт сохранён в уведомлениях центра (Telegram не настроен или недоступен)"})
            return web.json_response({"text": await m.weekly_report()})
        if request.method == "POST":
            st = await m.send_daily_report()
            return web.json_response({"ok": st == "sent", "message": "Отчёт отправлен в Telegram" if st == "sent"
                                      else "Отчёт сохранён в уведомлениях центра (Telegram не настроен или недоступен)"})
        return web.json_response({"text": m.daily_report()})

    async def ack(request: web.Request):
        d = await request.json()
        aid = d.get("id")
        ok, msg = await m.ack(str(d.get("source") or ""), None if aid in (None, "", "all") else int(aid))
        return web.json_response({"ok": ok, "message": msg}, status=200 if ok else 409)

    async def ack_all(request: web.Request):
        results = []
        for name, w in center.workers.items():
            if w.state == RUNNING:
                results.append(await m.ack(name, None))
        if not results:
            return web.json_response({"ok": False, "message": "ни один бот не запущен"}, status=409)
        return web.json_response({"ok": all(r[0] for r in results), "message": "; ".join(r[1] for r in results)})

    return [
        web.get("/api/monitor/status", status), web.get("/api/monitor/history", history),
        web.get("/api/monitor/settings", settings), web.post("/api/monitor/settings", settings),
        web.post("/api/monitor/test", test), web.get("/api/monitor/report", report), web.post("/api/monitor/report", report),
        web.post("/api/alerts/ack", ack), web.post("/api/alerts/ack-all", ack_all),
    ]
