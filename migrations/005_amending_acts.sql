-- Amending acts of the in-scope statutes (phase 4, slice 2): from the amendment register
-- (SFSR: scope and in-force date) and the SFS publication feed (early notice of new acts).
CREATE TABLE amending_act (
  sfs             text        PRIMARY KEY,          -- '2026:385'
  statute         text        NOT NULL,             -- '2005:716'
  title           text        NOT NULL,
  scope           text        NOT NULL DEFAULT '',  -- 'ändr. 1 kap. 1, 7 §§, …' (register)
  in_force        date,                             -- empty: not known yet
  partly_pending  boolean     NOT NULL DEFAULT false,
  transitional    boolean     NOT NULL DEFAULT false,
  published       date,                             -- from the feed
  in_register     boolean     NOT NULL DEFAULT false,
  first_seen      timestamptz NOT NULL,
  last_seen       timestamptz NOT NULL
);
