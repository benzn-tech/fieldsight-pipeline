"""Unit tests for the Task 10 owner-labelled batch: sampler, label page, and
importer (Track A, Jev shadow eval).

All of this runs against synthetic in-memory data -- no database, no `aws`
call. `tests/integration/test_jev_eval_sample_batch_sql.py` covers the SQL
itself against a real Postgres.
"""
import json

import pytest

from scripts.jev_eval import import_labels as il
from scripts.jev_eval import label_page as lp
from scripts.jev_eval import sample_batch as sb


# ---------------------------------------------------------------------------
# SQL shape
# ---------------------------------------------------------------------------

def test_every_sql_string_starts_with_select_or_with():
    sqls = [
        sb.sql_threads_site_ids(120),
        sb.sql_threads_topics_for_site("11111111-1111-1111-1111-111111111111", 120, 200),
        sb.sql_threads_existing_pairs(),
        sb.sql_work_class_existing(),
    ]
    for work_class, low_confidence in sb.WORK_CLASS_STRATA:
        sqls.append(sb.sql_work_class_topics_stratum(work_class, low_confidence, 365, 0, 500))
    for sql in sqls:
        stripped = sql.strip().upper()
        assert stripped.startswith("SELECT") or stripped.startswith("WITH"), sql


def test_sql_threads_topics_for_site_filters_site_and_bounds_response():
    sql = sb.sql_threads_topics_for_site("site-1", 120, 200)
    assert "t.site_id = 'site-1'" in sql
    assert "left(t.summary, 1000)" in sql
    assert "LIMIT 200" in sql
    assert "ORDER BY t.report_date DESC, t.id DESC" in sql


def test_sql_work_class_topics_stratum_filters_class_confidence_and_bounds_response():
    sql_low = sb.sql_work_class_topics_stratum("work", True, 365, 7, 500)
    assert "t.work_class = 'work'" in sql_low
    assert "t.work_confidence < 0.8" in sql_low
    assert "left(t.summary, 1000)" in sql_low
    assert "LIMIT 500" in sql_low
    assert "'7'" in sql_low

    sql_high = sb.sql_work_class_topics_stratum("non_work", False, 365, 7, 500)
    assert "t.work_class = 'non_work'" in sql_high
    assert "work_confidence IS NULL OR t.work_confidence >= 0.8" in sql_high


# ---------------------------------------------------------------------------
# threads: synthetic topic pool
# ---------------------------------------------------------------------------

def _topic(id_, title, summary, report_date, site_id="s1", open_items=1, company_id="c1"):
    return {
        "id": id_, "title": title, "summary": summary, "report_date": report_date,
        "site_id": site_id, "open_items": open_items, "company_id": company_id,
    }


def _synthetic_thread_pool():
    """A pool engineered to land in all three strata:
    - t2/t1: same distinctive subject ("door hardware"), both have open work,
      21 day gap -> high stratum (score >= thread_match.MIN_SCORE).
    - t4/t3: weaker overlap, still eligible -> mid stratum.
    - t6/t5: below the lowered floor, but share a title token ("documentation");
      both sides carry open work and the gap (39 days) sits comfortably under
      thread_match.MAX_GAP_DAYS (45) -- i.e. this pair meets find_candidates'
      FULL eligibility, it just scores below LOWERED_FLOOR. This is what a
      genuine low-stratum hard negative looks like: the matcher COULD have
      proposed it, and would have said no on the words alone.
    """
    return [
        _topic("t1", "Door hardware ordered", "Handles and hinges ordered for level 2.",
               "2026-05-01", open_items=1),
        _topic("t2", "Door hardware installed", "Handles and hinges fitted on level 2.",
               "2026-05-22", open_items=1),
        _topic("t3", "Floor box location confirmed", "Floor box position agreed with electrician.",
               "2026-05-10", open_items=1),
        _topic("t4", "Floor box installed downstairs", "Box fitted downstairs near the meter.",
               "2026-05-30", open_items=1),
        _topic("t5", "Documentation review scaffold permit safety induction paperwork "
                     "toolbox crane inspection ladder harness",
               "General discussion scaffold permits induction paperwork roster crane "
               "lift plan review harness checklist survey.",
               "2026-04-01", open_items=1),
        _topic("t6", "Documentation follow-up weather delay reporting insurance claim "
                     "concrete pour timeline budget variance",
               "Follow-up weather delay reporting insurance claim concrete pour timeline "
               "update summary budget forecast variance review.",
               "2026-05-10", open_items=1),
    ]


