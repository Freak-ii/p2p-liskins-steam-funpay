"""Круги Орбиты: как деньги возвращаются в оборот через открытые позиции.

Модель — граф. Узлы — места, где лежат деньги (баланс FunPay, карта, USDT на бирже, кошелёк TON, Lis-Skins,
Steam…). Рёбра двух видов:
  sale      — продажа позиции в канале: тратим деньги одного узла на закупку и получаем выручку в другом
              (звёзды: TON → ₽ на FunPay; ключи Mann: Steam → ₽ на FunPay). Ребро есть, только пока позиция
              и этот канал открыты в Пульте Орбиты;
  transfer  — перевод между узлами (вывод FunPay на карту −3%, P2P карта → USDT, USDT → TON на Bybit…).
              Переводы тоже можно выключать (например, вывод FunPay в USDT).
У каждого ребра оценка в процентах к стоимости: маржа продажи или потеря на переводе. Круг — замкнутый
путь по рёбрам; доходность круга = произведение (1 + p) по его рёбрам − 1. Опорные курсы при этом
сокращаются: в замкнутом пути важно только, сколько теряет или даёт каждое звено.

Что даёт пересчёт при перетасовке позиций:
  * закрыли позицию — её рёбра пропали, круги через неё исчезли, лучший круг остальных позиций пересчитан;
  * канал продажи, через который больше нет ни одного круга, «без круга»: деньги туда войдут, а выйти не смогут
    (пример: закрыли ключи Mann — Steam-кошелёк остаётся без выхода, и закупка скинов под Steam теряет смысл).
    Такие каналы Орбита паркует (orbit.auto_park): они закрываются у ботов, как снятые вручную, и
    возвращаются сами, когда круг снова появится;
  * позиции, у которых лучший круг ниже orbit.min_loop_pct, получают подсказку «в минусе — снять с орбиты?».

id круга (общий с разделом экономики): узлы по порядку, начиная с fp_rub, если он есть в круге, иначе с
первого по алфавиту; после узла, из которого идёт продажа, в скобках позиция@канал. Например
"fp_rub>card_rub>usdt>ton[stars@funpay]>fp_rub".
"""
from __future__ import annotations

from dataclasses import dataclass, field

NODES: dict[str, str] = {
    "fp_rub": "Баланс FunPay, ₽",
    "ggsel_rub": "Баланс GGSEL, ₽",
    "card_rub": "Карта / счёт, ₽",
    "usdt": "USDT на бирже (Bybit)",
    "ton": "Кошелёк TON (хаб и StarsBot)",
    "lis_usd": "Баланс Lis-Skins, $",
    "steam": "Steam-кошелёк и инвентарь",
    "market_rub": "Баланс Market.CSGO, ₽",
    "xtr": "Звёзды бота (XTR)",
}

# Переводы между узлами. pct — сколько теряется (минус) относительно рыночной стоимости.
TRANSFERS: list[dict] = [
    {"id": "fp_rub>card_rub", "from": "fp_rub", "to": "card_rub", "pct": -3.0, "enabled": True,
     "title": "Вывод FunPay на карту (3%)"},
    {"id": "fp_rub>usdt", "from": "fp_rub", "to": "usdt", "pct": -4.3, "enabled": False,
     "title": "Вывод FunPay в USDT TRC20 (6% ≈ 90,7 ₽ за USDT против P2P 86,8)"},
    {"id": "ggsel_rub>card_rub", "from": "ggsel_rub", "to": "card_rub", "pct": -3.0, "enabled": True,
     "title": "Вывод GGSEL на карту (оценка)"},
    {"id": "card_rub>usdt", "from": "card_rub", "to": "usdt", "pct": 0.0, "enabled": True,
     "title": "P2P на Bybit: карта → USDT (опорный курс)"},
    {"id": "usdt>ton", "from": "usdt", "to": "ton", "pct": -0.3, "enabled": True,
     "title": "Bybit: USDT → TON и вывод на хаб"},
    {"id": "ton>usdt", "from": "ton", "to": "usdt", "pct": -0.3, "enabled": True,
     "title": "TON → USDT на бирже"},
    {"id": "usdt>lis_usd", "from": "usdt", "to": "lis_usd", "pct": 0.0, "enabled": True,
     "title": "Пополнение Lis-Skins криптой"},
    {"id": "market_rub>usdt", "from": "market_rub", "to": "usdt", "pct": -2.5, "enabled": True,
     "title": "Вывод Market.CSGO в BTC (1%) и обмен на USDT"},
    {"id": "xtr>ton", "from": "xtr", "to": "ton", "pct": -5.0, "enabled": True,
     "title": "Звёзды бота → TON через Fragment (через 21 день)"},
]


