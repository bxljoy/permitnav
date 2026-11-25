-- Phase 6 slice 3: feedback a person chose to save, for review by the maintainer.
-- Only what the person saw and confirmed in the form is stored: the question and answer as
-- they edited them, an optional comment, and context that identifies no one (the answer's
-- kind, the date the rules were asked for, which sources it cited). No address, token,
-- conversation or trace id. Never part of the public corpus snapshot. A case reaches the
-- golden set only rewritten into a generic question; the row here is then deleted.

CREATE TABLE IF NOT EXISTS feedback (
  id             text        PRIMARY KEY,        -- random: says nothing about arrival order
  created_on     date        NOT NULL DEFAULT (now() AT TIME ZONE 'Europe/Stockholm')::date,
  question       text        NOT NULL,
  answer         text        NOT NULL,
  comment        text        NOT NULL DEFAULT '',
  kind           text        NOT NULL,          -- answered | follow_up | not_settled | …
  as_of          date        NOT NULL,
  cited          text[]      NOT NULL DEFAULT '{}',   -- unit ids the answer cited
  withdraw_hash  text        NOT NULL UNIQUE     -- SHA-256 of the browser's withdrawal code
);