def test_generate_thread_pairs_produces_all_three_strata():
    topics = _synthetic_thread_pool()
    pairs, diagnostics = sb.generate_thread_pairs(sb.group_by_site(topics))
    strata = {sb._stratum_for_score(p["score"]) for p in pairs}
    assert "high" in strata or "mid" in strata  # eligible pairs found
    assert "low" in strata  # eligible-but-below-floor pair found
    low_pairs = [p for p in pairs if sb._stratum_for_score(p["score"]) == "low"]
    assert any(p["later"]["id"] == "t6" and p["earlier"]["id"] == "t5" for p in low_pairs)
    assert "s1" in diagnostics
    assert diagnostics["s1"]["n_topics"] == len(topics)
    assert diagnostics["s1"]["low_pairs_scored"] > 0


def test_generate_thread_pairs_low_stratum_respects_full_eligibility():
    # Same base pair as above, but t6 now sits 100 days after t5 -- beyond
    # thread_match.MAX_GAP_DAYS (45) -- so it must never appear in ANY
    # stratum, low included.
    topics = _synthetic_thread_pool()
    for t in topics:
        if t["id"] == "t6":
            t["report_date"] = "2026-07-10"  # ~100 days after t5's 2026-04-01
    pairs, _ = sb.generate_thread_pairs(sb.group_by_site(topics))
    assert not any(p["later"]["id"] == "t6" and p["earlier"]["id"] == "t5" for p in pairs)

    # A later topic with zero open_items contributes NO low-stratum pairs at
    # all, no matter how much title vocabulary it shares.
    topics2 = _synthetic_thread_pool()
    for t in topics2:
        if t["id"] == "t6":
            t["open_items"] = 0
    pairs2, _ = sb.generate_thread_pairs(sb.group_by_site(topics2))
    assert not any(p["later"]["id"] == "t6" for p in pairs2)

    # Fix round 2: an EARLIER topic with zero open_items must ALSO never
    # appear in any stratum -- find_candidates (src/thread_match.py:173)
    # would never propose it, and candidate_corpus's own SQL (src/
    # repositories/threads.py, HAVING open_items > 0) would never have put
    # it in the corpus at all, so it is not a hard negative -- it is a pair
    # the real matcher can never see.
    topics3 = _synthetic_thread_pool()
    for t in topics3:
        if t["id"] == "t5":
            t["open_items"] = 0
    pairs3, _ = sb.generate_thread_pairs(sb.group_by_site(topics3))
    assert not any(p["earlier"]["id"] == "t5" for p in pairs3)


def test_stratify_thread_pairs_fills_quotas_and_is_deterministic():
    topics = _synthetic_thread_pool()
    # Duplicate the pool across a few more synthetic sites so each stratum
    # has more than enough candidates to fill a small quota.
    all_topics = []
    for i in range(4):
        for t in _synthetic_thread_pool():
            t2 = dict(t)
            t2["id"] = f"{t['id']}-{i}"
            t2["site_id"] = f"site-{i}"
            all_topics.append(t2)

    pairs, _ = sb.generate_thread_pairs(sb.group_by_site(all_topics))
    chosen_a, counts_a = sb.stratify_thread_pairs(pairs, size=10, seed=42)
    chosen_b, counts_b = sb.stratify_thread_pairs(pairs, size=10, seed=42)

    ids_a = [(p["later"]["id"], p["earlier"]["id"], s) for p, s in chosen_a]
    ids_b = [(p["later"]["id"], p["earlier"]["id"], s) for p, s in chosen_b]
    assert ids_a == ids_b  # same seed -> same sample
    assert counts_a == counts_b

    total_sampled = sum(v["sampled"] for v in counts_a.values())
    assert total_sampled <= 10
    assert counts_a["low"]["sampled"] > 0  # low stratum has candidates and gets some


def test_stratify_thread_pairs_different_seed_can_differ():
    all_topics = []
    for i in range(4):
        for t in _synthetic_thread_pool():
            t2 = dict(t)
            t2["id"] = f"{t['id']}-{i}"
            t2["site_id"] = f"site-{i}"
            all_topics.append(t2)
    pairs, _ = sb.generate_thread_pairs(sb.group_by_site(all_topics))
    chosen_1, _ = sb.stratify_thread_pairs(pairs, size=10, seed=1)
    chosen_2, _ = sb.stratify_thread_pairs(pairs, size=10, seed=2)
    ids_1 = [(p["later"]["id"], p["earlier"]["id"]) for p, _ in chosen_1]
    ids_2 = [(p["later"]["id"], p["earlier"]["id"]) for p, _ in chosen_2]
    assert ids_1 != ids_2


