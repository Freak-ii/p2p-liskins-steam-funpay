"""Коллекционные (улучшенные, NFT) подарки Telegram: справочник коллекций для TG Gift.

slug — как в адресе fragment.com/gifts/<slug>; en — название в Telegram и на Fragment (по нему же лоты ищут
покупатели и конкуренты на FunPay); ru — короткое русское название для карточки; fp_type — вариант «Тип подарка»
в форме раздела FunPay «Подарки» (funpay.com/lots/3064/, список прочитан 2026-11-21); season — когда подарок
продаётся лучше обычного (автоподбор ассортимента поднимает его в рейтинге).

Новые коллекции Telegram выпускает регулярно: бот берёт их из каталога Telegram (title) и с fragment.com/gifts
сам, этот список только задаёт русские названия, тип FunPay и сезон для известных.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

WINTER, VALENTINE, SPRING, SUMMER, HALLOWEEN = "winter", "valentine", "spring", "summer", "halloween"
SEASON_TITLES = {WINTER: "Новый год", VALENTINE: "14 февраля", SPRING: "8 марта и Пасха", SUMMER: "лето",
                 HALLOWEEN: "Хэллоуин"}
# окна сезонов: (месяц, день) начала и конца, включительно; Новый год переходит через 1 января
SEASON_WINDOWS = {WINTER: ((11, 15), (1, 15)), VALENTINE: ((1, 25), (2, 15)), SPRING: ((2, 25), (4, 30)),
                  SUMMER: ((6, 1), (8, 31)), HALLOWEEN: ((10, 1), (11, 2))}


@dataclass(frozen=True)
class Collection:
    slug: str
    en: str
    ru: str
    fp_type: str = "Прочее"
    season: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)

    @property
    def title(self) -> str:
        return f"{self.en} ({self.ru})" if self.ru and self.ru.lower() != self.en.lower() else self.en


_RAW = [
    ("algorithmcup", "Algorithm Cup", "Кубок алгоритмов", "Кубок", ""),
    ("artisanbrick", "Artisan Brick", "Кирпич", "Кирпич", ""),
    ("astralshard", "Astral Shard", "Астральный кристалл", "Кристалл", ""),
    ("bdaycandle", "B-Day Candle", "Свеча на торт", "Свеча", ""),
    ("berrybox", "Berry Box", "Коробка ягод", "Клубника", ""),
    ("bigyear", "Big Year", "Удачный год", "Удачный год", WINTER),
    ("blingbinky", "Bling Binky", "Соска", "Прочее", ""),
    ("bondedring", "Bonded Ring", "Связанное кольцо", "Кольцо", VALENTINE),
    ("bowtie", "Bow Tie", "Галстук-бабочка", "Галстук", ""),
    ("bunnymuffin", "Bunny Muffin", "Кекс-кролик", "Маффин", SPRING),
    ("candycane", "Candy Cane", "Леденцовая трость", "Леденец", WINTER),
    ("chillflame", "Chill Flame", "Холодное пламя", "Факел", ""),
    ("cloverpin", "Clover Pin", "Клевер", "Клевер", SPRING),
    ("cookieheart", "Cookie Heart", "Печенье-сердце", "Печенье", VALENTINE),
    ("crystalball", "Crystal Ball", "Хрустальный шар", "Шар", ""),
    ("cupidcharm", "Cupid Charm", "Талисман Купидона", "Медальон", VALENTINE),
    ("deskcalendar", "Desk Calendar", "Календарь", "Календарь", WINTER),
    ("diamondring", "Diamond Ring", "Кольцо с бриллиантом", "Кольцо", VALENTINE),
    ("durovscap", "Durov's Cap", "Кепка Дурова", "Кепка", ""),
    ("durovsglasses", "Durov's Glasses", "Очки Дурова", "Прочее", ""),
    ("easteregg", "Easter Egg", "Пасхальное яйцо", "Прочее", SPRING),
    ("electricskull", "Electric Skull", "Электрический череп", "Череп", HALLOWEEN),
    ("eternalcandle", "Eternal Candle", "Вечная свеча", "Свеча", ""),
    ("eternalrose", "Eternal Rose", "Вечная роза", "Роза", VALENTINE),
    ("evileye", "Evil Eye", "Сглаз", "Глаз", HALLOWEEN),
    ("faithamulet", "Faith Amulet", "Амулет", "Амулет", ""),
    ("finepen", "Fine Pen", "Перьевая ручка", "Прочее", ""),
    ("flyingbroom", "Flying Broom", "Метла", "Метла", HALLOWEEN),
    ("freshsocks", "Fresh Socks", "Носки", "Носок", ""),
    ("gemsignet", "Gem Signet", "Драгоценный перстень", "Кольцо", ""),
    ("genielamp", "Genie Lamp", "Лампа джинна", "Лампа", ""),
    ("gingercookie", "Ginger Cookie", "Имбирное печенье", "Печенье", WINTER),
    ("hangingstar", "Hanging Star", "Звезда", "Звезда", WINTER),
    ("happybrownie", "Happy Brownie", "Брауни", "Прочее", ""),
    ("heartlocket", "Heart Locket", "Медальон-сердце", "Медальон", VALENTINE),
    ("heroichelmet", "Heroic Helmet", "Шлем героя", "Шлем", ""),
    ("hexpot", "Hex Pot", "Ведьмин котёл", "Котлы", HALLOWEEN),
    ("holidaydrink", "Holiday Drink", "Праздничный напиток", "Напиток", WINTER),
    ("homemadecake", "Homemade Cake", "Домашний торт", "Торт", ""),
    ("hypnolollipop", "Hypno Lollipop", "Гипно-леденец", "Леденец", ""),
    ("icecream", "Ice Cream", "Мороженое", "Мороженое", SUMMER),
    ("inputkey", "Input Key", "Клавиша", "Кнопка", ""),
    ("instantramen", "Instant Ramen", "Рамен", "Рамен", ""),
    ("intelligencecup", "Intelligence Cup", "Кубок интеллекта", "Кубок", ""),
    ("iongem", "Ion Gem", "Ионный камень", "Кристалл", ""),
    ("ionicdryer", "Ionic Dryer", "Фен", "Фен", ""),
    ("jackinthebox", "Jack-in-the-Box", "Шкатулка с сюрпризом", "Коробка", ""),
    ("jellybunny", "Jelly Bunny", "Желейный кролик", "Желе", SPRING),
    ("jesterhat", "Jester Hat", "Шутовской колпак", "Колпак", ""),
    ("jinglebells", "Jingle Bells", "Бубенцы", "Колокольчик", WINTER),
    ("jollychimp", "Jolly Chimp", "Весёлая обезьянка", "Обезьяна", ""),
    ("joyfulbundle", "Joyful Bundle", "Радостный набор", "Сумка", WINTER),
    ("khabibspapakha", "Khabib's Papakha", "Папаха Хабиба", "Шапка", ""),
    ("kissedfrog", "Kissed Frog", "Поцелованная жаба", "Жаба", VALENTINE),
    ("libertyfigure", "Liberty Figure", "Статуя Свободы", "Прочее", ""),
    ("lightsword", "Light Sword", "Световой меч", "Меч", ""),
    ("lolpop", "Lol Pop", "Леденец", "Леденец", ""),
    ("lootbag", "Loot Bag", "Мешок с добычей", "Пакетик", ""),
    ("lovecandle", "Love Candle", "Любовная свеча", "Свеча", VALENTINE),
    ("lovepotion", "Love Potion", "Любовное зелье", "Зелье", VALENTINE),
    ("lowrider", "Low Rider", "Лоурайдер", "Машина", ""),
    ("luckylocket", "Lucky Locket", "Медальон удачи", "Медальон", ""),
    ("lunarsnake", "Lunar Snake", "Лунная змея", "Змея", ""),
    ("lushbouquet", "Lush Bouquet", "Пышный букет", "Цветок", SPRING),
    ("madpumpkin", "Mad Pumpkin", "Тыква", "Тыква", HALLOWEEN),
    ("magicpotion", "Magic Potion", "Волшебное зелье", "Зелье", ""),
    ("mightyarm", "Mighty Arm", "Бицепс", "Бицепс", ""),
    ("minioscar", "Mini Oscar", "Оскар", "Оскар", ""),
    ("miragelamp", "Mirage Lamp", "Лампа-мираж", "Лампа", ""),
    ("moneypot", "Money Pot", "Горшок с деньгами", "Котлы", ""),
    ("moodpack", "Mood Pack", "Рюкзак", "Рюкзак", ""),
    ("moonpendant", "Moon Pendant", "Кулон-луна", "Полумесяц", ""),
    ("moussecake", "Mousse Cake", "Муссовый торт", "Торт", ""),
    ("nailbracelet", "Nail Bracelet", "Браслет-гвоздь", "Браслет", ""),
    ("nekohelmet", "Neko Helmet", "Неко-шлем", "Шлем", ""),
    ("partysparkler", "Party Sparkler", "Бенгальский огонь", "Бенгальские огни", WINTER),
    ("perfumebottle", "Perfume Bottle", "Духи", "Духи", VALENTINE),
    ("petsnake", "Pet Snake", "Змейка", "Змея", ""),
    ("plushpepe", "Plush Pepe", "Плюшевый Пепе", "Лягушка", ""),
    ("poolfloat", "Pool Float", "Надувной круг", "Надувной круг", SUMMER),
    ("preciouspeach", "Precious Peach", "Персик", "Персики", ""),
    ("prettyposy", "Pretty Posy", "Букетик", "Цветок", SPRING),
    ("rarebird", "Rare Bird", "Редкая птица", "Прочее", ""),
    ("recordplayer", "Record Player", "Проигрыватель", "Плеер", ""),
    ("resistancedog", "Resistance Dog", "Собака сопротивления", "Собака", ""),
    ("restlessjar", "Restless Jar", "Беспокойная банка", "Банка", ""),
    ("sakuraflower", "Sakura Flower", "Сакура", "Цветок", SPRING),
    ("santahat", "Santa Hat", "Шапка Санты", "Шапка", WINTER),
    ("scaredcat", "Scared Cat", "Испуганный кот", "Кошка", ""),
    ("sharptongue", "Sharp Tongue", "Острый язык", "Губы", ""),
    ("signetring", "Signet Ring", "Перстень", "Перстень", ""),
    ("skullflower", "Skull Flower", "Цветок-череп", "Цветок", HALLOWEEN),
    ("skystilettos", "Sky Stilettos", "Туфли", "Туфли", ""),
    ("sleighbell", "Sleigh Bell", "Колокольчик", "Колокольчик", WINTER),
    ("snakebox", "Snake Box", "Коробка со змеёй", "Коробка", ""),
    ("snoopcigar", "Snoop Cigar", "Сигара Снупа", "Сигарета", ""),
    ("snoopdogg", "Snoop Dogg", "Снуп Дог", "Прочее", ""),
    ("snowglobe", "Snow Globe", "Снежный шар", "Шар", WINTER),
    ("snowmittens", "Snow Mittens", "Варежки", "Варежки", WINTER),
    ("spicedwine", "Spiced Wine", "Глинтвейн", "Бокал", WINTER),
    ("springbasket", "Spring Basket", "Весенняя корзина", "Корзина", SPRING),
    ("spyagaric", "Spy Agaric", "Мухомор", "Гриб", ""),
    ("starnotepad", "Star Notepad", "Блокнот", "Блокнот", ""),
    ("stellarrocket", "Stellar Rocket", "Ракета", "Ракета", ""),
    ("surgeboard", "Surge Board", "Доска для сёрфа", "Прочее", SUMMER),
    ("swagbag", "Swag Bag", "Сумка", "Сумка", ""),
    ("swisswatch", "Swiss Watch", "Швейцарские часы", "Часы", ""),
    ("tamagadget", "Tama Gadget", "Тамагочи", "Гаджет", ""),
    ("timelessbook", "Timeless Book", "Вечная книга", "Прочее", ""),
    ("tophat", "Top Hat", "Цилиндр", "Шапка", ""),
    ("toybear", "Toy Bear", "Игрушечный мишка", "Медведь", ""),
    ("trappedheart", "Trapped Heart", "Сердце в клетке", "Сердце", VALENTINE),
    ("ufcstrike", "UFC Strike", "Удар UFC", "Прочее", ""),
    ("valentinebox", "Valentine Box", "Коробка Валентина", "Коробка", VALENTINE),
    ("vicecream", "Vice Cream", "Мороженое Vice", "Мороженое", SUMMER),
    ("victorymedal", "Victory Medal", "Медаль победы", "Прочее", ""),
    ("vintagecigar", "Vintage Cigar", "Винтажная сигара", "Сигарета", ""),
    ("voodoodoll", "Voodoo Doll", "Кукла вуду", "Вуду", HALLOWEEN),
    ("westsidesign", "Westside Sign", "Жест Westside", "Рука", ""),
    ("whipcupcake", "Whip Cupcake", "Капкейк", "Маффин", ""),
    ("winterwreath", "Winter Wreath", "Зимний венок", "Венок", WINTER),
    ("witchhat", "Witch Hat", "Шляпа ведьмы", "Шапка", HALLOWEEN),
    ("xmasstocking", "Xmas Stocking", "Рождественский носок", "Носок", WINTER),
]
COLLECTIONS: dict[str, Collection] = {r[0]: Collection(*r) for r in _RAW}


def slug_of(name: str) -> str:
    """«Durov’s Cap» → «durovscap», «Jack-in-the-Box» → «jackinthebox» (так Fragment называет коллекции)."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[’'`´]", "", (text or "").lower().replace("ё", "е")))


