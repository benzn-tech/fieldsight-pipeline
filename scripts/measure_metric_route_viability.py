#!/usr/bin/env python3
"""Can Ask answer a question whose answer is a number? Six queries that decide.

Run BEFORE building the metric route, and again whenever someone doubts a figure
in the spec. Every number in
`docs/superpowers/specs/2026-08-31-ask-answers-with-numbers-design.md` comes from
here, so the spec is re-derivable rather than remembered.

It reads through the RDS Data API, which is enabled on the cluster
(`HttpEndpointEnabled: true`) — Aurora is in-VPC and nothing else here can reach
it. READ ONLY: every statement is a SELECT, and this script must never gain one
that is not.

    AWS_PROFILE=fieldsight-deployer python scripts/measure_metric_route_viability.py
    ... --database fieldsight_test        # the TEST database on the same cluster

WHAT EACH ONE DECIDES

1. row coverage — if days with topics mostly have no `recordings` rows, this is a
   COLLECTION problem in the mobile client and the whole route is premature.
   Measured 84.6% on prod 2026-09-01: viable, but the 15.4% is the "third zero"
   the answer must not report as "you recorded nothing".
2. duration availability — folded to SESSIONS, never rows.
3. the fold ratio itself — 9.8x on prod (2823 audio+video rows, 287 sessions).
   Counting rows would tell a person they made nearly ten times the recordings
   they made. NOT 10.9x: that is 3127/287, and 3127 includes the 304 photos,
   which a session count is not over. This header carried the wrong one for a
   commit, in the very file whose job is to stop numbers being remembered.
4. `findings.domain` NULL rate — 0/189 on prod, which FALSIFIED the spec's
   original argument for reporting a denominator.
5. NULL-author findings — invisible to a SELF-scoped caller.
6. topic source split — decides whether the findings/safety_observations
   fallback is theoretical. It is not: the two paths are disjoint.
"""
import argparse
import json
import subprocess
import sys

CLUSTER = ("arn:aws:rds:ap-southeast-2:509194952652:cluster:"
           "fieldsight-db-test-dbcluster-hywiixu8ihi9")
SECRET = ("arn:aws:secretsmanager:ap-southeast-2:509194952652:secret:"
          "rds!cluster-1757a281-ee31-460d-b56e-950817921010-Ansbey")

QUERIES = [
    ("1. row coverage — days with topics that have any recordings rows", """
WITH topic_days AS (
  SELECT DISTINCT t.report_date::text AS d, u.folder_name AS folder
  FROM topics t JOIN users u ON u.id = t.user_id
  WHERE t.user_id IS NOT NULL AND t.report_date >= current_date - 120),
rec_days AS (
  SELECT DISTINCT substring(s3_key from 'users/([^/]+)/') AS folder,
         substring(s3_key from '/([0-9]{4}-[0-9]{2}-[0-9]{2})/') AS d
  FROM recordings WHERE kind IN ('audio','video'))
SELECT count(*) AS topic_days,
       count(*) FILTER (WHERE r.d IS NOT NULL) AS with_rows,
       round(100.0*count(*) FILTER (WHERE r.d IS NOT NULL)/greatest(count(*),1),1) AS pct
FROM topic_days td LEFT JOIN rec_days r ON r.folder=td.folder AND r.d=td.d"""),

    ("2+3. sessions, the fold, and duration availability", r"""
WITH sess AS (
  SELECT COALESCE(substring(s3_key from '_(sid[0-9a-f]{32})_c[0-9]+\.'), s3_key) AS sid,
         bool_or(duration_s IS NOT NULL AND duration_s > 0) AS has_dur,
         bool_or(ended_at IS NOT NULL AND started_at IS NOT NULL) AS has_span
  FROM recordings WHERE kind IN ('audio','video') GROUP BY 1)
SELECT (SELECT count(*) FROM recordings WHERE kind IN ('audio','video')) AS chunk_rows,
       count(*) AS sessions,
       count(*) FILTER (WHERE has_dur) AS from_duration_s,
       count(*) FILTER (WHERE NOT has_dur AND has_span) AS from_span,
       count(*) FILTER (WHERE NOT has_dur AND NOT has_span) AS unmeasurable
FROM sess"""),

    ("4+5. findings: domain labelling, and NULL-author rows", """
SELECT count(*) AS findings,
       count(*) FILTER (WHERE f.domain IS NULL) AS unlabelled,
       count(*) FILTER (WHERE f.domain='safety') AS safety,
       count(*) FILTER (WHERE f.domain='quality') AS quality,
       count(*) FILTER (WHERE t.user_id IS NULL) AS null_author
FROM findings f JOIN topics t ON t.id = f.topic_id"""),

    ("6. topic source split — is the safety fallback theoretical?", """
SELECT CASE WHEN t.source_s3_key LIKE 'reports/%' THEN 'nightly report'
            ELSE 'live extraction' END AS src,
       count(*) AS topics,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM findings f WHERE f.topic_id=t.id)) AS with_findings,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM safety_observations s WHERE s.topic_id=t.id)) AS with_safety_obs
FROM topics t WHERE t.report_date >= current_date - 120 GROUP BY 1 ORDER BY 1"""),
]


def run(sql, database):
    out = subprocess.run(
        ["aws", "rds-data", "execute-statement", "--region", "ap-southeast-2",
         "--resource-arn", CLUSTER, "--secret-arn", SECRET,
         "--database", database, "--sql", sql, "--output", "json"],
        capture_output=True, text=True)
    if out.returncode != 0:
        return None, (out.stderr or "").strip()[:300]
    d = json.loads(out.stdout)
    rows = []
    for rec in d.get("records", []):
        rows.append(["NULL" if "isNull" in c else str(list(c.values())[0]) for c in rec])
    return rows, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--database", default="fieldsight",
                    help="fieldsight (prod) or fieldsight_test")
    a = ap.parse_args()

    print(f"database: {a.database}\n")
    failed = 0
    for title, sql in QUERIES:
        assert sql.strip().upper().startswith(("SELECT", "WITH")), \
            "read-only: this script may only SELECT"
        print(title)
        rows, err = run(sql, a.database)
        if err:
            print("   ERROR:", err)
            failed += 1
            continue
        if not rows:
            print("   (no rows)")
        for r in rows:
            print("   " + " | ".join(r))
        print()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