def test_thread_pair_id_is_stable_and_direction_sensitive():
    id_1 = sb.thread_pair_id("later-uuid", "earlier-uuid")
    id_2 = sb.thread_pair_id("later-uuid", "earlier-uuid")
    assert id_1 == id_2
    id_swapped = sb.thread_pair_id("earlier-uuid", "later-uuid")
    assert id_swapped != id_1


def test_apply_thread_exclusions_drops_already_suggested_pairs():
    topics = _synthetic_thread_pool()
    pairs, _ = sb.generate_thread_pairs(sb.group_by_site(topics))
    assert pairs  # sanity: the pool does produce pairs
    some_pair = pairs[0]
    existing = {(some_pair["later"]["id"], some_pair["earlier"]["id"])}
    filtered = sb.apply_thread_exclusions(pairs, existing)
    assert len(filtered) == len(pairs) - 1
    assert all(
        (p["later"]["id"], p["earlier"]["id"]) not in existing for p in filtered
    )


# ---------------------------------------------------------------------------
# threads: cost caps (fix round 1, Important #2)
# ---------------------------------------------------------------------------

def _many_topics(n, site_id="s1"):
    return [
        _topic(f"m{i}", f"Topic number {i} about widgets and gadgets",
               f"Summary text number {i} describing widgets and gadgets in detail today.",
               f"2026-05-{(i % 27) + 1:02d}", site_id=site_id, open_items=1)
        for i in range(n)
    ]


def test_cap_topics_per_site_keeps_most_recent_n():
    topics = [
        _topic("old", "Old topic", "old", "2026-01-01"),
        _topic("mid", "Mid topic", "mid", "2026-03-01"),
        _topic("new", "New topic", "new", "2026-06-01"),
    ]
    capped = sb.cap_topics_per_site(sb.group_by_site(topics), max_topics_per_site=2)
    kept_ids = {t["id"] for t in capped["s1"]}
    assert kept_ids == {"mid", "new"}


def test_cap_topics_per_site_is_a_noop_under_the_limit():
    topics = _synthetic_thread_pool()
    capped = sb.cap_topics_per_site(sb.group_by_site(topics), max_topics_per_site=200)
    assert len(capped["s1"]) == len(topics)


def test_generate_thread_pairs_bounds_low_stratum_score_pair_calls():
    # 40 topics on one site, all eligible for each other (open_items=1, all
    # within a month) -- without a cap this would score up to 40*39 pairs
    # per later topic's eligible set; with max_pairs_per_topic=5 it must
    # score at most 5 per later topic.
    topics = _many_topics(40)
    pairs, diagnostics = sb.generate_thread_pairs(
        sb.group_by_site(topics), max_pairs_per_topic=5, seed=0)
    n_topics = len(topics)
    assert diagnostics["s1"]["low_pairs_scored"] <= n_topics * 5


def test_generate_thread_pairs_low_stratum_sampling_is_deterministic():
    topics = _many_topics(30)
    pairs_a, diag_a = sb.generate_thread_pairs(
        sb.group_by_site(topics), max_pairs_per_topic=4, seed=99)
    pairs_b, diag_b = sb.generate_thread_pairs(
        sb.group_by_site(topics), max_pairs_per_topic=4, seed=99)
    ids_a = sorted((p["later"]["id"], p["earlier"]["id"]) for p in pairs_a)
    ids_b = sorted((p["later"]["id"], p["earlier"]["id"]) for p in pairs_b)
    assert ids_a == ids_b
    assert diag_a == diag_b


