-- Phase 5 experiment: plain-language descriptions of law sections, used only to find them.
-- Swedish law often says whom a rule is for only through section numbers ("5 kap. 10 §",
-- "6 kap. 2 §"), which meaning-based search cannot follow. A model-written description is
-- indexed as an extra chunk of the wording (kind 'description'); it is never shown to the
-- answer model or cited. Retrieval ignores it unless asked to (RetrievalConfig.descriptions).

ALTER TABLE chunk ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'text';
DO $$ BEGIN
  ALTER TABLE chunk ADD CONSTRAINT chunk_kind CHECK (kind IN ('text', 'description'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS unit_description (
  version_id   bigint      PRIMARY KEY REFERENCES unit_version(id),
  text         text        NOT NULL,        -- Swedish, then English, as the model wrote it
  model        text        NOT NULL,
  prompt_hash  text        NOT NULL,        -- which describe.md produced it
  created_at   timestamptz NOT NULL DEFAULT now()
);
