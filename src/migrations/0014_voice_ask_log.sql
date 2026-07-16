-- 0014: voice ask audit log (SP-Ask). One row per hands-free voice ask:
-- who asked (caller_sub + resolved company), what was heard (transcript) and
-- answered (answer), when. Additive-only. No FK on caller_sub/company_id: the
-- audit write must never fail an ask, so we don't couple it to users/companies
-- referential integrity (an unprovisioned caller still gets a row).
CREATE TABLE voice_ask_log (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id   uuid,
    caller_sub   text NOT NULL,
    transcript   text,
    answer       text,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX voice_ask_log_company_created_idx
    ON voice_ask_log (company_id, created_at DESC);