def test_sample_threads_caps_are_wired_through_cli(monkeypatch, tmp_path):
    # Exercise the full sample_threads() pipeline with tiny caps, using fake
    # DB calls, to prove main()'s --max-topics-per-site/--max-pairs-per-topic
    # actually reach generate_thread_pairs/cap_topics_per_site rather than
    # only existing as unused CLI flags. Batch output redirected to tmp_path
    # so this never touches the real scripts/fixtures/jev_eval/batch/ dir.
    from scripts.jev_eval import export_labels as ex

    monkeypatch.setattr(sb, "BATCH_DIR", tmp_path / "batch")

    topics = _many_topics(10)

    def fake_begin_transaction(database, profile, region):
        return "tx"

    calls = {"n": 0}

    def fake_execute(database, tx, sql, profile, region):
        calls["n"] += 1
        if sql.startswith("SELECT DISTINCT t.site_id"):
            return {"records": [[{"stringValue": "s1"}]]}
        if "topic_thread_suggestions" in sql:
            return {"records": []}
        # the per-site topics query (sql_threads_topics_for_site) -- the fake
        # ignores the site filter/LIMIT clause itself, since capping is what
        # this test is checking downstream via cap_topics_per_site.
        records = []
        for t in topics:
            records.append([
                {"stringValue": t["id"]}, {"stringValue": t["report_date"]},
                {"stringValue": t["site_id"]}, {"stringValue": t["company_id"]},
                {"stringValue": t["title"]}, {"stringValue": t["summary"]},
                {"longValue": t["open_items"]},
            ])
        return {"records": records}

    def fake_rollback(tx, profile, region):
        return None

    monkeypatch.setattr(ex, "_begin_transaction", fake_begin_transaction)
    monkeypatch.setattr(ex, "_execute", fake_execute)
    monkeypatch.setattr(ex, "_rollback", fake_rollback)

    result = sb.sample_threads(
        "fieldsight_test", size=5, seed=0,
        max_topics_per_site=3, max_pairs_per_topic=2)

    assert result["max_topics_per_site"] == 3
    assert result["max_pairs_per_topic"] == 2
    assert result["per_site_diagnostics"]["s1"]["n_topics"] == 3


# ---------------------------------------------------------------------------
# work_class: synthetic topic pool
# ---------------------------------------------------------------------------

def _wc_topic(id_, work_class, confidence, site_id="s1", company_id="c1"):
    return {
        "id": id_, "title": f"Topic {id_}", "summary": "summary", "category": "general",
        "work_class": work_class, "work_confidence": confidence,
        "site_id": site_id, "company_id": company_id,
    }


def test_apply_work_class_exclusions_drops_fed_back_topics():
    topics = [_wc_topic("w1", "work", 0.9), _wc_topic("w2", "non_work", 0.5)]
    filtered = sb.apply_work_class_exclusions(topics, already_fed_back={"w1"})
    assert [t["id"] for t in filtered] == ["w2"]


def test_stratify_work_class_is_fifty_fifty_and_oversamples_low_confidence():
    topics = (
        [_wc_topic(f"work-hi-{i}", "work", 0.95) for i in range(10)]
        + [_wc_topic(f"work-lo-{i}", "work", 0.4) for i in range(3)]
        + [_wc_topic(f"nonwork-hi-{i}", "non_work", 0.95) for i in range(10)]
        + [_wc_topic(f"nonwork-lo-{i}", "non_work", 0.3) for i in range(3)]
    )
    chosen, counts = sb.stratify_work_class(topics, size=10, seed=7)
    labels = [t["work_class"] for t, _ in chosen]
    assert labels.count("work") == 5
    assert labels.count("non_work") == 5

    work_ids = [t["id"] for t, s in chosen if s == "work"]
    # all 3 low-confidence "work" topics must be included before any high-confidence one
    assert all(i.startswith("work-lo") for i in work_ids[:3])


def test_stratify_work_class_deterministic_with_seed():
    topics = (
        [_wc_topic(f"w{i}", "work", 0.95) for i in range(6)]
        + [_wc_topic(f"n{i}", "non_work", 0.95) for i in range(6)]
    )
    chosen_a, _ = sb.stratify_work_class(topics, size=6, seed=3)
    chosen_b, _ = sb.stratify_work_class(topics, size=6, seed=3)
    assert [t["id"] for t, _ in chosen_a] == [t["id"] for t, _ in chosen_b]


# ---------------------------------------------------------------------------
# batch row shape
# ---------------------------------------------------------------------------

def test_build_thread_batch_row_shape():
    topics = _synthetic_thread_pool()
    pairs, _ = sb.generate_thread_pairs(sb.group_by_site(topics))
    pair = pairs[0]
    row = sb.build_thread_batch_row(pair, "high", topics)
    assert row["set"] == "threads"
    assert row["label"] is None
    assert row["label_source"] == "owner"
    assert set(row["features"]) == {"earlier", "later", "gap_days"}
    assert set(row["baseline"]) == {"score", "top_hit"}
    assert "stratum" in row and "stratum" not in row["features"]


