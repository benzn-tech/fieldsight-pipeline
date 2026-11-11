-- Action pack quality fixes (spec 2026-11-05 §9.1): one additive, idempotent migration.
-- action_packs.sub_tasks: the planner's sub-tasks and how the run answered each (§4);
--   NULL for a plan without sub-tasks and for every pack stored before this migration.
-- action_pack_actions.verifier_detail: the verifier's record for one action (§7);
--   NULL for every action stored before this migration.
-- Both are nullable, with no CHECK: the store tolerates a database without them
-- (repositories/action_packs.py retries the whole store without the two keys).
ALTER TABLE action_packs ADD COLUMN IF NOT EXISTS sub_tasks jsonb;
ALTER TABLE action_pack_actions ADD COLUMN IF NOT EXISTS verifier_detail jsonb;
