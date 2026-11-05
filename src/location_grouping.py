"""One day's topics and photos, grouped under the places he said out loud
(Track L, Task 3).

The day is a walk. He announces a place ("Level one now"), talks and takes
photos there, announces the next. This module turns that into a list of
groups, in the order he visited them, so the day view (Task 4) and the
assembled report (Task 5) can render "what was found where" instead of one
flat list.

Rules that carry the design:

- WHERE comes from the day's location markers, through
  `location_markers.locate` -- the same locator the day view's photo groups
  use. A marker owns the time after it for CARRY_MINUTES; before the first
  marker, and past that window, the place is None (unknown), never the
  nearest marker.
- A place is keyed by `place_normalise.place_key`, so "Level one" and
  "Level 1" are one place. Only the marker's short `location` string is
  normalised, never a sentence.
- A TOPIC is placed where it STARTED (the start of its time_range). A topic
  that spans two markers stays with the first: he was there when he began
  talking about it; the next marker is a new place, not a split of the topic.
- A topic with no parseable time_range cannot be placed. It goes to a
  `location: None` group -- never into an invented place.
- A PHOTO bound to topics (their `related_photos`) follows the EARLIEST bound
  topic (by topic start; ties by list order; untimed topics rank after every
  timed one) and is listed once, in that group's `photos`. "Bound" is about
  the topic link, not placement: a photo whose earliest topic is untimed goes
  with that topic into a None group's `photos`.
- A photo bound to no topic is placed by its own time (from its filename)
  into that group's `unbound_photos`. A photo whose time cannot be read goes
  to a None group's `unbound_photos`.
- VISIT ORDER is built from the time order of everything placed (topics by
  start, unbound photos by their time), not from the marker list. Consecutive
  items at one place merge into one group; a RETURN to a place after another
  place is a NEW group -- Level 1 -> Level 2 -> Level 1 is three groups,
  because "back on Level 1" is what he did. A place he announced where
  nothing fell gives no group: empty groups are never emitted.
- None is a stop in the walk like any other. The opening None group ("before
  he said where he was") and a later None group ("more than CARRY_MINUTES
  after the last announcement") are separate groups when a place lies between
  them. Items with NO time at all (unparseable time_range, unreadable photo
  time) go in a None group at the END of the walk -- merged into the last
  group if that is already None -- because having no time means having no
  position in the walk, and putting them under the opening group would claim
  they came before the first announcement.

Group shape:
    {"location": label | None,   # canonical label, e.g. "Level 1"
     "kind": kind | None,        # place_normalise kind: level/room/zone/block/other
     "key": place_key | None,    # ADDED to the plan's shape: Task 4 needs a
                                 #   stable identity to compare places by
     "order": int,               # 0-based visit order, contiguous
     "topic_ids": [...],
     "photos": [filename, ...],          # bound to this group's topics
     "unbound_photos": [filename, ...]}  # placed by their own time

A topic's id is its `id`, else its `topic_id`, else -- only when it has
neither -- its index in the `topics` list.

Photos are given as bare filenames or dicts with `s3_key` / `filename`;
they are compared by basename. The `photos` argument is the universe: every
photo in it appears exactly once across all groups, and a `related_photos`
entry naming a file NOT in it is ignored rather than invented into the day.

Markers that are not dicts or have no `at` / `location` are skipped, as
`location_markers.for_day` already skips them on read; dropping one lets the
previous marker's carry run on, which is what the day view does today.

Never raises on bad data; returns None when there are no markers ("nobody
said where they were" stays distinct from a list, as in the day view's
`_photo_groups`).

Pure: no database, no network.
"""
from photo_binding import parse_time_range, photo_time
from place_normalise import _resolve, normalise, place_key
from repositories.location_markers import locate


def _clean_markers(markers):
    return [m for m in (markers or [])
            if isinstance(m, dict) and m.get("at") and isinstance(m.get("location"), str)
            and m["location"].strip()]


def _place(markers, when, aliases):
    """(key, label, kind) of the place at `when`, or None. `markers` must be
    already cleaned."""
    if not when:
        return None
    raw = locate(markers, when)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        key = place_key(raw, aliases)
        # Label from the alias-resolved text, so label, kind and key agree:
        # "the new block" aliased to "Block C" is labelled "Block C".
        got = normalise(_resolve(raw, aliases))
    except ValueError:
        return None
    return key, got["label"], got["kind"]