# ---------------------------------------------------------------------------
# I6: thread baseline fidelity -- recomputed under the DEPLOYED gate's corpus
# ---------------------------------------------------------------------------

def test_deployed_corpus_for_matches_candidate_corpus_eligibility():
    # Same pool as candidate_corpus would apply: same site, earlier, within
    # MAX_GAP_DAYS, open_items > 0. A topic outside the gap, or with no open
    # items, or on/after the later date, must be excluded.
    later = _topic("t_later", "Door hardware installed", "Handles fitted.",
                    "2026-06-15", open_items=1)
    in_window = _topic("t_in", "Door hardware ordered", "Order placed.",
                        "2026-06-01", open_items=1)
    too_old = _topic("t_old", "Door hardware first raised", "First mention.",
                      "2026-01-01", open_items=1)
    no_open_items = _topic("t_zero", "Door hardware note", "No open work.",
                            "2026-06-05", open_items=0)
    same_day = _topic("t_same", "Door hardware same day", "Same day note.",
                       "2026-06-15", open_items=1)
    later_topic = _topic("t_future", "Door hardware future", "Future note.",
                          "2026-06-20", open_items=1)
    pool = [later, in_window, too_old, no_open_items, same_day, later_topic]

    corpus = sb.deployed_corpus_for(later, pool)
    ids = {t["id"] for t in corpus}
    assert ids == {"t_in"}


def test_deployed_thread_baseline_top_hit_true_when_earlier_is_the_best_candidate():
    later = _topic("t2", "Door hardware installed", "Handles and hinges fitted on level 2.",
                    "2026-05-22", open_items=1)
    earlier = _topic("t1", "Door hardware ordered", "Handles and hinges ordered for level 2.",
                      "2026-05-01", open_items=1)
    distractor = _topic("t3", "Unrelated concrete pour", "Concrete pour scheduling notes.",
                         "2026-05-10", open_items=1)
    site_topics = [later, earlier, distractor]
    pair = {"later": later, "earlier": earlier, "score": 0.9, "gap_days": 21}

    baseline = sb.deployed_thread_baseline(pair, site_topics)
    assert baseline["top_hit"] is True
    assert isinstance(baseline["score"], float)


def test_deployed_thread_baseline_top_hit_false_when_a_better_candidate_exists():
    later = _topic("t2", "Door hardware installed", "Handles and hinges fitted on level 2.",
                    "2026-05-22", open_items=1)
    weak_earlier = _topic("t1", "Something else entirely", "Totally unrelated text about paint.",
                           "2026-05-01", open_items=1)
    strong_earlier = _topic("t3", "Door hardware ordered handles hinges", "Handles hinges ordered.",
                             "2026-05-10", open_items=1)
    site_topics = [later, weak_earlier, strong_earlier]
    pair = {"later": later, "earlier": weak_earlier, "score": 0.01, "gap_days": 21}

    baseline = sb.deployed_thread_baseline(pair, site_topics)
    assert baseline["top_hit"] is False


def test_build_work_class_batch_row_shape():
    topic = _wc_topic("w1", "non_work", 0.4)
    row = sb.build_work_class_batch_row(topic, "non_work")
    assert row["set"] == "work_class"
    assert row["label"] is None
    assert row["baseline"] == {"classifier_verdict": "non_work", "classifier_confidence": 0.4}
    assert set(row["features"]) == {"title", "summary", "category"}


# ---------------------------------------------------------------------------
# I2: labelling order does not leak the stratum
# ---------------------------------------------------------------------------

def _longest_run(values: list) -> int:
    best = cur = 0
    prev = object()
    for v in values:
        cur = cur + 1 if v == prev else 1
        best = max(best, cur)
        prev = v
    return best


def test_order_for_labelling_breaks_up_stratum_runs():
    rows = (
        [{"id": f"high-{i}", "stratum": "high"} for i in range(20)]
        + [{"id": f"mid-{i}", "stratum": "mid"} for i in range(20)]
        + [{"id": f"low-{i}", "stratum": "low"} for i in range(20)]
    )
    ordered = sb.order_for_labelling(rows, seed=42)
    assert {r["id"] for r in ordered} == {r["id"] for r in rows}
    strata_sequence = [r["stratum"] for r in ordered]
    # A perfectly-shuffled 60-row/3-class sequence should never keep an
    # entire class run intact (20) -- a small bound well under that catches
    # "still grouped by stratum" without demanding exact uniform mixing.
    assert _longest_run(strata_sequence) <= 8


