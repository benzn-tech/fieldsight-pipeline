"""How many photographs a report carries, at what size, and which.

Owner decisions, 2026-10-01:
  * up to STANDARD_LIMIT (60) photographs go in at STANDARD_EDGE;
  * from 61 to MAX_LIMIT (120) they ALL go in, shrunk automatically to
    COMPACT_EDGE -- nobody is asked;
  * past MAX_LIMIT the person generating the report chooses what to leave out
    (the choice is kept for the day, in S3, and the nightly report follows it
    too); with no choice made, the report takes MAX_LIMIT fairly across the
    day's topics and says how many more were taken.

One module for both report paths -- the template report (lambda_session_report)
and the nightly daily report (lambda_report_generator) -- so the two documents
for one day never carry different photographs by different rules.

The selection lives in S3, not Aurora: the nightly generator runs outside the
VPC and must be able to read it.
"""
import json
import logging
from io import BytesIO

logger = logging.getLogger(__name__)

STANDARD_LIMIT = 60
MAX_LIMIT = 120
STANDARD_EDGE = 1600        # px, long edge
COMPACT_EDGE = 1024
JPEG_QUALITY = 80

try:                         # python-docx-layer:3 carries Pillow
    from PIL import Image, ImageOps
except ImportError:          # pragma: no cover - older layer: photographs go in as taken
    Image = ImageOps = None


def selection_key(folder, date):
    return "report_photo_selection/%s/%s.json" % (folder, date)


def read_excluded(s3, bucket, folder, date):
    """The filenames the day's person chose to leave out of reports, or an
    empty set. A missing or unreadable choice leaves nothing out."""
    try:
        obj = s3.get_object(Bucket=bucket, Key=selection_key(folder, date))
    except s3.exceptions.NoSuchKey:
        return set()
    except Exception:
        logger.warning("photo selection for %s/%s unreadable; leaving nothing out",
                       folder, date, exc_info=True)
        return set()
    try:
        return {str(n) for n in json.loads(obj["Body"].read()).get("excluded") or []}
    except Exception:
        logger.warning("photo selection for %s/%s is not JSON; leaving nothing out",
                       folder, date)
        return set()


def plan(groups, excluded=()):
    """Which photographs a report carries, and at what size.

    `groups` is the day in reading order: [(group_key, [filename, ...]), ...] --
    a topic's photographs, or a location's. A filename in more than one group
    counts once, in its first. Returns (chosen: {group_key: [filenames]},
    edge, left_out) where left_out counts photographs past MAX_LIMIT.

    PAST THE LIMIT, FAIRLY: one from each group in turn, in each group's own
    order, so a long inspection walk cannot crowd every other topic out --
    and every topic with photographs keeps at least one while there is room.
    """
    skip = set(excluded or ())
    seen, ordered = set(), []
    for key, names in groups:
        mine = []
        for n in names or []:
            if n and n not in skip and n not in seen:
                seen.add(n)
                mine.append(n)
        ordered.append((key, mine))
    total = sum(len(m) for _, m in ordered)
    edge = STANDARD_EDGE if total <= STANDARD_LIMIT else COMPACT_EDGE
    if total <= MAX_LIMIT:
        return {k: list(m) for k, m in ordered}, edge, 0
    chosen = {k: [] for k, _ in ordered}
    taken, depth = 0, 0
    while taken < MAX_LIMIT:
        moved = False
        for key, mine in ordered:
            if depth < len(mine) and taken < MAX_LIMIT:
                chosen[key].append(mine[depth])
                taken += 1
                moved = True
        if not moved:
            break
        depth += 1
    # Back into each group's own order: round-robin picks are in pick order.
    for key, mine in ordered:
        keep = set(chosen[key])
        chosen[key] = [n for n in mine if n in keep]
    return chosen, edge, total - taken


def shrink(body, edge=STANDARD_EDGE):
    """A photograph upright, `edge` px on its long side, as JPEG -- or the
    original bytes when Pillow is missing, the file is not an image it can
    read, or shrinking would not make it smaller. Never raises."""
    if Image is None:
        return body
    try:
        img = ImageOps.exif_transpose(Image.open(BytesIO(body)))
        img = img.convert("RGB")
        img.thumbnail((edge, edge))
        out = BytesIO()
        img.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
        small = out.getvalue()
        return small if len(small) < len(body) else body
    except Exception:
        logger.warning("could not shrink a photo; using it as taken", exc_info=True)
        return body
