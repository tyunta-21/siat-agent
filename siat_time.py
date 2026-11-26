"""Time words in questions and period labels in answers.

SIAT periods: annual "2025", quarterly "2025-Q3", monthly "2025-09", school year "2016/2017".
Quarterly and monthly volumes are usually year-to-date totals (indicators.cumulative): 2025-Q3 is
January–September, so it is called "yanvar–sentabr", not "III chorak".

A question may ask for
  * a period end:     "yanvar-sentabr", "9 oyda", "III chorak", "birinchi yarim yil", "sentabr",
                      "за 9 месяцев", "1 квартал"  -> month 9 / 9 / 9 / 6 / 9 / 9 / 3
  * a frequency:      "choraklar bo‘yicha", "oylar bo‘yicha", "по месяцам"  -> every quarter / month
  * a school year:    "2016/2017 o‘quv yili", "учебный год"
"Oylik" alone is NOT a frequency word: "oylik ish haqi" is the (annual) average monthly wage.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from siat_dictionary import norm

MONTHS_UZ = ["yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul", "avgust", "sentabr", "oktabr",
             "noyabr", "dekabr"]
_MONTH_PATTERNS = [  # Uzbek Latin, Russian / Uzbek Cyrillic stems
    (1, r"yanvar|январ"), (2, r"fevral|феврал"), (3, r"mart|март"), (4, r"aprel|апрел"),
    (5, r"may(?:da|ga|dan)?\b|ма[йяе]\b|майда"), (6, r"iyun|июн"), (7, r"iyul|июл"), (8, r"avgust|август"),
    (9, r"sentabr|sentyabr|сентябр|сентабр"), (10, r"oktabr|oktyabr|октябр|октабр"),
    (11, r"noyabr|ноябр"), (12, r"dekabr|декабр"),
]
MONTH_RE = [(n, re.compile(r"(?<![a-zа-я'])(?:" + p + ")")) for n, p in _MONTH_PATTERNS]
ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "1": 1, "2": 2, "3": 3, "4": 4}
ORDINAL_Q = {"birinchi": 1, "ikkinchi": 2, "uchinchi": 3, "to'rtinchi": 4, "tortinchi": 4,
             "перв": 1, "втор": 2, "трет": 3, "четв": 4}

# words that are about time, not about the indicator (the catalog search ignores them)
TIME_WORDS = set(MONTHS_UZ) | {
    "oylik", "choraklik", "chorak", "choragi", "choragida", "choraklar", "choraklarda", "kvartal", "kvartali",
    "oy", "oyi", "oyida", "oylar", "oylarda", "oyda", "oyligi", "yarim", "yilning", "boshidan", "o'quv",
    "квартал", "квартала", "квартале", "кварталам", "кварталы", "месяц", "месяца", "месяцев", "месяцам",
    "помесячно", "полугодие", "полугодия", "учебный", "учебном", "учебного", "января", "январь", "сентября",
    "март", "марта", "июня", "июнь", "декабря", "декабрь"}


@dataclass
class TimeAsk:
    freq: str | None = None      # 'Q' / 'M' / 'S' when the question names a frequency
    month: int | None = None     # end month of the asked period (quarter 3 -> 9)
    every: bool = False          # every quarter / month of the period ("choraklar bo‘yicha")

    @property
    def any(self) -> bool:
        return bool(self.freq or self.month or self.every)


def parse_time(question: str) -> TimeAsk:
    q = norm(question)
    t = TimeAsk()
    if re.search(r"o'quv yil|учебн|o'quv-yil|\d{4}\s*/\s*\d{4}", q):
        t.freq = "S"
        return t
    if re.search(r"choraklar (?:bo'yicha|kesimida)|har chorak|по кварталам|поквартальн", q):
        t.freq, t.every = "Q", True
    elif re.search(r"choraklik|ежеквартальн|квартальн", q):
        t.freq = "Q"
    if re.search(r"oylar (?:bo'yicha|kesimida)|har oy|oyma-oy|по месяцам|помесячн|ежемесячн", q):
        t.freq, t.every = "M", True

    # "III chorak", "3-chorak", "1 квартал", "Q3", "birinchi chorak", "первый квартал"
    m = re.search(r"(?<![a-zа-я0-9])(iv|i{1,3}|[1-4])\s*[-–]?\s*(?:chi\s+|inchi\s+|й\s+|-й\s+)?"
                  r"(?:chorak|kvartal|квартал|кв\b)", q) or re.search(r"(?<![a-z0-9])q([1-4])(?![0-9])", q)
    if m:
        t.month = ROMAN[m.group(1)] * 3
    else:
        for word, n in ORDINAL_Q.items():
            if re.search(word + r"\w*\s+(?:chorak|kvartal|квартал)", q):
                t.month = n * 3
    if t.month is None and re.search(r"(?:birinchi\s+)?yarim\s+yil|полугоди|yanvar\s*[-–]\s*iyun", q):
        t.month = 6
    if t.month is None:
        m = re.search(r"(?<![0-9])(1[0-2]|[1-9])\s*(?:oy(?!lik ish)|мес)", q)
        if m:
            t.month = int(m.group(1))
    if t.month is None:
        found = [(mm.start(), n) for n, rx in MONTH_RE for mm in [rx.search(q)] if mm]
        if found:
            # "yanvar-sentabr": the end of the range is the period; a single month is itself
            t.month = max(found)[1]
    return t


def school_years(question: str) -> list[int]:
    """'2016/2017' -> [2016] (a school year is stored with its first calendar year)."""
    return [int(a) for a, b in re.findall(r"(?<!\d)(\d{4})\s*/\s*(\d{4})(?!\d)", question)
            if int(b) == int(a) + 1]


QUARTER_ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV"}


def sub_label(freq: str, sub: int | None, cumulative: bool) -> str:
    """Name of the part of the year: 'yanvar–sentabr', 'III chorak', 'sentabr'."""
    if freq == "Q" and sub:
        if cumulative:
            return "yanvar–mart" if sub == 1 else f"yanvar–{MONTHS_UZ[sub * 3 - 1]}"
        return f"{QUARTER_ROMAN[sub]} chorak"
    if freq == "M" and sub:
        if cumulative and sub > 1:
            return f"yanvar–{MONTHS_UZ[sub - 1]}"
        return MONTHS_UZ[sub - 1]
    return ""


def period_label(freq: str, year: int, sub: int | None = None, cumulative: bool = False,
                 short: bool = False) -> str:
    """'2025' (A, short) / '2025-yil' (A) / '2016/2017 o‘quv yili' / '2025-yil yanvar–sentabr'."""
    if freq == "S":
        return f"{year}/{year + 1}" + ("" if short else " o‘quv yili")
    if freq == "D":
        return f"{year} {sub}-davr" if short else f"{year}-yil {sub}-davr"
    if freq in ("Q", "M"):
        return f"{year} {sub_label(freq, sub, cumulative)}" if short else f"{year}-yil {sub_label(freq, sub, cumulative)}"
    return str(year) if short else f"{year}-yil"
