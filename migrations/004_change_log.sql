-- The "what changed" log (phase 4): one row per unit whose wording a download added, changed,
-- removed or re-dated, with a short diff. Written by ingest in the same transaction as the
-- versions. It is part of the service's own data (the change history), not of the public
-- eval corpus snapshot.
CREATE TABLE change_log (
  id              bigserial   PRIMARY KEY,
  observed_at     timestamptz NOT NULL,                 -- the download's fetch time
  download_id     bigint      REFERENCES download(id),       -- empty: a page that disappeared
  unit_id         text        NOT NULL REFERENCES unit(id),
  kind            text        NOT NULL CHECK (kind IN ('added', 'changed', 'removed', 'dates')),
  old_version_id  bigint      REFERENCES unit_version(id),
  new_version_id  bigint      REFERENCES unit_version(id),
  diff            text        NOT NULL DEFAULT ''
);
CREATE INDEX ON change_log (observed_at);
CREATE INDEX ON change_log (unit_id);
