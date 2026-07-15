-- 0013: optional site street address (mirrors location/client — freeform text).
ALTER TABLE sites ADD COLUMN address text;
