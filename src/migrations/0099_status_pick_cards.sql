-- 0099: status pick cards (cross-session status reconcile r0).
--
-- One spoken status statement ("that one's done") that could refer to 2-4 open
-- items. Rather than guess, the writer stores a card listing the candidates and
-- a person picks one (state 'chosen', chosen_stable_id set) or none. Like 0090
-- this never writes action_items.status: the pick goes through the normal
-- audited path.
--
-- `candidates` is a jsonb array of stable_id strings (identity survives
-- re-extraction; action_items.id does not). The 2..4 bound is a CHECK: one
-- candidate is a plain suggestion (0090), five or more is not a pick.
-- UNIQUE (segment_key, offset_sec, suggested_status) with ON CONFLICT DO NOTHING
-- anchors a card to the moment it was said, so a re-run is idempotent and a
-- resolved card is never raised again. `run` is the writer's pass id, as in 0090.
CREATE TABLE IF NOT EXISTS status_pick_cards (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id       uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  site_id          uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  suggested_status text NOT NULL
                   CHECK (suggested_status IN ('done','closed','blocked','in_progress')),
  quote            text,
  segment_key      text NOT NULL,
  offset_sec       double precision NOT NULL,
  candidates       jsonb NOT NULL
                   CHECK (jsonb_array_length(candidates) BETWEEN 2 AND 4),
  run              text NOT NULL,
  state            text NOT NULL DEFAULT 'pending'
                   CHECK (state IN ('pending','chosen','none')),
  chosen_stable_id uuid,
  resolved_by      uuid REFERENCES users(id),
  resolved_at      timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (segment_key, offset_sec, suggested_status)
);

CREATE INDEX IF NOT EXISTS idx_status_pick_cards_pending
  ON status_pick_cards (site_id) WHERE state = 'pending';
