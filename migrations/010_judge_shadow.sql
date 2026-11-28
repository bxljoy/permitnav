-- A shadow comparison of two faithfulness judges on the maintainer's own questions
-- (permitnav.shadow): one row per answer both judges read. Temporary by design: the rows
-- are removed when the review is over (`permitnav shadow clear`).
--
-- No text: no question, no answer, no source, none of a judge's quotations or reasons.
-- What identifies the answer is the random trace id (to open it in Langfuse, where the
-- maintainer's own questions are recorded in full) and a hash of what the judges were
-- given. Only requests made with the maintainer's token are ever written here. Never part
-- of the public corpus snapshot.
--
-- The row is written before the shadow judge is called (state 'reserved', with an
-- estimated cost held against the budget) and completed after it, so the sample limit and
-- the budget hold across restarts and failed calls.

CREATE TABLE IF NOT EXISTS judge_shadow (
  id            bigserial    PRIMARY KEY,
  at            timestamptz  NOT NULL DEFAULT now(),
  trace_id      text         NOT NULL UNIQUE,
  release       text,                        -- the deployed commit
  prompt_hash   text         NOT NULL,       -- the faithfulness prompt both judges used
  context_hash  text         NOT NULL,       -- sha256 of the question, answer and sources given to both
  token_limit   integer      NOT NULL,       -- the same for both
  state         text         NOT NULL CHECK (state IN ('reserved', 'done')),
  charged_usd   numeric      NOT NULL,       -- held against the budget: an estimate until the cost is known

  deployed_model          text     NOT NULL,
  deployed_effort         text,
  deployed_version        text,
  deployed_faithful       boolean  NOT NULL,
  deployed_unsupported    integer  NOT NULL,   -- how many claims it listed
  deployed_calls          integer  NOT NULL,
  deployed_input_tokens   integer  NOT NULL,
  deployed_output_tokens  integer  NOT NULL,
  deployed_cost_usd       numeric,             -- NULL: the provider did not report it
  deployed_latency_ms     integer  NOT NULL,

  shadow_model            text     NOT NULL,
  shadow_effort           text,
  shadow_version          text,
  shadow_status           text CHECK (shadow_status IN ('completed', 'failed')),  -- NULL: not finished
  shadow_faithful         boolean,
  shadow_unsupported      integer,
  shadow_calls            integer,
  shadow_input_tokens     integer,
  shadow_output_tokens    integer,
  shadow_cost_usd         numeric,
  shadow_latency_ms       integer
);

-- Answers the shadow judge was not asked about, counted by reason (a fixed vocabulary in
-- permitnav.shadow), so the review can say how many were eligible and why some are not in
-- the sample. A count only; removed with the rows above.

CREATE TABLE IF NOT EXISTS judge_shadow_skip (
  reason  text     PRIMARY KEY,
  n       integer  NOT NULL DEFAULT 0
);
