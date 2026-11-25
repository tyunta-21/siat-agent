# SIAT AI Agent

## Maqsad

SIAT rasmiy statistik ma’lumotlarini yig‘ish, JSON va SQL formatida saqlash hamda foydalanuvchi savollariga aniq statistik javob beradigan AI agent yaratish.

## Konsept

Bu oddiy matnli RAG emas. Asosiy raqamli javoblar SQL orqali olinadi. Indikator nomi, metadata, metodika va sinonimlarni topishda semantic qidiruv/RAG ishlatiladi.

## Rejalashtirilgan arxitektura

1. SIAT API, JSON, CSV va XML manbalarini yig‘ish.
2. Raw ma’lumotlarni o‘zgartirmasdan saqlash.
3. Ma’lumotlarni normalizatsiya qilish.
4. PostgreSQL bazasiga yozish.
5. Indikator va metadata uchun semantic qidiruv yaratish.
6. Text-to-SQL agent ishlab chiqish.
7. Natijani tekshirish, jadval va grafik ko‘rinishida berish.
8. Yangilanishlarni avtomatik sinxronlash.

## Ma’lumot modeli

- `datasets`
- `indicators`
- `observations`
- `regions`
- `periods`
- `classifiers`
- `metadata`
- `raw_json`

## Agent vositalari

- indikator qidirish;
- metadata olish;
- statistik SQL so‘rov bajarish;
- hududlarni taqqoslash;
- o‘sish/kamayishni hisoblash;
- grafik yaratish;
- ma’lumot yangilanganini tekshirish.

## Hozirgi holat

- [x] Loyiha g‘oyasi belgilandi.
- [x] JSON + SQL + AI agent yondashuvi tanlandi.
- [x] Obsidian brain tuzilmasi yaratildi.
- [ ] SIAT katalogini tekshirish.
- [ ] Birinchi indikatorlar to‘plamini yuklash.
- [ ] PostgreSQL sxemasini yaratish.
- [ ] ETL pipeline yozish.
- [ ] AI agent MVP yaratish.

## Keyingi qadam

Avval 5–10 ta indikator bilan kichik ETL prototipini yaratish va SQL orqali savol-javobni tekshirish.

## To‘liq ish bosqichlari

### 1. Tadqiqot

- SIAT katalogi va kategoriyalarini aniqlash.
- API, JSON, CSV va XML endpointlarini tekshirish.
- Indikator ID, kod, hudud, davriylik va metadata tuzilmasini o‘rganish.
- Yuklab olish cheklovlari va yangilanish chastotasini aniqlash.

### 2. Ma’lumot yig‘ish

- Katalogni yuklash.
- Barcha dataset metadata’larini saqlash.
- Raw JSON/XML/CSV fayllarni arxivlash.
- Har bir dataset uchun yuklash logini yuritish.
- Xatolik bo‘lsa qayta urinish va log yozish.

### 3. Tozalash va standartlashtirish

- O‘zbek, rus va ingliz nomlarini alohida saqlash.
- Hudud nomlarini yagona kod bilan bog‘lash.
- Yil, oy va choraklarni standart formatga o‘tkazish.
- Sonlarni matndan numeric turga aylantirish.
- Bo‘sh qiymat, maxsus belgi va noto‘g‘ri qiymatlarni belgilash.
- O‘lchov birliklarini metadata bilan bog‘lash.

### 4. PostgreSQL bazasi

- Raw qatlam.
- Staging qatlam.
- Normalizatsiya qilingan asosiy qatlam.
- Analitik view’lar.
- Yangilanish va audit jadvallari.

### 5. Qidiruv qatlami

- Keyword qidiruv.
- Metadata semantic qidiruvi.
- Indikator sinonimlari.
- Hudud va klassifikator mapping’i.
- Embedding faqat nom, tavsif va metodika uchun ishlatiladi.

### 6. AI agent

- Savolni indikatorga aylantirish.
- Kerakli hudud va davrni aniqlash.
- SQL yaratish.
- SQL xavfsizligini tekshirish.
- Natijani validatsiya qilish.
- Hisob-kitoblarni bajarish.
- Jadval, grafik va tushuntirish chiqarish.
- Manba, dataset kodi va yangilanish sanasini ko‘rsatish.

### 7. Yangilash tizimi

- Rejalashtirilgan ETL.
- Faqat o‘zgargan datasetlarni qayta yuklash.
- Versiya va yangilanish tarixini saqlash.
- Noto‘g‘ri yoki o‘zgargan schema haqida ogohlantirish.

### 8. Test va sifat nazorati

- Ma’lum savollar uchun expected result testlari.
- SQL natijasini SIAT jadvali bilan solishtirish.
- Hudud, davr va birlik xatolarini tekshirish.
- Agentning manbasiz raqam uydirmasligini nazorat qilish.

## Ishlash qoidasi

Agent raqamlarni o‘z xotirasidan taxmin qilmaydi. Har bir raqamli javob bazadagi SQL natijasiga, har bir metodik izoh esa SIAT metadata’siga tayanadi. Ma’lumot topilmasa, agent buni ochiq aytadi.
