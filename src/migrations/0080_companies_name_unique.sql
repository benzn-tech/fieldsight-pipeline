-- One company per name, ignoring case and surrounding space (spec 2026-10-03).
-- companies.name has had no uniqueness since 0002; the create endpoint guarded
-- it alone, which a race or any path bypassing the API could defeat. If two
-- rows already share a name this statement FAILS and the deploy stops: a
-- duplicate tenant has to be resolved by a person, never silently.
CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_name_ci
    ON companies (lower(btrim(name)));
