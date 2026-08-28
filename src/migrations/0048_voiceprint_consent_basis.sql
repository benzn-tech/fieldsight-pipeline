-- 0048 — on what basis a profile was created, and who said so.
--
-- Until now a named profile required `consented_by`: the uuid of the person whose voice it
-- is. That is the strongest basis and it is also the rarest one, because it needs the subject
-- to have an account. On a construction site the people most often named are subcontractors
-- with no login, so the strongest basis excluded exactly the population the feature is for,
-- and the result was a library with nothing in it.
--
-- Two columns rather than repurposing `consented_by`, which stays what it says: the SUBJECT,
-- when the subject is known. Overloading it with "whoever is doing the labelling" would make
-- every existing row ambiguous and every future audit unanswerable.
--
--   consent_basis  attestation | confirmed | notice
--                  attestation = somebody states the subject agreed. A record of a claim,
--                                made by a person with an interest in making it.
--                  confirmed   = the subject agreed on their own device. `consented_by` set.
--                  notice      = site-level notice with an opt-out register.
--   asserted_by    who made the claim, when the basis is `attestation`. Never the subject.
--
-- NULL basis on the rows that predate this is correct and deliberate: they were created under
-- the old rule, which required `consented_by`, so their basis is `confirmed` in substance —
-- but writing that in would be inventing a record of something nobody actually did. NULL says
-- "created before this was recorded", which is true.
ALTER TABLE speaker_voiceprints
    ADD COLUMN IF NOT EXISTS consent_basis text,
    ADD COLUMN IF NOT EXISTS asserted_by   uuid;
