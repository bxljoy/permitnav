-- Schema from docs/DESIGN.md, "Phase 1 internals" (D2, D3, D4, D5).

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- Evidence: every download; raw bytes live in object storage (data/raw locally).
CREATE TABLE download (
  id            bigserial PRIMARY KEY,
  source        text        NOT NULL,
  url           text        NOT NULL,
  fetched_at    timestamptz NOT NULL,
  content_hash  text        NOT NULL,
  storage_path  text        NOT NULL,
  http_status   int         NOT NULL
);

-- Stable identity of a citable unit (D2).
CREATE TABLE unit (
  id              text PRIMARY KEY,          -- 'sfs:2005:716/6kap/2§'
  source_type     text NOT NULL,
  authority       text NOT NULL,
  language        text NOT NULL CHECK (language IN ('sv', 'en')),
  document        text NOT NULL,             -- 'sfs:2005:716', page URL, 'mig:2023:1'
  heading         text NOT NULL,
  url             text NOT NULL,
  translation_of  text REFERENCES unit(id)   -- linked translations (D6)
);

-- One row per wording of a unit (D3). Status is computed from the dates, never stored.
CREATE TABLE unit_version (
  id             bigserial PRIMARY KEY,
  unit_id        text NOT NULL REFERENCES unit(id),
  text           text NOT NULL,
  text_hash      text NOT NULL,
  time_basis     text NOT NULL CHECK (time_basis IN ('valid', 'observed')),
  valid_from     date,                       -- NULL = start not known
  valid_to       date,                       -- NULL = still applies
  amended_by     text,                       -- '2026:295'
  observed_from  timestamptz NOT NULL,
  observed_to    timestamptz,
  download_id    bigint NOT NULL REFERENCES download(id),
  CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_from < valid_to),
  CHECK (time_basis = 'valid' OR (valid_from IS NULL AND valid_to IS NULL)),
  CONSTRAINT one_wording_per_day
    EXCLUDE USING gist (unit_id WITH =, daterange(valid_from, valid_to) WITH &&)
    WHERE (time_basis = 'valid')
);
CREATE INDEX ON unit_version (unit_id);

-- Links: transitional provisions both ways (D4), rulings' cited units (D7).
CREATE TABLE unit_link (
  from_unit_id    text   NOT NULL REFERENCES unit(id),
  to_version_id   bigint NOT NULL REFERENCES unit_version(id),
  kind            text   NOT NULL
    CHECK (kind IN ('transitional_introduced', 'transitional_ended', 'cites')),
  PRIMARY KEY (from_unit_id, to_version_id, kind)
);

-- Chunks are built from a version, per chunking strategy.
CREATE TABLE chunk (
  id          bigserial PRIMARY KEY,
  version_id  bigint    NOT NULL REFERENCES unit_version(id),
  strategy    text      NOT NULL,            -- 'structure' | 'fixed500'
  ordinal     int       NOT NULL,
  text        text      NOT NULL,            -- heading prepended
  ts_config   regconfig NOT NULL,            -- 'swedish' | 'english'
  tsv         tsvector GENERATED ALWAYS AS (to_tsvector(ts_config, text)) STORED,
  UNIQUE (version_id, strategy, ordinal)
);
CREATE INDEX ON chunk USING gin (tsv);

-- Embeddings per model (dimensions differ per model).
CREATE TABLE chunk_embedding_bge_m3 (
  chunk_id   bigint PRIMARY KEY REFERENCES chunk(id),
  embedding  vector(1024) NOT NULL
);
CREATE INDEX ON chunk_embedding_bge_m3 USING hnsw (embedding vector_cosine_ops);
