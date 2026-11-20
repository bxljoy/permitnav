-- Slice 2 (docs/DESIGN.md, "Known open points" from the statute parser).

-- A wording that takes effect "on the day the government decides" has no start date yet.
-- 'starts': not in force until a date is set; 'ends': in force, but will end on that day.
ALTER TABLE unit_version
  ADD COLUMN pending text CHECK (pending IN ('starts', 'ends'));

-- TRUE when valid_from/valid_to were not in the source but set to the date the crawler saw
-- one wording replace another (the source gave no date for the change).
ALTER TABLE unit_version
  ADD COLUMN dates_estimated boolean NOT NULL DEFAULT false;

-- TRUE when a wording was announced but disappeared from the source before it took effect
-- (the amendment was changed or cancelled). It never applies.
ALTER TABLE unit_version
  ADD COLUMN withdrawn boolean NOT NULL DEFAULT false;

-- Not-yet-dated and withdrawn wordings must not take part in the overlap rule.
-- Deferrable, so one download is applied in a single transaction in any order and checked
-- at commit.
ALTER TABLE unit_version DROP CONSTRAINT one_wording_per_day;
ALTER TABLE unit_version ADD CONSTRAINT one_wording_per_day
  EXCLUDE USING gist (unit_id WITH =, daterange(valid_from, valid_to) WITH &&)
  WHERE (time_basis = 'valid' AND pending IS DISTINCT FROM 'starts' AND NOT withdrawn)
  DEFERRABLE INITIALLY DEFERRED;
