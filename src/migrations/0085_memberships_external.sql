-- Project-owned tenancy (design 2026-10-09, P1): a person from another company
-- can be added to ONE site of this company. The membership grants exactly that
-- site; the person keeps their home company. `external` marks those rows so the
-- site's member list can say so and the home company is never confused with the
-- site's. Every pre-existing row is an employee of the site's company.
ALTER TABLE memberships ADD COLUMN IF NOT EXISTS external boolean NOT NULL DEFAULT false;
