"""Find SIAT indicators by a free-text question (Uzbek Latin / Cyrillic, Russian, English).

Searches every indicator loaded into the database: its names in four languages, its catalog
path (section / subsection) and the hand-written synonyms from siat_dictionary.py.

Scoring is simple and explainable (no extra libraries, no LLM):
  * the question is normalized, region names, years and question words are removed;
  * each remaining word matches a word of the indicator if they are equal or share a stem
    (Uzbek and Russian add suffixes: "sanoat" ~ "sanoatda", "населения" ~ "население");
  * a match is worth the word's rarity (IDF): "qishloq" says more than "hajmi";
    name and synonym words count fully, catalog-path words count less;
  * a small bonus for covering a larger share of the indicator name prefers the plain
    indicator ("Aholi soni") over long variants ("Aholi soni, jinsi bo‘yicha ...").

Usage:
  python siat_search.py "qishloq aholisi soni"       # top matches with scores
  python siat_search.py --top 20 "go'sht"
"""
from __future__ import annotations

import argparse
import math
import re
from dataclasses import dataclass

from siat_dictionary import _CYR2LAT, INDICATOR_SYNONYMS, REGION_ALIASES, norm

STOP = set("""
va hamda bilan yoki uchun bo'yicha boyicha qanday qancha necha nechta qaysi o'zgargan o'zgardi ozgargan
o'zgarishi o'zgarish dinamika dinamikasi ko'rsat ko'rsating korsat ber bering haqida ma'lumot malumot
yil yili yilda yilgi yildagi yillar yillarda yillardagi yillik davomida davrida oralig'ida gacha dan
viloyat viloyati viloyatida viloyatlar viloyatlarida viloyatlari shahar shahri shahrida tuman tumanlar
respublika respublikasi respublikada o'zbekiston hudud hududlar hududlarida hududida kesimida barcha jami
qilib necha nima edi bo'ldi bo'lgan the of in by and for what how was were year years region regions
в во и по за на с со от до годы год году годах годам как какой какая каков изменился изменилась
сколько область области областях республика республике узбекистан регион регионам регионах всех все
динамика данные покажи показать был была были
qanchaga o'sgan o'sdi oshgan oshdi kamaygan kamaydi ko'paygan ko'paydi qisqargan
вырос выросла выросло увеличился увеличилась снизился снизилась уменьшился изменение изменения
""".split())

# Words that turn an indicator into a derived one (growth rate, per capita, share, index) or a
# subgroup (women, rural). A name with such a word loses points unless the question asks for it,
# so "chakana savdo" finds the turnover itself, not its growth rate.
MODIFIERS = {"o'sish": 2.0, "sur'ati": 2.0, "indeksi": 2.0, "ulushi": 2.0, "salmog'i": 2.0, "boshiga": 2.0,
             "koeffitsienti": 2.0, "nisbati": 2.0, "fizik": 2.0, "ayol": 1.0, "ayollar": 1.0, "erkak": 1.0,
             "erkaklar": 1.0, "qiz": 1.0, "o'g'il": 1.0, "shahar": 1.0, "qishloq": 1.0}

_WORD = re.compile(r"[a-z0-9'а-яё]+")


def translit(text: str) -> str:
    """Uzbek Cyrillic -> Latin even when the text has no ў/қ/ғ/ҳ (e.g. "саноат")."""
    return norm(text.lower().translate(_CYR2LAT))


def words(text: str) -> list[str]:
    return [w.strip("'") for w in _WORD.findall(text or "") if len(w.strip("'")) > 1]


UZ_SUFFIXES = ("li", "lar", "ni", "ning", "da", "dan", "ga", "lari", "larning", "i", "si")


def same_stem(a: str, b: str) -> bool:
    if a == b:
        return True
    s_, l_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(s_) >= 3 and l_.startswith(s_) and l_[len(s_):] in UZ_SUFFIXES:  # "don" ~ "donli"
        return True
    if min(len(a), len(b)) < 4:
        return False
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if long_.startswith(short):
        return True
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n >= 4 and n >= min(len(a), len(b)) - 2


@dataclass
class Hit:
    id: int
    score: float
    code: str
    name: str
    unit: str
    path: str
    synonym: bool = False
    regions: int = 0          # how many regions have data (1 = republic only)
    confidence: float = 0.0  # share of the question's meaning words this indicator explains


