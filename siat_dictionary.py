"""Synonyms for indicators and regions (Uzbek Latin / Cyrillic / Russian spellings).

Used by load_data.py (stored in the DB) and by query_agent.py (matching questions).
Add new words here; no code changes needed elsewhere.
"""
from __future__ import annotations

import re

INDICATOR_SYNONYMS: dict[int, list[str]] = {
    246:  ["aholi soni", "doimiy aholi", "aholi", "население", "численность населения", "population"],
    223:  ["tug'ilganlar", "tugilganlar", "tug'ilish", "tug'ilganlar soni", "рождаемость", "родившиеся", "births"],
    1299: ["yalpi hududiy mahsulot", "yahm", "yaim", "yalpi ichki mahsulot", "hududiy mahsulot", "врп", "ввп", "grp", "gdp"],
    660:  ["aholi jon boshiga yahm", "jon boshiga yalpi", "jon boshiga yahm", "врп на душу", "per capita gdp", "per capita grp"],
    552:  ["sanoat", "sanoat mahsulotlari", "sanoat ishlab chiqarish", "промышленность", "промышленное производство", "industrial output"],
    1326: ["investitsiya", "investitsiyalar", "asosiy kapitalga investitsiyalar", "инвестиции", "investment"],
    2704: ["chakana savdo", "savdo", "розничная торговля", "розница", "retail"],
    500:  ["ish haqi", "oylik ish haqi", "o'rtacha ish haqi", "maosh", "зарплата", "заработная плата", "wage", "salary"],
    1875: ["ishsizlik", "ishsizlik darajasi", "безработица", "unemployment"],
    2360: ["eksport", "eksport hajmi", "экспорт", "export"],
}

REGION_ALIASES: dict[str, list[str]] = {
    "1700": ["o'zbekiston", "respublika", "uzbekistan", "узбекистан", "республика", "jami"],
    "1735": ["qoraqalpog'iston", "qoraqalpogiston", "karakalpakstan", "каракалпакстан", "нукус"],
    "1703": ["andijon", "andijan", "андижан"],
    "1706": ["buxoro", "bukhara", "бухара"],
    "1708": ["jizzax", "jizzakh", "джизак"],
    "1710": ["qashqadaryo", "kashkadarya", "кашкадар"],
    "1712": ["navoiy", "navoi", "навои"],
    "1714": ["namangan", "наманган"],
    "1718": ["samarqand", "samarkand", "самарканд"],
    "1722": ["surxondaryo", "surkhandarya", "сурхандар"],
    "1724": ["sirdaryo", "syrdarya", "сырдар"],
    "1727": ["toshkent viloyati", "toshkent vil", "tashkent region", "ташкентская область", "ташкентской области"],
    "1730": ["farg'ona", "fargona", "fergana", "фергана", "ферганск"],
    "1733": ["xorazm", "khorezm", "хорезм"],
    "1726": ["toshkent shahri", "toshkent sh", "tashkent city", "город ташкент", "г. ташкент", "ташкент город"],
}

_APOS = re.compile(r"[‘’ʻʼ`´'՚]")
_CYR2LAT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "j", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "x", "ц": "ts", "ч": "ch", "ш": "sh", "ъ": "'", "ь": "", "э": "e", "ю": "yu",
    "я": "ya", "ў": "o'", "қ": "q", "ғ": "g'", "ҳ": "h",
})
UZ_CYR = set("ўқғҳ")


def norm(text: str) -> str:
    """Lowercase, unify apostrophes, transliterate Uzbek Cyrillic to Latin, squeeze spaces."""
    t = text.lower()
    if any(c in UZ_CYR for c in t):
        t = t.translate(_CYR2LAT)
    t = _APOS.sub("'", t)
    return re.sub(r"\s+", " ", t).strip()
