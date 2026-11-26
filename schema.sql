-- SIAT AI Agent: PostgreSQL schema (MVP)
-- Layers: raw (raw_json) -> core (indicators, regions, periods, observations, metadata) -> view v_obs for the agent.
-- Safe to run several times.

CREATE SCHEMA IF NOT EXISTS siat;
SET search_path = siat, public;

-- Raw layer: SIAT responses exactly as downloaded
CREATE TABLE IF NOT EXISTS raw_json (
    id            bigserial PRIMARY KEY,
    indicator_id  integer,
    endpoint      text        NOT NULL,
    sha256        text        NOT NULL,
    fetched_at    timestamptz NOT NULL,
    body          jsonb       NOT NULL,
    UNIQUE (endpoint, sha256)
);

-- SIAT catalog nodes (sections and indicators)
CREATE TABLE IF NOT EXISTS datasets (
    id            integer PRIMARY KEY,          -- SIAT sdmx id
    parent_id     integer,
    code          text,
    period        text,                         -- Yillik / Choraklik / Oylik / Yangilanmaydigan
    classifier_id integer,
    is_leaf       boolean NOT NULL DEFAULT true,
    name_uz text, name_ru text, name_en text, name_uzc text,
    path_uz       text,
    updated_at    timestamptz
);

CREATE TABLE IF NOT EXISTS classifiers (
    id      integer PRIMARY KEY,
    name_uz text, name_ru text, name_en text, name_uzc text
);
INSERT INTO classifiers (id, name_uz, name_ru, name_en, name_uzc)
VALUES (1, 'Hududlar', 'Регионы', 'Territories', 'Ҳудудлар') ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS indicators (
    id             integer PRIMARY KEY,         -- SIAT sdmx id
    code           text,                        -- e.g. 1.02.01.0001 (dataset code)
    parent_id      integer,
    classifier_id  integer REFERENCES classifiers(id),
    period         text,
    name_uz text, name_ru text, name_en text, name_uzc text,
    unit_uz text, unit_ru text, unit_en text, unit_uzc text,
    methodology_uz text, methodology_ru text, methodology_en text, methodology_uzc text,
    note_uz text, note_ru text, note_en text, note_uzc text,
    periodicity_uz text,
    synonyms       text[] NOT NULL DEFAULT '{}', -- search helpers, e.g. {'YaHM','yalpi hududiy mahsulot'}
    source_url     text,
    updated_at     timestamptz,                 -- last update on SIAT
    fetched_at     timestamptz                  -- when we downloaded it
);

CREATE TABLE IF NOT EXISTS regions (
    code      text PRIMARY KEY,                 -- SOATO: 1700 republic, 1727 Toshkent viloyati, 1726 Toshkent shahri
    siat_id   integer,
    level     text NOT NULL,                    -- country / region / district
    parent_code text,
    map_code  text,
    name_uz text, name_ru text, name_en text, name_uzc text,
    aliases   text[] NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS periods (
    period  text PRIMARY KEY,                   -- 2025 | 2025-Q3 | 2025-09 | 2016/2017 (school year) | 2026-D1 (season)
    freq    char(1) NOT NULL CHECK (freq IN ('A','Q','M','S','D')),
    year    smallint NOT NULL,                  -- school year 2016/2017 -> 2016
    quarter smallint,
    month   smallint
);
-- older databases: allow school years and seasons
ALTER TABLE periods DROP CONSTRAINT IF EXISTS periods_freq_check;
ALTER TABLE periods ADD CONSTRAINT periods_freq_check CHECK (freq IN ('A','Q','M','S','D'));
-- quarterly / monthly series that are year-to-date totals (2025-Q3 = January–September)
ALTER TABLE indicators ADD COLUMN IF NOT EXISTS cumulative boolean NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS regions_parent_idx ON regions (parent_code);

CREATE TABLE IF NOT EXISTS observations (
    indicator_id integer NOT NULL REFERENCES indicators(id),
    region_code  text    NOT NULL REFERENCES regions(code),
    period       text    NOT NULL REFERENCES periods(period),
    value        numeric,
    value_raw    text,                          -- original text when it was not a number
    flag         text,                          -- missing / special:- / text:...
    loaded_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (indicator_id, region_code, period)
);
CREATE INDEX IF NOT EXISTS observations_region_idx ON observations (region_code, period);

CREATE TABLE IF NOT EXISTS metadata (
    indicator_id integer NOT NULL REFERENCES indicators(id),
    key          text    NOT NULL,
    position     integer,
    label_uz text, label_ru text, label_en text, label_uzc text,
    value_uz text, value_ru text, value_en text, value_uzc text,
    PRIMARY KEY (indicator_id, key)
);

CREATE TABLE IF NOT EXISTS load_log (
    id          bigserial PRIMARY KEY,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    step        text NOT NULL,
    rows        integer,
    status      text,
    details     text
);

-- Flat view for the agent: the ONLY relation the Text-to-SQL model is allowed to query.
CREATE OR REPLACE VIEW v_obs AS
SELECT i.id        AS indicator_id,
       i.code      AS dataset_code,
       i.name_uz   AS indicator,
       i.unit_uz   AS unit,
       r.code      AS region_code,
       r.name_uz   AS region,
       r.level     AS region_level,
       p.freq,
       p.year,
       p.quarter,
       p.month,
       o.value,
       p.period,
       i.cumulative,
       r.parent_code
FROM observations o
JOIN indicators i ON i.id = o.indicator_id
JOIN regions    r ON r.code = o.region_code
JOIN periods    p ON p.period = o.period;

-- Read-only role for the agent (run once as an admin; change the password)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'siat_reader') THEN
        CREATE ROLE siat_reader LOGIN PASSWORD 'change_me';
    END IF;
END $$;
GRANT USAGE ON SCHEMA siat TO siat_reader;
GRANT SELECT ON siat.v_obs, siat.indicators, siat.regions, siat.metadata, siat.datasets TO siat_reader;
ALTER ROLE siat_reader SET statement_timeout = '5s';
ALTER ROLE siat_reader SET default_transaction_read_only = on;
ALTER ROLE siat_reader SET search_path = siat;