def place_of(markers, hhmm, *, aliases=None):
    """The canonical label of where he was at `hhmm` ('HH:MM' or 'HH:MM:SS'),
    or None (before the first marker, past the carry window, or unreadable)."""
    got = _place(_clean_markers(markers), hhmm, aliases)
    return got[1] if got else None


def _start(topic):
    """'HH:MM' a topic started, or None when its time_range is unparseable."""
    tr = topic.get("time_range")
    if not isinstance(tr, str):
        return None
    rng = parse_time_range(tr)
    if not rng:
        return None
    return "%02d:%02d" % divmod(rng[0], 60)


def _seconds(when):
    """'HH:MM[:SS]' -> seconds of the day; a minute-only time sits at second
    59, the same convention `locate` uses, so sort order and placement agree."""
    parts = when.split(":")
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + (int(parts[2]) if len(parts) == 3 else 59)


def _basename(photo):
    if isinstance(photo, dict):
        photo = photo.get("filename") or photo.get("s3_key")
    if not isinstance(photo, str) or not photo.strip():
        return None
    return photo.rsplit("/", 1)[-1]


def _topic_id(topic, index):
    if topic.get("id") is not None:
        return topic["id"]
    if topic.get("topic_id") is not None:
        return topic["topic_id"]
    return index


def group_day(markers, topics, photos, *, aliases=None):
    """The day's topics and photos grouped by place, in visit order, or None
    when there are no markers. See the module docstring for every rule."""
    if not markers:
        return None
    marks = _clean_markers(markers)

    # The universe of photos, by basename, first occurrence wins.
    names, seen_names = [], set()
    for p in photos or []:
        n = _basename(p)
        if n and n not in seen_names:
            seen_names.add(n)
            names.append(n)

    # Topics: (index, id, start 'HH:MM' or None, place or None).
    placed_topics = []
    for i, t in enumerate(topics or []):
        if not isinstance(t, dict):
            continue
        start = _start(t)
        placed_topics.append((i, _topic_id(t, i), start,
                              _place(marks, start, aliases) if start else None, t))

    # Each bound photo follows its earliest bound topic.
    def rank(pt):
        return (0, _seconds(pt[2]), pt[0]) if pt[2] else (1, 0, pt[0])

    owner = {}
    for pt in sorted(placed_topics, key=rank):
        related = pt[4].get("related_photos")
        if not isinstance(related, list):
            continue
        for r in related:
            n = _basename(r)
            if n in seen_names and n not in owner:
                owner[n] = pt[0]

    # The walk: everything with a time, in time order; topics before photos
    # at the same instant, then list order.
    timed, untimed = [], []
    for pt in placed_topics:
        item = ("topic", pt[0], pt)
        if pt[2]:
            timed.append((_seconds(pt[2]), 0, pt[0], item))
        else:
            untimed.append(item)
    for j, n in enumerate(names):
        if n in owner:
            continue
        when = photo_time(n)
        if when:
            timed.append((_seconds(when), 1, j, ("photo", n, _place(marks, when, aliases))))
        else:
            untimed.append(("photo", n, None))
    timed.sort(key=lambda x: x[:3])

    groups = []
    group_of_topic = {}

    def group_for(place):
        key = place[0] if place else None
        if groups and groups[-1]["key"] == key:
            return groups[-1]
        g = {"location": place[1] if place else None,
             "kind": place[2] if place else None,
             "key": key, "order": len(groups),
             "topic_ids": [], "photos": [], "unbound_photos": []}
        groups.append(g)
        return g

    def add(item, place):
        g = group_for(place)
        if item[0] == "topic":
            g["topic_ids"].append(item[2][1])
            group_of_topic[item[1]] = g
        else:
            g["unbound_photos"].append(item[1])

    for *_, item in timed:
        add(item, item[2][3] if item[0] == "topic" else item[2])
    for item in untimed:
        add(item, None)

    # Bound photos join their topic's group, in the day's photo order.
    for n in names:
        if n in owner:
            group_of_topic[owner[n]]["photos"].append(n)
    return groups
