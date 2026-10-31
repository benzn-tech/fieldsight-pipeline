-- 0093 (second file): the day view's probe for agent-created topics
-- (spec docs/superpowers/specs/2026-11-01-background-action-agent-design.md §14 D2).
--
-- An action pack's apply writes its todos under a synthetic topic with source = 'agent'.
-- Every /timeline read first asks "has this person an agent topic on this date?"
-- (repositories.topics.agent_topic_author_for_folder_day). Partial, so the index holds
-- only agent topics: empty until the feature is used, and that probe is one tiny index
-- lookup. A separate file rather than an edit to 0093_action_task_numbers.sql, because
-- the runner records applied migrations by file name and would skip an edited file.
--
-- Read-only use, no new column: sam deploy runs before this migration, and in that window
-- the probe still works (a scan), only slower. Idempotent: safe to run twice.
CREATE INDEX IF NOT EXISTS idx_topics_agent_user_date
  ON topics (user_id, report_date) WHERE source = 'agent';