def test_order_for_labelling_is_deterministic_for_a_seed():
    rows = [{"id": f"r{i}", "stratum": "high"} for i in range(10)]
    ordered_a = sb.order_for_labelling(rows, seed=7)
    ordered_b = sb.order_for_labelling(rows, seed=7)
    assert [r["id"] for r in ordered_a] == [r["id"] for r in ordered_b]


def test_order_for_labelling_different_seed_can_differ():
    rows = [{"id": f"r{i}", "stratum": "high"} for i in range(10)]
    ordered_1 = [r["id"] for r in sb.order_for_labelling(rows, seed=1)]
    ordered_2 = [r["id"] for r in sb.order_for_labelling(rows, seed=2)]
    assert ordered_1 != ordered_2


# ---------------------------------------------------------------------------
# label_page.py
# ---------------------------------------------------------------------------

def _batch_rows_for_page():
    return [
        {
            "set": "threads", "id": "threads:aaa", "label": None, "label_source": "owner",
            "features": {"earlier": {"title": "A"}, "later": {"title": "B"}, "gap_days": 5},
            "display": {"earlier_title": "Door hardware ordered",
                        "later_title": "Door hardware installed", "gap_days": 21},
            "site_id": "s1", "company_id": "c1", "baseline": {"score": 0.61}, "stratum": "high",
        },
        {
            "set": "threads", "id": "threads:bbb", "label": None, "label_source": "owner",
            "features": {"earlier": {"title": "C"}, "later": {"title": "D"}, "gap_days": 9},
            "display": {"earlier_title": "Floor box ordered",
                        "later_title": "Floor box installed", "gap_days": 20},
            "site_id": "s1", "company_id": "c1", "baseline": {"score": 0.31}, "stratum": "mid",
        },
    ]


def test_page_contains_every_batch_id():
    rows = _batch_rows_for_page()
    html = lp.build_html("threads", rows)
    for row in rows:
        assert row["id"] in html


def test_page_has_no_http_url():
    rows = _batch_rows_for_page()
    html = lp.build_html("threads", rows)
    assert "http://" not in html
    assert "https://" not in html


def test_page_escapes_closing_script_tag_in_customer_text():
    rows = [
        {
            "set": "threads", "id": "threads:zzz", "label": None, "label_source": "owner",
            "features": {}, "display": {"earlier_title": "Say </script><b>hi</b> now"},
            "site_id": "s1", "company_id": "c1", "baseline": {"score": 0.1}, "stratum": "low",
        },
    ]
    html = lp.build_html("threads", rows)
    assert "</script><b>" not in html
    assert "<\\/script>" in html
    # The page must still have exactly the two real <script> tags (open/close
    # of the one inline block) -- the embedded text must not add a third.
    assert html.count("<script>") == 1
    assert html.count("</script>") == 1


def test_page_never_shows_stratum_or_score():
    rows = _batch_rows_for_page()
    html = lp.build_html("threads", rows)
    assert "stratum" not in html.lower()
    assert "0.61" not in html
    assert "0.31" not in html
    assert "baseline" not in html.lower()


def test_page_shows_question_and_work_class_note():
    rows = [
        {"id": "w1", "display": {"title": "T", "summary": "S", "category": "general"}},
    ]
    html = lp.build_html("work_class", rows)
    assert "NOT work" in html
    assert lp.QUESTION_TEXT["work_class"].split("\n")[0] in html


def test_build_items_payload_excludes_features_and_baseline():
    rows = _batch_rows_for_page()
    items = lp.build_items_payload(rows)
    for item in items:
        assert set(item) == {"id", "display"}


def test_batch_hash_stable_for_same_ids():
    rows = _batch_rows_for_page()
    assert lp.batch_hash(rows) == lp.batch_hash(rows)
    assert lp.batch_hash(rows) != lp.batch_hash(rows[:1])


# ---------------------------------------------------------------------------
# import_labels.py
# ---------------------------------------------------------------------------