@dataclass
class Edge:
    id: str
    src: str
    dst: str
    kind: str                       # sale | transfer
    pct: float
    title: str
    position: str = ""
    channel: str = ""
    source: str = "оценка"         # откуда взята цифра: оценка | настройка | факт

    @property
    def factor(self) -> float:
        return 1 + self.pct / 100

    def leg(self) -> dict:
        d = {"from": self.src, "to": self.dst, "kind": self.kind, "pct": round(self.pct, 2), "title": self.title,
             "edge": self.id, "source": self.source}
        if self.kind == "sale":
            d.update(position=self.position, channel=self.channel)
        return d


@dataclass
class Circle:
    edges: list[Edge]
    id: str = ""
    pct: float = 0.0
    nodes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        sales = [e for e in self.edges if e.kind == "sale"]
        title = " → ".join(NODES.get(n, n).split(" (")[0].split(",")[0] for n in self.nodes)
        return {"id": self.id, "title": title, "pct": round(self.pct, 2), "open": True,
                "positions": sorted({e.position for e in sales}),
                "sales": [f"{e.position}@{e.channel}" for e in sales],
                "legs": [e.leg() for e in self.edges]}


def circle_id(edges: list[Edge]) -> tuple[str, list[str], list[Edge]]:
    """Канонический id круга и его рёбра, повернутые так, чтобы начинаться с опорного узла."""
    nodes = [e.src for e in edges]
    start = "fp_rub" if "fp_rub" in nodes else min(nodes)
    i = nodes.index(start)
    rot = edges[i:] + edges[:i]
    parts = []
    for e in rot:
        parts.append(e.src + (f"[{e.position}@{e.channel}]" if e.kind == "sale" else ""))
    parts.append(start)
    return ">".join(parts), [e.src for e in rot] + [start], rot


def find_circles(edges: list[Edge], max_len: int = 7, limit: int = 500) -> list[Circle]:
    """Все простые круги (без повтора узлов) длиной до max_len рёбер. Параллельные рёбра (две позиции
    из TON в FunPay) дают разные круги — так у каждой позиции свой круг."""
    order = sorted({e.src for e in edges} | {e.dst for e in edges})
    rank = {n: i for i, n in enumerate(order)}
    out_edges: dict[str, list[Edge]] = {}
    for e in edges:
        out_edges.setdefault(e.src, []).append(e)
    found: list[Circle] = []
    seen: set[str] = set()

    def dfs(start: str, node: str, path: list[Edge], visited: set[str]) -> None:
        if len(found) >= limit:
            return
        for e in out_edges.get(node, []):
            if e.dst == start:
                cid, nodes, rot = circle_id(path + [e])
                if cid not in seen:
                    seen.add(cid)
                    f = 1.0
                    for x in rot:
                        f *= x.factor
                    found.append(Circle(edges=rot, id=cid, pct=(f - 1) * 100, nodes=nodes))
            elif rank[e.dst] > rank[start] and e.dst not in visited and len(path) + 1 < max_len:
                visited.add(e.dst)
                dfs(start, e.dst, path + [e], visited)
                visited.discard(e.dst)

    for s in order:
        dfs(s, s, [], {s})
    found.sort(key=lambda c: -c.pct)
    return found


def evaluate(edges: list[Edge], min_loop_pct: float = 0.0) -> dict:
    """Круги, лучший круг каждого канала продажи и узлы, где деньги застрянут."""
    circles = find_circles(edges)
    # Свой круг канала — тот, где кроме него как можно меньше других продаж (иначе любая позиция выглядит
    # прибыльной за счёт соседа с большой маржой), а среди таких — самый доходный.
    best: dict[str, Circle] = {}
    in_circle: set[str] = set()

    def sales(c: Circle) -> int:
        return sum(1 for e in c.edges if e.kind == "sale")

    for c in circles:
        for e in c.edges:
            in_circle.add(e.id)
            if e.kind != "sale":
                continue
            cur = best.get(e.id)
            if cur is None or (sales(c), -c.pct) < (sales(cur), -cur.pct):
                best[e.id] = c
    sale_edges = [e for e in edges if e.kind == "sale"]
    no_circle = sorted(e.id for e in sale_edges if e.id not in in_circle)
    nodes_in = {e.dst for e in edges}
    nodes_cycled = {n for c in circles for n in c.nodes}
    stranded = sorted(n for n in nodes_in if n not in nodes_cycled)
    losing = sorted(e.id for e in sale_edges if e.id in best and best[e.id].pct < min_loop_pct)
    return {
        "circles": [c.as_dict() for c in circles],
        "best": {k: {"id": c.id, "pct": round(c.pct, 2),
                     "with": [f"{e.position}@{e.channel}" for e in c.edges if e.kind == "sale" and e.id != k]}
                 for k, c in best.items()},
        "no_circle": no_circle,
        "losing": losing,
        "stranded": [{"node": n, "title": NODES.get(n, n)} for n in stranded],
    }
