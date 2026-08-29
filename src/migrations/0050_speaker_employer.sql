-- 0050 — who a named speaker works for, which is not who owns the recording.
--
-- `speaker_voiceprints.company_id` is the TENANT: which customer's data this row is. The
-- employer in "Andy M is from ABC Ltd" is a subcontractor, and it is a different thing.
-- Putting subcontractors into `companies` would make each one a tenant shell -- with sites,
-- memberships, a consent basis and an ACL -- and would widen who can see what by one line
-- that is very hard to walk back once rows exist. So: plain text, no foreign key.
--
-- No `employers` table either, yet. "ABC Ltd", "ABC" and "A.B.C. Limited" will need
-- reconciling eventually; normalising before there is data to normalise builds a second
-- identity system beside `name_aliases`, which already does that job for display names.
--
--   employer_name    free text, nullable -- no register, no employer, and the correction
--                    that builds the voiceprint must still work
--   employer_source  typed | suggested | sign_on_site
--   employer_set_by  who recorded it
--   employer_set_at  when
--
-- `employer_source` is load-bearing rather than decorative. A name typed by somebody who
-- knows Andy and a name accepted with one click from a register are different evidence, and
-- they have to be separable later -- when a subcontractor changes, when a customer asks where
-- a claim came from, or when a source turns out to have been wrong for a month. Recording
-- only the string makes that permanently unanswerable, which is the mistake `consent_given`
-- made before `consent_basis` was added beside it.
--
-- The CHECK is here and not only in the endpoint because this table has three writers
-- already (org-api's upsert, the voiceprint writer, and the Sign On Site adapter to come). A
-- rule enforced in one caller is a rule until somebody adds a second caller.

ALTER TABLE speaker_voiceprints
  ADD COLUMN IF NOT EXISTS employer_name   text,
  ADD COLUMN IF NOT EXISTS employer_source text,
  ADD COLUMN IF NOT EXISTS employer_set_by uuid REFERENCES users(id),
  ADD COLUMN IF NOT EXISTS employer_set_at timestamptz;

ALTER TABLE speaker_voiceprints
  DROP CONSTRAINT IF EXISTS speaker_voiceprints_employer_source_known;
ALTER TABLE speaker_voiceprints
  ADD CONSTRAINT speaker_voiceprints_employer_source_known
  CHECK (employer_source IS NULL
         OR employer_source IN ('typed', 'suggested', 'sign_on_site'));

-- Both or neither. A name with no source cannot be audited; a source with no name records
-- nothing at all.
ALTER TABLE speaker_voiceprints
  DROP CONSTRAINT IF EXISTS speaker_voiceprints_employer_paired;
ALTER TABLE speaker_voiceprints
  ADD CONSTRAINT speaker_voiceprints_employer_paired
  CHECK ((employer_name IS NULL) = (employer_source IS NULL));

-- The lookup Task 3 serves: "has anyone in this company already said who Andy M works for?"
-- Partial, because a row with no employer answers nothing and there are far more of those.
CREATE INDEX IF NOT EXISTS speaker_voiceprints_employer_lookup
  ON speaker_voiceprints (company_id, display_name)
  WHERE employer_name IS NOT NULL;
