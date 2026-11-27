-- Counts per day of what happened to the questions asked: how many of each answer kind, how
-- many needed a second model call, how many were judged faithful to their sources, how many
-- were rated helpful. Langfuse's free plan shows only the last 30 days, so trends are kept
-- here. One row per Stockholm calendar day, measure and value, with a count only: no
-- question, no answer, no conversation or trace id, nothing about who asked. The values
-- are a fixed vocabulary written by the code (permitnav.stats), never text from a person.
-- Never part of the public corpus snapshot.

CREATE TABLE IF NOT EXISTS daily_count (
  day      date     NOT NULL,
  measure  text     NOT NULL,   -- kind | second_call | faithful | rating | …
  value    text     NOT NULL,   -- answered | yes | no | helpful | …
  n        integer  NOT NULL DEFAULT 0,
  PRIMARY KEY (day, measure, value)
);
