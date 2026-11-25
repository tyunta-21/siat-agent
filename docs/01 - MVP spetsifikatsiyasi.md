# SIAT AI Agent — MVP spetsifikatsiyasi

## MVP maqsadi

Cheklangan indikatorlar to‘plami bilan ishlaydigan, foydalanuvchi savolini qabul qilib, PostgreSQL’dan aniq statistik natija qaytaradigan agent yaratish.

## MVP chegarasi

- 5–10 ta indikator.
- Yillik ma’lumotlar.
- O‘zbekiston va viloyatlar kesimi.
- O‘zbek tilidagi savollar.
- JSON/CSV yuklash.
- PostgreSQL saqlash.
- OpenRouter LLM.
- Natijani jadval va matn ko‘rinishida berish.

## MVP muvaffaqiyat mezoni

Agent quyidagi savolga to‘g‘ri javob berishi kerak:

> 2020–2025-yillarda Toshkent viloyatida [indikator] qanday o‘zgargan?

Javobda quyidagilar bo‘lishi kerak:

- ishlatilgan indikator nomi;
- hudud;
- vaqt oralig‘i;
- qiymatlar jadvali;
- o‘zgarish foizi;
- SIAT manbasi;
- dataset kodi.

## Birinchi texnik deliverable’lar

1. `siat_catalog.py`
2. `siat_downloader.py`
3. `siat_normalizer.py`
4. `schema.sql`
5. `load_data.py`
6. `query_agent.py`
7. `.env.example`
8. `README.md`

## Hozirgi status

- [ ] SIAT endpointlari aniqlanadi.
- [ ] Birinchi dataset tanlanadi.
- [ ] Raw fayl yuklanadi.
- [ ] JSON strukturasi tekshiriladi.
- [ ] PostgreSQL schema yaratiladi.
- [ ] Birinchi SQL query ishlatiladi.
- [ ] Agentga ulanadi.
