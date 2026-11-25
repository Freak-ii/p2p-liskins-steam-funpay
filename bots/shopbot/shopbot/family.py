"""Свои семейные подписки (Яндекс Плюс): учёт аккаунтов-хозяев, мест в семьях и сроков.

Как работает Яндекс Плюс (справка Яндекса, 25.11.2026): владелец оплачивает Плюс и делится им с 3 близкими
(в семейной группе Яндекс ID до 7 человек, но Плюс — только на 3). Приглашают ссылкой «Поделиться ссылкой»,
SMS или почтой. Человек состоит только в одной семье и меняет семью не чаще раза в 14 дней. Работает в РФ, СНГ,
Израиле. Если у близкого есть свой Плюс, его списания прекращаются (у части тарифов идут параллельно).

Магазин держит пул СВОИХ аккаунтов с оплаченным Плюсом (купленным кодом/подпиской на год) и продаёт места.
Жёсткая сортировка — куда можно добавлять, куда нельзя (eligible/reasons):
  • аккаунт активен (не пауза, не бан, не выведен);
  • Плюс оплачен на весь срок места + запас (reserve_days) — иначе человек останется без подписки;
  • есть свободное место (seats минус занятые, приглашённые, ждущие исключения и места «на остывании»
    seat_cooldown_days после исключения);
  • не превышен лимит добавлений за 30 дней (max_adds_30d) — частая смена людей похожа на перепродажу;
  • risk ниже max_risk (оператор ставит 1–3 при подозрительных письмах/проверках от Яндекса).
Из подходящих берётся аккаунт с самым коротким достаточным сроком (не тратим длинные на короткие места),
при равенстве — самый заполненный (полные семьи, меньше «дыр»).

Выдача: ссылка-приглашение из пула ссылок аккаунта (оператор заранее нажимает «Поделиться ссылкой», вставляет
их в панель) — тогда всё автоматически; пула нет — задача оператору «пришлите ссылку для аккаунта N».
Покупатель жмёт «Я вступил» → место активно. Конец срока: за 3 дня напоминание с продлением; после — задача
оператору исключить участника, место освобождается после «Исключён». Бан/потеря аккаунта → «переселение»:
каждому участнику — место в другом аккаунте на оставшийся срок.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("family")

DAY = 86400.0
BUSY = ("reserved", "invited", "active", "leaving")

SCHEMA = """
CREATE TABLE IF NOT EXISTS family_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, service TEXT DEFAULT 'yandex_plus', label TEXT, login TEXT DEFAULT '',
    seats INTEGER DEFAULT 3, paid_until REAL DEFAULT 0, cost_rub REAL DEFAULT 0, period_months REAL DEFAULT 12,
    status TEXT DEFAULT 'active', risk INTEGER DEFAULT 0, note TEXT DEFAULT '', created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS family_seats (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER, order_id TEXT, user_id INTEGER, member TEXT DEFAULT '',
    status TEXT, until REAL, invite TEXT DEFAULT '', created_at REAL, joined_at REAL, removed_at REAL,
    reminded INTEGER DEFAULT 0, note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS family_seats_acc ON family_seats(account_id, status);
CREATE INDEX IF NOT EXISTS family_seats_order ON family_seats(order_id);
CREATE TABLE IF NOT EXISTS family_invites (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER, link TEXT, created_at REAL, used_by TEXT DEFAULT ''
);
"""

STATUS_TITLES = {"active": "активен", "paused": "пауза", "banned": "бан / потерян", "retired": "выведен"}


class FamilySource:
    name = "family"

    def __init__(self, cfg, db):
        self.cfg, self.db = cfg, db
        with db.lock:
            db.con.executescript(SCHEMA)
            db.con.commit()

    # ------------------------------------------------------------------ настройки
    def c(self, key: str, default):
        v = (self.cfg.get("services.family") or {}).get(key)
        return default if v is None else v

    # ------------------------------------------------------------------ аккаунты
    def add_account(self, label: str, *, service: str = "yandex_plus", login: str = "", seats: int | None = None,
                    paid_until: float = 0, cost_rub: float = 0, period_months: float = 12, note: str = "") -> int:
        now = time.time()
        seats = int(seats if seats is not None else self.c("seats_per_account", 3))
        cur = self.db.exec("INSERT INTO family_accounts(service,label,login,seats,paid_until,cost_rub,period_months,"
                           "note,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (service, label[:60], login[:80], max(1, min(seats, 7)), float(paid_until),
                            float(cost_rub), float(period_months or 12), note[:200], now, now))
        return int(getattr(cur, "lastrowid", 0) or self.db.scalar("SELECT MAX(id) FROM family_accounts", (), 0))

    def account(self, aid: int):
        return self.db.one("SELECT * FROM family_accounts WHERE id=?", (int(aid),))

    def accounts(self, service: str | None = None) -> list:
        if service:
            return self.db.q("SELECT * FROM family_accounts WHERE service=? ORDER BY paid_until", (service,))
        return self.db.q("SELECT * FROM family_accounts ORDER BY service, paid_until")

    def update_account(self, aid: int, **f) -> None:
        allowed = {"label", "login", "seats", "paid_until", "cost_rub", "period_months", "status", "risk", "note"}
        f = {k: v for k, v in f.items() if k in allowed}
        if not f:
            return
        f["updated_at"] = time.time()
        self.db.exec(f"UPDATE family_accounts SET {','.join(k + '=?' for k in f)} WHERE id=?", [*f.values(), int(aid)])

    def extend(self, aid: int, months: float, cost_rub: float) -> None:
        """Плюс на аккаунте продлён (куплен код/оплачен год): срок от max(сейчас, старый срок)."""
        a = self.account(aid)
        if a is None:
            return
        base = max(time.time(), float(a["paid_until"] or 0))
        self.update_account(aid, paid_until=base + float(months) * 30 * DAY, cost_rub=float(cost_rub),
                            period_months=float(months))

    def add_invites(self, aid: int, links: list[str]) -> int:
        have = {r["link"] for r in self.db.q("SELECT link FROM family_invites WHERE account_id=?", (int(aid),))}
        n = 0
        for link in links:
            link = link.strip()
            if link.startswith("http") and link not in have:
                self.db.exec("INSERT INTO family_invites(account_id,link,created_at) VALUES(?,?,?)",
                             (int(aid), link, time.time()))
                have.add(link)
                n += 1
        return n

    def fresh_invites(self, aid: int) -> list:
        ttl = float(self.c("invite_ttl_hours", 24)) * 3600
        return self.db.q("SELECT * FROM family_invites WHERE account_id=? AND used_by='' AND created_at>? "
                         "ORDER BY id", (int(aid), time.time() - ttl))

    # ------------------------------------------------------------------ места
    def seats_used(self, aid: int) -> int:
        cool = float(self.c("seat_cooldown_days", 1)) * DAY
        busy = int(self.db.scalar(f"SELECT COUNT(*) FROM family_seats WHERE account_id=? AND status IN "
                                  f"({','.join('?' * len(BUSY))})", (int(aid), *BUSY), 0))
        cooling = int(self.db.scalar("SELECT COUNT(*) FROM family_seats WHERE account_id=? AND status='free' AND "
                                     "removed_at>?", (int(aid), time.time() - cool), 0))
        return busy + cooling

    def adds_30d(self, aid: int) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM family_seats WHERE account_id=? AND created_at>?",
                                  (int(aid), time.time() - 30 * DAY), 0))

    def reasons(self, a, months: float) -> list[str]:
        """Почему в этот аккаунт НЕЛЬЗЯ добавить человека на months месяцев (пусто — можно)."""
        out = []
        if a["status"] != "active":
            out.append(STATUS_TITLES.get(a["status"], a["status"]))
        need = time.time() + (float(months) * 30 + float(self.c("reserve_days", 3))) * DAY
        if float(a["paid_until"] or 0) < need:
            till = time.strftime("%d.%m.%Y", time.localtime(float(a["paid_until"] or 0))) if a["paid_until"] else "—"
            out.append(f"Плюс оплачен до {till} — мало для {months:g} мес.")
        free = int(a["seats"]) - self.seats_used(a["id"])
        if free <= 0:
            out.append("мест нет")
        if self.adds_30d(a["id"]) >= int(self.c("max_adds_30d", 8)):
            out.append("лимит добавлений за 30 дней")
        if int(a["risk"] or 0) >= int(self.c("max_risk", 2)):
            out.append(f"риск {a['risk']}")
        return out

    def pick(self, service: str, months: float, exclude: int | None = None):
        ok = [a for a in self.accounts(service) if a["id"] != exclude and not self.reasons(a, months)]
        if not ok:
            return None
        return min(ok, key=lambda a: (float(a["paid_until"]), -self.seats_used(a["id"])))

    def seat_cost(self, a, months: float) -> float:
        """Себестоимость места: цена Плюса аккаунта / (срок × мест) × months, плюс простой мест (vacancy_pct)."""
        per = float(a["cost_rub"] or 0) / max(1.0, float(a["period_months"] or 12)) / max(1, int(a["seats"]))
        return round(per * float(months) * (1 + float(self.c("vacancy_pct", 15)) / 100), 2)

    def quote(self, service: str, months: float) -> dict | None:
        a = self.pick(service, months)
        if a is None:
            return None
        return {"source": "family", "auto": bool(self.fresh_invites(a["id"])), "cost_rub": self.seat_cost(a, months),
                "account": a["id"]}

    def seat(self, order_id: str):
        return self.db.one("SELECT * FROM family_seats WHERE order_id=? ORDER BY id DESC LIMIT 1", (order_id,))

    def reserve(self, service: str, order_id: str, user_id: int, member: str, months: float,
                renew_of: str = "", exclude: int | None = None, until: float = 0) -> dict:
        """Занять место. Продление (renew_of) — то же место, если аккаунт оплачен на новый срок.
        → {ok, account, seat, invite, cost_rub} или {ok: False, error}."""
        now = time.time()
        if renew_of:
            old = self.seat(renew_of)
            if old is not None and old["status"] in ("active", "invited", "leaving"):
                a = self.account(old["account_id"])
                new_until = max(now, float(old["until"])) + float(months) * 30 * DAY
                if a is not None and a["status"] == "active" and \
                        float(a["paid_until"]) >= new_until + float(self.c("reserve_days", 3)) * DAY:
                    self.db.exec("UPDATE family_seats SET until=?, status=CASE WHEN status='leaving' THEN 'active' "
                                 "ELSE status END, reminded=0, note=? WHERE id=?",
                                 (new_until, f"продлено заказом {order_id}", old["id"]))
                    self.db.exec("INSERT INTO family_seats(account_id,order_id,user_id,member,status,until,invite,"
                                 "created_at,joined_at,note) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                 (a["id"], order_id, user_id, old["member"], "renewal", new_until, old["invite"], now,
                                  old["joined_at"], f"продление места #{old['id']}"))
                    return {"ok": True, "account": a["id"], "seat": old["id"], "invite": "", "renewed": True,
                            "until": new_until, "cost_rub": self.seat_cost(a, months)}
            if old is not None:      # в этой семье Плюс кончится раньше — новое место в другой, на весь срок
                member = member or old["member"]
                exclude = exclude or old["account_id"]
                until = until or max(now, float(old["until"])) + float(months) * 30 * DAY
        with self.db.lock:
            a = self.pick(service, max(float(months), ((until or 0) - now) / (30 * DAY)), exclude=exclude)
            if a is None:
                return {"ok": False, "error": "нет свободных мест в семьях"}
            until = until or now + float(months) * 30 * DAY
            cur = self.db.exec("INSERT INTO family_seats(account_id,order_id,user_id,member,status,until,created_at) "
                               "VALUES(?,?,?,?,?,?,?)", (a["id"], order_id, user_id, member[:80], "reserved", until, now))
            sid = int(getattr(cur, "lastrowid", 0) or self.db.scalar("SELECT MAX(id) FROM family_seats", (), 0))
            inv = self.fresh_invites(a["id"])
            link = ""
            if inv:
                link = inv[0]["link"]
                self.db.exec("UPDATE family_invites SET used_by=? WHERE id=?", (order_id, inv[0]["id"]))
                self.db.exec("UPDATE family_seats SET status='invited', invite=? WHERE id=?", (link, sid))
        return {"ok": True, "account": a["id"], "seat": sid, "invite": link, "until": until,
                "cost_rub": self.seat_cost(a, months)}

    def set_invite(self, order_id: str, link: str) -> None:
        s = self.seat(order_id)
        if s is not None and s["status"] in ("reserved", "invited"):
            self.db.exec("UPDATE family_seats SET status='invited', invite=? WHERE id=?", (link.strip(), s["id"]))

    def joined(self, order_id: str) -> bool:
        s = self.seat(order_id)
        if s is None or s["status"] not in ("invited", "reserved"):
            return False
        self.db.exec("UPDATE family_seats SET status='active', joined_at=? WHERE id=?", (time.time(), s["id"]))
        return True

    def removed(self, seat_id: int) -> bool:
        """Оператор исключил участника из семьи — место освобождается (после остывания)."""
        s = self.db.one("SELECT * FROM family_seats WHERE id=?", (int(seat_id),))
        if s is None or s["status"] not in ("leaving", "active", "invited", "reserved", "moved"):
            return False
        self.db.exec("UPDATE family_seats SET status='free', removed_at=? WHERE id=?", (time.time(), s["id"]))
        return True

    def release(self, order_id: str) -> None:
        """Возврат денег до вступления — место снова свободно сразу (без остывания)."""
        s = self.seat(order_id)
        if s is not None and s["status"] in ("reserved", "invited"):
            self.db.exec("UPDATE family_seats SET status='free', removed_at=0 WHERE id=?", (s["id"],))

    def expiring(self, days: float = 3) -> list:
        now = time.time()
        return self.db.q("SELECT s.*, a.label, a.service FROM family_seats s JOIN family_accounts a ON "
                         "a.id=s.account_id WHERE s.status='active' AND s.reminded=0 AND s.until>? AND s.until<?",
                         (now, now + days * DAY))

    def expired(self) -> list:
        return self.db.q("SELECT s.*, a.label, a.login FROM family_seats s JOIN family_accounts a ON "
                         "a.id=s.account_id WHERE s.status IN ('active','invited','reserved') AND s.until<?",
                         (time.time(),))

    def mark(self, seat_id: int, **f) -> None:
        self.db.exec(f"UPDATE family_seats SET {','.join(k + '=?' for k in f)} WHERE id=?", [*f.values(), int(seat_id)])

    def unconfirmed(self) -> list:
        """Приглашение отправлено, но «Я вступил» не нажато дольше invite_ttl_hours — напомнить один раз."""
        ttl = float(self.c("invite_ttl_hours", 24)) * 3600 / 2
        return self.db.q("SELECT * FROM family_seats WHERE status='invited' AND reminded=0 AND created_at<?",
                         (time.time() - ttl,))

    def account_alerts(self) -> list[str]:
        out = []
        now = time.time()
        for a in self.accounts():
            if a["status"] != "active":
                continue
            late = int(self.db.scalar("SELECT COUNT(*) FROM family_seats WHERE account_id=? AND status IN "
                                      "('active','invited','reserved') AND until>?", (a["id"], float(a["paid_until"])), 0))
            if late:
                out.append(f"аккаунт «{a['label']}»: {late} участн. дольше, чем оплачен Плюс — продлите")
            elif float(a["paid_until"]) < now + 7 * DAY and self.seats_used(a["id"]):
                out.append(f"аккаунт «{a['label']}»: Плюс кончается "
                           f"{time.strftime('%d.%m', time.localtime(float(a['paid_until'])))}")
        return out

    def to_migrate(self, aid: int) -> list:
        return self.db.q("SELECT * FROM family_seats WHERE account_id=? AND status IN ('active','invited','reserved') "
                         "AND until>?", (int(aid), time.time()))

    # ------------------------------------------------------------------ сводка для панели
    def summary(self, service: str = "yandex_plus", months: float = 1) -> dict:
        rows = []
        free_total = 0
        for a in self.accounts(service):
            used = self.seats_used(a["id"])
            why = self.reasons(a, months)
            free = max(0, int(a["seats"]) - used) if a["status"] == "active" else 0
            free_total += free if not why else 0
            sold = float(self.db.scalar("SELECT COALESCE(SUM(o.price_rub - o.discount_rub),0) FROM family_seats s "
                                        "JOIN orders o ON o.id=s.order_id WHERE s.account_id=? AND o.status='delivered'",
                                        (a["id"],), 0) or 0)
            rows.append({**dict(a), "used": used, "free": free, "why": why, "adds_30d": self.adds_30d(a["id"]),
                         "invites": len(self.fresh_invites(a["id"])), "sold_rub": round(sold, 2),
                         "status_title": STATUS_TITLES.get(a["status"], a["status"]),
                         "paid_till": time.strftime("%d.%m.%Y", time.localtime(float(a["paid_until"])))
                         if a["paid_until"] else "—",
                         "members": [dict(s) for s in self.db.q(
                             "SELECT * FROM family_seats WHERE account_id=? AND status IN "
                             "('reserved','invited','active','leaving') ORDER BY until", (a["id"],))]})
        return {"accounts": rows, "free": free_total, "alerts": self.account_alerts(),
                "leaving": [dict(s) for s in self.db.q(
                    "SELECT s.*, a.label FROM family_seats s JOIN family_accounts a ON a.id=s.account_id "
                    "WHERE s.status='leaving' ORDER BY s.until")]}
