-- 0042 — who said yes.
--
-- 0038 recorded `consent_at` but not WHO consented. §6 is explicit that the consent must
-- come from the person whose voice it is, and a timestamp alone cannot tell that apart from
-- the wearer clicking a box on somebody else's behalf — which is precisely the failure mode
-- the section was written about.
--
-- Nullable: profiles created before this column existed have no answer, and inventing one
-- would be worse than admitting it. An unnamed profile has no consent and needs none.
ALTER TABLE speaker_voiceprints ADD COLUMN IF NOT EXISTS consented_by uuid;
