-- A daily total of what the site's own model calls cost, for the daily spend cap: a leaked
-- access token can then cost at most the cap per day. One row per Stockholm calendar day
-- with sums only: no question, no token, nothing about who asked. Never part of the public
-- corpus snapshot.

CREATE TABLE IF NOT EXISTS spend (
  day       date     PRIMARY KEY,
  usd       numeric  NOT NULL DEFAULT 0,   -- as the provider reported it, plus estimates
  calls     integer  NOT NULL DEFAULT 0,   -- model calls made
  unpriced  integer  NOT NULL DEFAULT 0,   -- of those, calls whose cost was not reported
  refused   integer  NOT NULL DEFAULT 0,   -- questions declined because the cap was reached
  reported  boolean  NOT NULL DEFAULT false -- a capped day the daily refresh has reported
);