def en_pattern(en: str) -> re.Pattern:
    """Название коллекции в описании конкурента: регистр, апостроф, дефисы и множественное число не важны
    («Snow Mitten», «snow-mittens», «Durovs Cap»)."""
    words = [re.escape(w) for w in re.split(r"[\s\-]+", norm(en)) if w]
    if words and words[-1].endswith("s") and len(words[-1]) > 3:
        words[-1] = words[-1][:-1] + "s?"
    else:
        words[-1] = words[-1] + "s?"
    return re.compile(r"(?<![a-z])" + r"[\s\-]*".join(words) + r"(?![a-z])")


_PATTERNS: dict[str, re.Pattern] = {}


def find_collection(text: str, known: dict[str, Collection] | None = None) -> Collection | None:
    """Коллекция, о которой описание лота (по английскому названию). Несколько разных — None (набор/неясно)."""
    known = known or COLLECTIONS
    t = norm(text)
    hits = []
    for c in known.values():
        rx = _PATTERNS.get(c.en)
        if rx is None:
            rx = _PATTERNS[c.en] = en_pattern(c.en)
        if rx.search(t):
            hits.append(c)
    if len(hits) > 1:            # «Snoop Dogg» и «Snoop Cigar» не пересекаются; «Ice Cream» внутри «Vice Cream» — да
        longest = max(hits, key=lambda c: len(c.en))
        hits = [c for c in hits if c is longest or not en_pattern(c.en).search(norm(longest.en))]
    return hits[0] if len(hits) == 1 else None


def season_active(season: str, month: int, day: int) -> bool:
    if not season or season not in SEASON_WINDOWS:
        return False
    (m1, d1), (m2, d2) = SEASON_WINDOWS[season]
    cur, start, end = (month, day), (m1, d1), (m2, d2)
    if start <= end:
        return start <= cur <= end
    return cur >= start or cur <= end


def from_title(title: str) -> Collection:
    """Коллекция по названию из каталога Telegram (для новых, которых нет в справочнике)."""
    slug = slug_of(title)
    return COLLECTIONS.get(slug) or Collection(slug, title.strip(), title.strip())