@pytest.fixture
def fixtures_and_batch(tmp_path):
    fixtures_dir = tmp_path / "fixtures"
    batch_dir = fixtures_dir / "batch"
    batch_dir.mkdir(parents=True)
    batch_rows = [
        {
            "set": "threads", "id": "threads:aaa", "label": None, "label_source": "owner",
            "features": {"earlier": {"title": "A"}, "later": {"title": "B"}, "gap_days": 5},
            "display": {}, "site_id": "s1", "company_id": "c1",
            "baseline": {"score": 0.61}, "stratum": "high", "topic_ids": ["t-a2", "t-a1"],
        },
        {
            "set": "threads", "id": "threads:bbb", "label": None, "label_source": "owner",
            "features": {"earlier": {"title": "C"}, "later": {"title": "D"}, "gap_days": 9},
            "display": {}, "site_id": "s1", "company_id": "c1",
            "baseline": {"score": 0.31}, "stratum": "mid", "topic_ids": ["t-b2", "t-b1"],
        },
        {
            "set": "threads", "id": "threads:ccc", "label": None, "label_source": "owner",
            "features": {"earlier": {"title": "E"}, "later": {"title": "F"}, "gap_days": 2},
            "display": {}, "site_id": "s1", "company_id": "c1",
            "baseline": {"score": 0.02}, "stratum": "low", "topic_ids": ["t-c2", "t-c1"],
        },
    ]
    with open(batch_dir / "threads.batch.jsonl", "w", encoding="utf-8") as fh:
        for row in batch_rows:
            fh.write(json.dumps(row) + "\n")
    return fixtures_dir, batch_dir


def test_import_labels_drops_unsure_and_counts_it(tmp_path, fixtures_and_batch):
    fixtures_dir, batch_dir = fixtures_and_batch
    labels_path = tmp_path / "threads.labels.json"
    labels_path.write_text(json.dumps({
        "threads:aaa": "yes", "threads:bbb": "no", "threads:ccc": "unsure",
    }), encoding="utf-8")

    result = il.import_set("threads", labels_path, batch_dir=batch_dir, fixtures_dir=fixtures_dir,
                            now_iso="2026-09-28T00:00:00+00:00")

    assert result["imported_yes_no"] == 2
    assert result["unsure_dropped"] == 1
    assert result["skipped_unknown_id"] == 0

    merged = il.load_jsonl(fixtures_dir / "threads.jsonl")
    assert {r["id"]: r["label"] for r in merged} == {"threads:aaa": "yes", "threads:bbb": "no"}
    assert all(r["label_source"] == "owner" for r in merged)


def test_import_labels_counts_ids_not_in_the_batch(tmp_path, fixtures_and_batch):
    fixtures_dir, batch_dir = fixtures_and_batch
    labels_path = tmp_path / "threads.labels.json"
    labels_path.write_text(json.dumps({
        "threads:aaa": "yes", "threads:not-in-batch": "no",
    }), encoding="utf-8")

    result = il.import_set("threads", labels_path, batch_dir=batch_dir, fixtures_dir=fixtures_dir,
                            now_iso="2026-09-28T00:00:00+00:00")

    assert result["imported_yes_no"] == 1
    assert result["skipped_unknown_id"] == 1
    merged = il.load_jsonl(fixtures_dir / "threads.jsonl")
    assert {r["id"] for r in merged} == {"threads:aaa"}
    assert all(r["decided_at"] == "2026-09-28T00:00:00+00:00" for r in merged)


def test_import_labels_is_idempotent_on_reimport(tmp_path, fixtures_and_batch):
    fixtures_dir, batch_dir = fixtures_and_batch
    labels_path = tmp_path / "threads.labels.json"
    labels_path.write_text(json.dumps({"threads:aaa": "yes"}), encoding="utf-8")

    il.import_set("threads", labels_path, batch_dir=batch_dir, fixtures_dir=fixtures_dir,
                   now_iso="2026-09-28T00:00:00+00:00")
    il.import_set("threads", labels_path, batch_dir=batch_dir, fixtures_dir=fixtures_dir,
                   now_iso="2026-09-28T01:00:00+00:00")

    merged = il.load_jsonl(fixtures_dir / "threads.jsonl")
    assert len(merged) == 1  # no duplicate row for the same id
    assert merged[0]["decided_at"] == "2026-09-28T01:00:00+00:00"  # replaced, not appended


