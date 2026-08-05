-- 0032: recurring-item threading
-- (spec docs/superpowers/specs/2026-08-05-recurring-item-threading-design.md).
--
-- A commitment made on site is restated on later days -- "Sub A said the
-- ground floor walls finish Wednesday" comes back on Wednesday, done or
-- slipped. Each recording produces a brand-new topic with no link to the
-- earlier one, so nothing can notice that a thing has been promised three
-- times and slipped twice.
--
-- A thread is the SUBJECT, threaded across days. Measuring prod settled
-- that: matching the commitments to each other produced the cross-product of
-- two related topics ("Check door stop availability" scoring the same
-- against "Install door handles" as against everything else), because the
-- similarity lives in the subject while the promises differ every time.
--
-- Additive and idempotent. thread_id NULL = an unthreaded topic, which is
-- every row that exists today and will stay the common case: most topics are
-- a new subject, and inventing a parent for them is the failure this design
-- exists to avoid.

CREATE TABLE IF NOT EXISTS topic_threads (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- A thread is one site's ongoing job. Cross-site threading is not a
    -- feature waiting to be enabled: "door hardware" at two schools is two
    -- jobs, and merging them would put one site's slippage on another's card.
    site_id     uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    -- The CURRENT framing, seeded from the first topic and updatable through
    -- the same propose-and-confirm path as any other extracted text. The
    -- originals stay on their topics; this never overwrites them.
    title       text NOT NULL,
    first_seen  date NOT NULL,
    -- Denormalised so "what came up again this week" is one index scan
    -- rather than an aggregate over every member topic.
    last_raised date NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz
);

CREATE INDEX IF NOT EXISTS idx_topic_threads_site_last
    ON topic_threads (site_id, last_raised DESC);

ALTER TABLE topics ADD COLUMN IF NOT EXISTS thread_id uuid
    REFERENCES topic_threads(id) ON DELETE SET NULL;

-- ON DELETE SET NULL, deliberately not CASCADE: deleting a thread must
-- un-thread its topics, never delete them. CLAUDE.md records the programme
-- version of this mistake -- parent_id cascaded, so a DELETE scoped to
-- imported rows silently took the local children with it, and 1598 unit
-- tests passed either way because the doubles do not enforce foreign keys.

-- Partial: only threaded topics are ever looked up this way, and the
-- overwhelming majority carry NULL.
CREATE INDEX IF NOT EXISTS idx_topics_thread
    ON topics (thread_id) WHERE thread_id IS NOT NULL;

-- The proposal a human has not answered yet. Kept OFF the topic row so an
-- unanswered suggestion can never be mistaken for a confirmed link: a
-- thread_id means a person said yes.
CREATE TABLE IF NOT EXISTS topic_thread_suggestions (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    topic_id     uuid NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    -- Exactly one of these: join an existing thread, or start one anchored
    -- on an earlier topic that has none yet.
    thread_id    uuid REFERENCES topic_threads(id) ON DELETE CASCADE,
    parent_topic_id uuid REFERENCES topics(id) ON DELETE CASCADE,
    score        real NOT NULL,
    gap_days     integer NOT NULL,
    status       text NOT NULL DEFAULT 'pending',   -- pending|confirmed|rejected
    created_at   timestamptz NOT NULL DEFAULT now(),
    resolved_at  timestamptz,
    resolved_by  text,
    CONSTRAINT topic_thread_suggestions_target
        CHECK ((thread_id IS NOT NULL) <> (parent_topic_id IS NOT NULL))
);

-- One live proposal per topic. A second matcher run must update the existing
-- row rather than stack a queue of near-identical questions in front of a
-- reviewer -- the review queue is only useful while it is short.
CREATE UNIQUE INDEX IF NOT EXISTS idx_topic_thread_suggestions_live
    ON topic_thread_suggestions (topic_id) WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_topic_thread_suggestions_pending
    ON topic_thread_suggestions (status, created_at) WHERE status = 'pending';
