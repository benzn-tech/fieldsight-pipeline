#!/usr/bin/env python3
"""How often does the inspector say where he is? (Track L, Task 1 and Task 8.)

Plan: docs/superpowers/plans/2026-09-25-track-l-photos-and-topics-by-location.md

READ ONLY. Every statement is a SELECT, through the RDS Data API (Aurora is
in-VPC; the Data API is enabled on the cluster). This script must never gain a
statement that is not a SELECT.

    AWS_PROFILE=fieldsight-deployer python scripts/measure_location_markers.py
    ... --database fieldsight_test --days 30 --strings-out <local file>

WHAT IS PRINTED (and may be committed): counts only. Companies appear as C1..Cn,
ordered by topic-days; the C-label -> company mapping and the raw location
strings go ONLY to --strings-out, which must point outside the repo (the raw
strings are customer speech).

DEFINITIONS
- topic-day: a (folder, report_date) with at least one live (not superseded)
  topic whose source is an extraction or a nightly report of that folder.
- photo-day: a topic-day that also has at least one photo row. A photo means
  someone was walking with the camera; this is the closer proxy for an
  inspection day than "any topic", which also counts meetings and phone calls.
- marker-day: a day_location_markers row with at least one usable marker.
- photo placed: `location_markers.locate` (what the day view groups by) gives a
  place. `photo_binding.place_at` (what binding and the template report use) is
  counted beside it: two locators exist, and if they disagree that is a finding.
- topic placed: `locate` at the start of the topic's time_range.
"""
import argparse
import json
import os
import statistics

import boto3
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from photo_binding import photo_time, place_at  # noqa: E402
from repositories.location_markers import locate  # noqa: E402

CLUSTER = ("arn:aws:rds:ap-southeast-2:509194952652:cluster:"
           "fieldsight-db-test-dbcluster-hywiixu8ihi9")
SECRET = ("arn:aws:secretsmanager:ap-southeast-2:509194952652:secret:"
          "rds!cluster-1757a281-ee31-460d-b56e-950817921010-Ansbey")

# folder of a topic: extractions/{folder}/{date}/... or reports/{date}/{folder}/...
SQL_TOPICS = r"""
SELECT s.company_id::text,
       COALESCE(substring(t.source_s3_key from '^extractions/([^/]+)/'),
                substring(t.source_s3_key from '^reports/[0-9-]+/([^/]+)/')) AS folder,
       t.report_date::text,
       string_agg(COALESCE(substring(t.time_range from '([0-9]{{1,2}}:[0-9]{{2}})'), '-'), ',')
FROM topics t JOIN sites s ON s.id = t.site_id
WHERE t.report_date >= current_date - {days}
  AND t.superseded_at IS NULL
  AND (t.source_s3_key LIKE 'extractions/%' OR t.source_s3_key LIKE 'reports/%')
GROUP BY 1, 2, 3"""

SQL_MARKERS = """
SELECT company_id::text, user_folder, report_date::text, markers::text
FROM day_location_markers WHERE report_date >= current_date - {days}"""

SQL_PHOTOS = r"""
SELECT company_id::text, substring(s3_key from 'users/([^/]+)/') AS folder,
       substring(s3_key from '/([0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})/') AS d,
       regexp_replace(s3_key, '^.*/', '') AS name
FROM recordings
WHERE kind = 'photo'
  AND substring(s3_key from '/([0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})/') >= (current_date - {days})::text"""

SQL_COMPANIES = "SELECT id::text, name FROM companies"


def run(sql, database):
    assert sql.strip().upper().startswith(("SELECT", "WITH")), "read-only: SELECT only"
    d = boto3.client("rds-data", region_name="ap-southeast-2").execute_statement(
        resourceArn=CLUSTER, secretArn=SECRET, database=database, sql=sql)
    return [[None if c.get("isNull") else list(c.values())[0] for c in rec]
            for rec in d.get("records", [])]


def _pct(n, d):
    return "%.1f%%" % (100.0 * n / d) if d else "n/a"