class IndicatorIndex:
    def __init__(self, rows: list[dict]):
        """rows: id, code, name_uz, name_ru, name_en, name_uzc, unit_uz, path_uz, synonyms"""
        self.docs = []
        df: dict[str, int] = {}
        for r in rows:
            name = set(words(norm(r.get("name_uz") or "")) + words(translit(r.get("name_uzc") or ""))
                       + words((r.get("name_ru") or "").lower()) + words((r.get("name_en") or "").lower()))
            name -= STOP
            syn_phrases = [norm(s) for s in (r.get("synonyms") or []) + INDICATOR_SYNONYMS.get(r["id"], [])]
            syn = {w for s in syn_phrases for w in words(s)} - STOP
            path = set(words(norm(r.get("path_uz") or ""))) - STOP - name
            langs = [set(words(norm(r.get("name_uz") or ""))), set(words((r.get("name_ru") or "").lower())),
                     set(words((r.get("name_en") or "").lower()))]
            mods = {m for m in MODIFIERS if any(same_stem(m, t) for t in words(norm(r.get("name_uz") or "")))}
            langs = [l - STOP - {"yillik", "годовой", "годовые", "annual"} for l in langs]
            self.docs.append(dict(row=r, name=name, syn=syn, syn_phrases=syn_phrases, path=path,
                                  langs=[l for l in langs if l], mods=mods))
            for w in name | syn:
                df[w] = df.get(w, 0) + 1
        n = max(len(self.docs), 1)
        self.idf = {w: math.log(1 + n / c) for w, c in df.items()}
        self.default_idf = math.log(1 + n)

    @classmethod
    def from_db(cls, conn) -> "IndicatorIndex":
        with conn.cursor() as cur:
            cur.execute("""SELECT i.id, i.code, i.name_uz, i.name_ru, i.name_en, i.name_uzc, i.unit_uz,
                                  d.path_uz, i.synonyms, r.codes AS region_codes
                           FROM siat.indicators i LEFT JOIN siat.datasets d ON d.id = i.id
                           JOIN (SELECT indicator_id, array_agg(DISTINCT region_code) AS codes
                                 FROM siat.v_obs GROUP BY 1) r ON r.indicator_id = i.id""")
            cols = [c.name for c in cur.description]
            return cls([dict(zip(cols, r)) for r in cur.fetchall()])

    def _idf(self, w: str) -> float:
        if w in self.idf:
            return self.idf[w]
        best = [v for k, v in self.idf.items() if same_stem(w, k)]
        return max(best) if best else self.default_idf

    @staticmethod
    def query_words(question: str) -> list[str]:
        q = norm(question)
        for aliases in REGION_ALIASES.values():  # region names are not part of the indicator
            for a in sorted(aliases, key=len, reverse=True):
                q = re.sub(r"(?<![a-zа-я'])" + re.escape(norm(a)) + r"[a-zа-я']*", " ", q)
        q = re.sub(r"\d+", " ", q)
        ws = words(q)
        # Uzbek Cyrillic without special letters ("саноат") is also tried in Latin
        ws += [t for w in ws if re.search(r"[а-яё]", w) and w not in STOP for t in words(translit(w))]
        out = []
        for w in ws:
            if w not in STOP and w not in out and not w.startswith("toshkent") and not w.startswith("ташкент"):
                out.append(w)
        return out

    def known(self, w: str) -> bool:
        return w in self.idf or any(same_stem(w, k) for k in self.idf)

    def search(self, question: str, top: int = 10, regions: list[str] | None = None) -> list[Hit]:
        """regions: the regions asked about. Many indicators exist only for the whole republic;
        when the question names regions, indicators without data for them lose points."""
        qw = self.query_words(question)
        if not qw:
            return []
        qn = norm(question)
        idf = {w: self._idf(w) for w in qw}
        # One "meaning" per question word; a Cyrillic word and its Latin transliteration are one
        # meaning. Words no indicator uses ("go'sht" when meat is only quarterly) still count, so a
        # hit that ignores them gets low confidence.
        groups: list[set[str]] = []
        for w in qw:
            if any(w in g for g in groups):
                continue
            g = {w} | ({t for t in words(translit(w)) if t in qw} if re.search(r"[а-яё]", w) else set())
            groups.append(g)
        weight = [max(idf[w] if self.known(w) else 4.0 for w in g) for g in groups]
        total = sum(weight) or 1.0
        hits = []
        for d in self.docs:
            score, matched = 0.0, set()
            for w in qw:
                if any(same_stem(w, t) for t in d["name"]) or any(same_stem(w, t) for t in d["syn"]):
                    score += idf[w]
                    matched.add(w)
                elif any(same_stem(w, t) for t in d["path"]):
                    score += 0.3 * idf[w]
            if not matched:
                continue
            syn_hit = any(re.search(r"(?<![a-zа-я'])" + re.escape(p), qn) for p in d["syn_phrases"])
            if syn_hit:
                score += 1.0
            cover = max((sum(1 for t in l if any(same_stem(w, t) for w in matched)) / len(l)
                         for l in d["langs"]), default=0.0)
            score += 3.0 * cover
            have = set(d["row"].get("region_codes") or [])
            if regions and have and regions != ["*"] and not set(regions) <= have:
                score -= 4.0
            elif regions == ["*"] and have and len(have) < 10:
                score -= 4.0
            score -= sum(MODIFIERS[m] for m in d["mods"] if not any(same_stem(m, w) for w in qw))
            explained = sum(wt for g, wt in zip(groups, weight) if g & matched)
            r = d["row"]
            hits.append(Hit(r["id"], round(score, 3), r.get("code") or "", r.get("name_uz") or "",
                            r.get("unit_uz") or "", r.get("path_uz") or "", syn_hit,
                            len(r.get("region_codes") or []),
                            round(min(explained / total, 1.0), 2)))
        hits.sort(key=lambda h: (-h.score, len(h.name), h.id))
        return hits[:top]


_cache: dict[str, IndicatorIndex] = {}


def get_index(conn) -> IndicatorIndex:
    """Built once per process (2 000 indicators take well under a second)."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*), max(fetched_at)::text FROM siat.indicators")
        key = str(cur.fetchone())
    if key not in _cache:
        _cache.clear()
        _cache[key] = IndicatorIndex.from_db(conn)
    return _cache[key]


def main() -> None:
    import psycopg

    from query_agent import AGENT_DB_URL
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="+")
    ap.add_argument("--top", type=int, default=10)
    a = ap.parse_args()
    with psycopg.connect(AGENT_DB_URL) as conn:
        idx = get_index(conn)
        q = " ".join(a.question)
        print("words:", idx.query_words(q))
        for h in idx.search(q, a.top):
            print(f"{h.score:6.2f} {h.id:5d} {h.code:14s} {h.name}  [{h.unit}]  <{h.path}>"
                  + ("  *syn" if h.synonym else ""))


if __name__ == "__main__":
    main()
