-- src/migrations/0023_classification_feedback.sql
-- Life-conversation separation (2026-07-21 spec §4/§7): the privacy-preserving
-- feedback loop. Stores ONLY the human's verdict on the machine's work/non_work
-- call, the classifier confidence, and a COARSE topic category -- NEVER the
-- transcript or any personal text. This is the entire signal used to measure/
-- tune the classifier; personal content is never a training input, never
-- embedded. human_verdict: classifier flagged non_work & human agrees
-- (confirm_non_work=TP) / disagrees, it is work (reject_is_work=FP) / human
-- removed a NOT-flagged topic as personal (missed_personal=FN).
CREATE TABLE classification_feedback (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id            uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  topic_id              uuid NOT NULL,
  classifier_verdict    text CHECK (classifier_verdict IN ('work', 'non_work')),
  classifier_confidence real,
  human_verdict         text NOT NULL
                          CHECK (human_verdict IN ('confirm_non_work', 'reject_is_work', 'missed_personal')),
  topic_category        text,
  actor_user_id         uuid REFERENCES users(id),
  created_at            timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_classification_feedback_company ON classification_feedback (company_id, created_at);