def _q(values, q):
    if not values:
        return "n/a"
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def measure(database, days):
    topics = run(SQL_TOPICS.format(days=int(days)), database)
    markers = run(SQL_MARKERS.format(days=int(days)), database)
    photos = run(SQL_PHOTOS.format(days=int(days)), database)
    companies = dict(run(SQL_COMPANIES, database))

    topic_days = defaultdict(list)            # (company, folder, date) -> [start]
    for company, folder, d, starts in topics:
        if folder:
            topic_days[(company, folder, d)].extend(
                None if x == "-" else x.zfill(5) for x in (starts or "").split(","))
    marks = {}
    for company, folder, d, raw in markers:
        ms = [m for m in json.loads(raw or "[]")
              if isinstance(m, dict) and m.get("at") and m.get("location")]
        if ms:
            marks[(company, folder, d)] = ms
    day_photos = defaultdict(list)
    for company, folder, d, name in photos:
        if folder and d:
            day_photos[(company, folder, d)].append(name)

    order = Counter(k[0] for k in topic_days)
    label = {c: "C%d" % (i + 1) for i, (c, _) in enumerate(order.most_common())}
    report, raw_strings = [], Counter()
    per = defaultdict(Counter)
    per_counts = defaultdict(list)
    for key, starts in topic_days.items():
        c = per[key[0]]
        ms = marks.get(key, [])
        ph = day_photos.get(key, [])
        c["topic_days"] += 1
        c["photo_days"] += bool(ph)
        c["marker_days"] += bool(ms)
        c["marker_photo_days"] += bool(ms and ph)
        c["topics"] += len(starts)
        c["topics_placed"] += sum(1 for s in starts if s and locate(ms, s))
        c["photos"] += len(ph)
        c["photos_on_marker_days"] += len(ph) if ms else 0
        for n in ph:
            t = photo_time(n)
            a, b = locate(ms, t), place_at(ms, t)
            c["photos_placed_locate"] += bool(a)
            c["photos_placed_place_at"] += bool(b)
            c["locators_disagree"] += bool(a) != bool(b)
        if ms:
            per_counts[key[0]].append(len(ms))
            for m in ms:
                raw_strings[(" ".join(str(m["location"]).split()))] += 1
    # Marker rows on days with no live topic (re-extracted away, or a folder
    # whose topics are filed elsewhere): counted, not silently dropped.
    orphan_marker_days = sum(1 for k in marks if k not in topic_days)

    for cid in sorted(per, key=lambda c: label[c]):
        c = per[cid]
        n = per_counts[cid]
        report.append(
            "%-4s topic-days %4d | photo-days %4d | marker-days %4d (%s of topic-days, %s of photo-days) "
            "| markers/day p50 %s p90 %s | topics placed %s | photos %d (%d on marker-days), placed %s (locate) / %s "
            "(place_at), disagree %d" % (
                label[cid], c["topic_days"], c["photo_days"], c["marker_days"],
                _pct(c["marker_days"], c["topic_days"]), _pct(c["marker_photo_days"], c["photo_days"]),
                _q(n, .5), _q(n, .9), _pct(c["topics_placed"], c["topics"]),
                c["photos"], c["photos_on_marker_days"], _pct(c["photos_placed_locate"], c["photos"]),
                _pct(c["photos_placed_place_at"], c["photos"]), c["locators_disagree"]))
    tot = Counter()
    for c in per.values():
        tot.update(c)
    allm = [x for v in per_counts.values() for x in v]
    report.append(
        "ALL  topic-days %d | photo-days %d | marker-days %d (%s of topic-days, %s of photo-days) "
        "| markers/day p50 %s p90 %s | topics placed %s | photos %d (%d on marker-days), placed %s (locate) / %s "
        "(place_at), disagree %d | marker rows with no live topic that day: %d | distinct raw strings: %d" % (
            tot["topic_days"], tot["photo_days"], tot["marker_days"],
            _pct(tot["marker_days"], tot["topic_days"]), _pct(tot["marker_photo_days"], tot["photo_days"]),
            _q(allm, .5), _q(allm, .9), _pct(tot["topics_placed"], tot["topics"]), tot["photos"], tot["photos_on_marker_days"],
            _pct(tot["photos_placed_locate"], tot["photos"]),
            _pct(tot["photos_placed_place_at"], tot["photos"]), tot["locators_disagree"],
            orphan_marker_days, len(raw_strings)))
    private = ["labels:"] + ["  %s = %s" % (label[c], companies.get(c, c)) for c in sorted(label, key=label.get)]
    private += ["", "top 50 raw location strings (customer speech; never commit):"]
    private += ["  %4d  %s" % (n, s) for s, n in raw_strings.most_common(50)]
    return report, private


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--database", default="fieldsight", help="fieldsight (prod) or fieldsight_test")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--strings-out", required=True,
                    help="LOCAL file, outside the repo, for the label map and the raw strings")
    a = ap.parse_args()
    repo = os.path.abspath(os.path.join(HERE, ".."))
    if os.path.abspath(a.strings_out).startswith(repo):
        raise SystemExit("--strings-out must be outside the repo: it holds customer speech")
    report, private = measure(a.database, a.days)
    print("database %s, last %d days\n" % (a.database, a.days))
    print("\n".join(report))
    with open(a.strings_out, "w", encoding="utf-8") as f:
        f.write("\n".join(private) + "\n")
    print("\nlabel map + raw strings written to %s" % a.strings_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
