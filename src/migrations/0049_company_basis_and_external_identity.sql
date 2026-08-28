-- 0049 — the basis belongs to the company, and the identity comes from the sign-in system.
--
-- 0048 added `consent_basis` and had the endpoint type it per correction. That was wrong
-- about where the fact lives. The basis on a real site is a COMPANY fact: the induction tells
-- workers their voice is captured for reports and archiving and not for training, and the
-- subcontract says the same. It is settled before anyone opens the app, and every correction
-- made inside that company inherits it. Typing it per request would let two corrections in
-- one company disagree about the basis under which the same person was recorded.
--
--   companies.voiceprint_consent_basis   notice | attestation | confirmed | NULL
--                                        NULL = this company has not settled one, and
--                                        enrolment falls back to the strict pre-0048 rule.
--
-- The identity half. Site sign-in (Sign On Site and its equivalents) already knows who was on
-- a site on a day, which is both a stabler key than a display name and the candidate pool the
-- matcher has never had -- `profiles_for_matching`'s site narrowing has been a measured no-op
-- twice, for want of anything that could say who was there.
--
--   external_ref     the sign-in system's id for this person
--   external_source  which system it came from, e.g. 'sign_on_site'
--
-- UNIQUE per (company, source, ref) and NOT global, which is the load-bearing part.
--
-- A voiceprint built for company A must never reach company B, even when it is the same
-- human: the recording was made under A's induction and A's subcontract, and B has no basis
-- for it at all. `company_id` already scopes the row. What this constraint adds is that the
-- SAME external id under two companies stays two independent profiles.
--
-- There is deliberately no table anywhere linking those two rows. That is not an omission to
-- be tidied up later: a table saying "these two company profiles are the same person" IS the
-- cross-company disclosure, whether or not a vector ever moves. The system should be unable
-- to answer the question.
ALTER TABLE companies
    ADD COLUMN IF NOT EXISTS voiceprint_consent_basis text;

ALTER TABLE speaker_voiceprints
    ADD COLUMN IF NOT EXISTS external_ref    text,
    ADD COLUMN IF NOT EXISTS external_source text;

CREATE UNIQUE INDEX IF NOT EXISTS speaker_voiceprints_external_ident
    ON speaker_voiceprints (company_id, external_source, external_ref)
    WHERE external_ref IS NOT NULL AND status <> 'withdrawn';