def test_import_labels_refreshes_counts_preserving_existing_fields(tmp_path, fixtures_and_batch):
    fixtures_dir, batch_dir = fixtures_and_batch
    counts_path = fixtures_dir / "counts.json"
    counts_path.write_text(json.dumps({
        "threads": {
            "n": 0, "positives": 0, "negatives": 0, "descriptive_only": True,
            "database": "fieldsight", "exported_at": "2026-09-01T00:00:00+00:00",
            "exclusions": {"orphaned_parent": 1},
        },
        "route_note": "labels exported read-only; no rows written",
    }), encoding="utf-8")

    labels_path = tmp_path / "threads.labels.json"
    labels_path.write_text(json.dumps({
        "threads:aaa": "yes", "threads:bbb": "no",
    }), encoding="utf-8")

    result = il.import_set("threads", labels_path, batch_dir=batch_dir, fixtures_dir=fixtures_dir,
                            now_iso="2026-09-28T00:00:00+00:00")

    counts = json.loads(counts_path.read_text(encoding="utf-8"))
    entry = counts["threads"]
    assert entry["n"] == 2
    assert entry["positives"] == 1
    assert entry["negatives"] == 1
    assert entry["label_source_breakdown"] == {"owner": 2}
    # untouched fields from the original export preserved
    assert entry["database"] == "fieldsight"
    assert entry["exported_at"] == "2026-09-01T00:00:00+00:00"
    assert entry["exclusions"] == {"orphaned_parent": 1}
    assert result["counts"]["n"] == 2


# ---------------------------------------------------------------------------
# Fix wave 4, D18: --work-class-stratum-limit default 150, plus a Python-side
# safety check estimating response size before any transaction opens.
# ---------------------------------------------------------------------------

def test_default_work_class_stratum_limit_is_150():
    assert sb.DEFAULT_WORK_CLASS_STRATUM_LIMIT == 150


def test_check_stratum_limit_safe_accepts_the_default():
    sb.check_stratum_limit_safe(sb.DEFAULT_WORK_CLASS_STRATUM_LIMIT)  # must not raise


def test_check_stratum_limit_safe_rejects_a_too_large_limit():
    with pytest.raises(sb.BatchSizeError, match="1 MiB"):
        sb.check_stratum_limit_safe(1000)


def test_sample_work_class_refuses_before_opening_a_transaction(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("must not begin a transaction for an unsafe stratum_limit")

    monkeypatch.setattr(sb.ex, "_begin_transaction", _boom)

    with pytest.raises(sb.BatchSizeError):
        sb.sample_work_class("fieldsight_test", stratum_limit=1000)



# ---------------------------------------------------------------------------
# Fix wave 5, item 6: import never passes a None topic_ids through for a
# pre-wave-4 batch -- work_class derives it from the id (the id IS the topic
# id), threads rows without it are dropped and counted (their id is a hash).
# ---------------------------------------------------------------------------

def test_build_import_rows_derives_or_drops_missing_topic_ids():
    wc_batch = [{"id": "topic-1", "features": {}, "baseline": {}}]
    rows, _unsure, _unknown, n_no_ids = il.build_import_rows(
        "work_class", wc_batch, {"topic-1": "yes"}, "2026-09-28T00:00:00+00:00")
    assert rows[0]["topic_ids"] == ["topic-1"]
    assert n_no_ids == 0

    th_batch = [{"id": "threads:x", "features": {}, "baseline": {}},
                {"id": "threads:y", "features": {}, "baseline": {}, "topic_ids": ["a", "b"]}]
    rows, _unsure, _unknown, n_no_ids = il.build_import_rows(
        "threads", th_batch, {"threads:x": "yes", "threads:y": "no"}, "2026-09-28T00:00:00+00:00")
    assert [r["id"] for r in rows] == ["threads:y"]
    assert rows[0]["topic_ids"] == ["a", "b"]
    assert n_no_ids == 1


def test_import_set_reports_rows_dropped_for_missing_topic_ids(tmp_path, fixtures_and_batch):
    fixtures_dir, batch_dir = fixtures_and_batch
    path = batch_dir / "threads.batch.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    rows[0].pop("topic_ids")
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    labels_path = tmp_path / "threads.labels.json"
    labels_path.write_text(json.dumps({"threads:aaa": "yes", "threads:bbb": "no"}),
                           encoding="utf-8")

    result = il.import_set("threads", labels_path, batch_dir=batch_dir, fixtures_dir=fixtures_dir,
                            now_iso="2026-09-28T00:00:00+00:00")
    assert result["imported_yes_no"] == 1
    assert result["skipped_no_topic_ids"] == 1
