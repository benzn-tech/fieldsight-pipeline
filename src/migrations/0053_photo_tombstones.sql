-- Photos become a first-class deletion target.
--
-- Until now a photo could only disappear as a SIDE EFFECT: photos are visible
-- solely as `related_photos` on a topic, so hiding the topic hid them. Any
-- surface that lists photos from `recordings` directly -- which is what the day
-- view needs in order to survive an extraction outage -- bypasses the only
-- mechanism that hides them, and would put deleted photos back on screen.
--
-- 'photo' rather than reusing 'recording': the two are tombstoned by different
-- keys and answer different questions. A recording tombstone names an
-- extraction/source PREFIX (`extractions/{folder}/{date}/sid{hex}`) and is
-- matched with LIKE against `source_s3_key`; a photo tombstone names ONE object
-- (`users/{folder}/pictures/{date}/IMG.jpg`) exactly. Folding them into one
-- type would make every reader guess which comparison it wanted.
--
-- It stays in `redactions` rather than a new table so that revert stays
-- symmetric for free: `revert_batch` restores by batch_id, and a photo hidden
-- by a session deletion must come back when that deletion is undone. A separate
-- table would have needed its own revert, and the day it drifted the customer
-- would get a restore that left their photos hidden.
ALTER TABLE redactions DROP CONSTRAINT IF EXISTS redactions_target_type_check;
ALTER TABLE redactions ADD CONSTRAINT redactions_target_type_check
  CHECK (target_type IN ('topic', 'segment', 'finding', 'recording', 'photo'));
