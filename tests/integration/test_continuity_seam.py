"""Task 9: the continuity seam, end to end, on real Postgres.

Drives the REAL `lambda_extract_session.extract_session` (only `llm_utils.call_llm` is
stubbed) into the REAL `lambda_item_writer.write_extraction_items` -- the prompt builder,
`item_continuity.resolve`/`prior_items`/`render_block`, `_map_action_items`,
`repositories.action_items` inserts, `carry_forward_apply`'s item_id pass-0 pairing, and
`continuity_records.record_claims` all run unstubbed. One in-memory S3 double is shared by
both lambdas -- extract_session's own `read_existing_extraction`/`put_object` and the
writer's `get_object` all resolve against the SAME dict, exactly as they do in production
where both read/write the one `extractions/{folder}/{date}/{session_base}.json` object.

Harness style follows tests/integration/test_supersede_two_passes.py: a real, separately
committed connection (not the rolled-back `db` fixture -- its rollback would hide one pass's
writes from the next), company/site/user/membership seeded for real so the identity bridge
resolves without any repository function stubbed out, and cleanup by id in a `finally`. Kept
local to this file rather than cross-imported from tests/unit, per that file's own stated
convention ("integration tests in this repo do not import from tests/unit").

The flag is flipped with `monkeypatch.setattr(les, "DECLARE_CONTINUITY", ...)`, per the task
brief -- not a module reload (that would leak state, e.g. the site cache, across tests).

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import difflib
import io
import json
import uuid

import pytest

import carry_forward
from db.connection import get_connection
from repositories import action_items, companies, memberships, sites, users

pytestmark = pytest.mark.integration

les = pytest.importorskip("lambda_extract_session", reason="requires boto3 (installed in CI)")
lambda_item_writer = pytest.importorskip(
    "lambda_item_writer", reason="requires psycopg (installed in CI)")
import llm_utils  # noqa: E402  (import after importorskip, the module both lambdas call)

BUCKET = "test-bucket"
DATE = "2026-09-29"


# ---------------------------------------------------------------------------
# S3 double -- shared by extract_session (get_object/get_paginator/put_object) and the
# writer (get_object/get_paginator). Same shape as tests/unit/test_lambda_extract_session.py's
# FakeS3, kept local per this directory's own convention.
# ---------------------------------------------------------------------------

class _FakeNoSuchKey(Exception):
    def __init__(self):
        super().__init__("NoSuchKey")
        self.response = {"Error": {"Code": "NoSuchKey"}}


class _FakeS3:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _FakeNoSuchKey()
        body = self.objects[Key]
        raw = body.encode("utf-8") if isinstance(body, str) else body
        return {"Body": io.BytesIO(raw)}

    def get_paginator(self, op):
        assert op == "list_objects_v2"
        return _FakePaginator(self.objects)

    def put_object(self, **kwargs):
        self.objects[kwargs["Key"]] = kwargs["Body"]
        return {}


class _FakePaginator:
    def __init__(self, objects):
        self.objects = objects

    def paginate(self, Bucket, Prefix):
        yield {"Contents": [{"Key": k} for k in self.objects if k.startswith(Prefix)]}


def _transcribe_json(text):
    """One pronunciation item per word -- just enough for assemble_session_turns to produce
    a non-empty turn. Content is irrelevant: the LLM is stubbed, never actually reads this."""
    words = text.split()
    items = []
    t = 0.0
    for w in words:
        items.append({"type": "pronunciation", "start_time": f"{t:.3f}",
                      "end_time": f"{t + 1:.3f}",
                      "alternatives": [{"content": w, "confidence": "0.9"}]})
        t += 1.0
    return {"results": {"transcripts": [{"transcript": text}], "items": items}}


# ---------------------------------------------------------------------------
# Identity seeding -- same pattern as test_supersede_two_passes.py's second harness.
# ---------------------------------------------------------------------------

def _seed_identity(seed, tag):
    company_name = f"Cont9-Co-{tag}"
    co = companies.create_company(seed, company_name)
    site = sites.create_site(seed, co["id"], f"Cont9-Site-{tag}")
    folder = f"Cont9-{tag}"
    user = users.upsert_field_only_user(seed, co["id"], folder, "Fol", "Der", "worker")
    memberships.add_membership(seed, user["id"], site["id"], "worker")
    return co, site, user, folder, company_name


def _cleanup(seed, co, site, user):
    co_id = co["id"] if co is not None else None
    site_id = site["id"] if site is not None else None
    user_id = user["id"] if user is not None else None
    if site_id is not None:
        seed.execute("DELETE FROM sites WHERE id=%s", (site_id,))
    if user_id is not None:
        seed.execute("DELETE FROM users WHERE id=%s", (user_id,))
    if co_id is not None:
        seed.execute("DELETE FROM companies WHERE id=%s", (co_id,))
    if co_id is not None:
        remaining = seed.execute(
            "SELECT "
            "(SELECT count(*) FROM companies WHERE id=%s), "
            "(SELECT count(*) FROM sites WHERE id=%s), "
            "(SELECT count(*) FROM users WHERE id=%s), "
            "(SELECT count(*) FROM memberships WHERE site_id=%s), "
            "(SELECT count(*) FROM topics WHERE site_id=%s), "
            "(SELECT count(*) FROM decision_records WHERE company_id=%s)",
            (co_id, site_id, user_id, site_id, site_id, co_id),
        ).fetchone()
        assert remaining == (0, 0, 0, 0, 0, 0), f"leaked rows after cleanup: {remaining}"


# ---------------------------------------------------------------------------
# Extraction payload builder
# ---------------------------------------------------------------------------

def _payload(action_text, claim=None):
    action = {"action": action_text, "responsible": None, "deadline": None, "priority": None}
    if claim is not None:
        action["continues"] = claim
    return {
        "topics": [{
            "topic_title": "Session topic",
            "category": "progress",
            "summary": "summary",
            "time_range": "10:00 – 10:05",
            "participants": [],
            "action_items": [action],
        }],
        "declared_site": None,
    }


# ---------------------------------------------------------------------------
# EMF parsing -- the writer prints one JSON line per metric (carry_forward_apply.py:
# _report_orphaned_human_edits): one OrphanedHumanEdits line, and (when anything was
# carried) one CarriedByMethod line per method.
# ---------------------------------------------------------------------------

def _emf_lines(out, metric_name):
    found = []
    for line in out.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        metrics = obj.get("_aws", {}).get("CloudWatchMetrics", [{}])[0].get("Metrics", [])
        if any(m.get("Name") == metric_name for m in metrics):
            found.append(obj)
    return found


def _carried_by_method(out):
    return {e["Method"]: e["CarriedByMethod"] for e in _emf_lines(out, "CarriedByMethod")}


def _orphaned_count(out):
    lines = _emf_lines(out, "OrphanedHumanEdits")
    assert len(lines) == 1, f"expected exactly one OrphanedHumanEdits line, got {lines}"
    return lines[0]["OrphanedHumanEdits"]


# ---------------------------------------------------------------------------
# Pass driver
# ---------------------------------------------------------------------------

def _run_pass(monkeypatch, capsys, folder, session_base, extraction_key, payload):
    """One live/final pass: stub the LLM, run the real extract_session, then the real
    write_extraction_items. Returns (extraction_dict, write_result, captured_stdout) -- the
    stdout capture is cleared right before the write, so it holds only THIS write's EMF
    lines, not anything printed by an earlier pass."""
    monkeypatch.setattr(llm_utils, "call_llm",
                        lambda prompt, max_tokens=4096, force_json=False,
                        enable_thinking=None, **kw: (json.dumps(payload), None))
    extraction = les.extract_session(BUCKET, folder, DATE, session_base, final=True)
    assert extraction is not None
    capsys.readouterr()
    result = lambda_item_writer.write_extraction_items(DATE, folder, extraction_key)
    assert result == {"skipped": False, "topics": 1}, result
    out = capsys.readouterr().out
    return extraction, result, out


def _live_action_row(seed, extraction_key):
    row = seed.execute(
        "SELECT a.id, a.stable_id, a.item_id, a.status, a.updated_by, a.carried_from "
        "FROM action_items a JOIN topics t ON t.id = a.topic_id "
        "WHERE t.source_s3_key=%s AND t.superseded_at IS NULL",
        (extraction_key,)).fetchone()
    assert row is not None, "no live action_items row for this extraction key"
    return {"id": row[0], "stable_id": row[1], "item_id": row[2], "status": row[3],
            "updated_by": row[4], "carried_from": row[5]}


def _tick(seed, action_id, user_id):
    action_items.update_action_item_fields(
        seed, action_id, {"status": "done"}, str(user_id))


def _s3_and_seg(tag):
    folder = f"Cont9-{tag}"
    session_base = f"{folder}_{DATE}_10-00-00"
    seg_key = f"transcripts/{folder}/{DATE}/{session_base}_off0.0_to30.0_srcwav.json"
    fake_s3 = _FakeS3({seg_key: json.dumps(_transcribe_json("hello world"))})
    return folder, session_base, fake_s3


def _wire(monkeypatch, fake_s3, company_name, migrated_db_url):
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    monkeypatch.setattr(lambda_item_writer, "_s3_client", fake_s3)
    monkeypatch.setattr(lambda_item_writer, "get_connection",
                        lambda *a, **k: get_connection(migrated_db_url))
    monkeypatch.setattr(lambda_item_writer.match_request, "emit", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# 1. A ticked item survives a reworded pass the model claims continuity for.
# ---------------------------------------------------------------------------

def test_a_ticked_item_survives_a_reworded_pass_the_model_claims(monkeypatch, capsys,
                                                                  migrated_db_url):
    tag = uuid.uuid4().hex[:8]
    seed = get_connection(migrated_db_url, autocommit=True)
    co = site = user = None
    try:
        co, site, user, folder, company_name = _seed_identity(seed, tag)
        folder2, session_base, fake_s3 = _s3_and_seg(tag)
        assert folder2 == folder
        extraction_key = f"extractions/{folder}/{DATE}/{session_base}.json"
        _wire(monkeypatch, fake_s3, company_name, migrated_db_url)
        monkeypatch.setattr(les, "DECLARE_CONTINUITY", True)

        PASS1 = "Platform initial login using temporary password then change to own password per PDF"
        PASS2 = "Platform login via temporary password at the office"

        # Pass 1: fresh id, no prior.
        _run_pass(monkeypatch, capsys, folder, session_base, extraction_key, _payload(PASS1))
        old = _live_action_row(seed, extraction_key)
        assert old["item_id"] is not None
        old_stable_id = old["stable_id"]

        # Human ticks it off while it is still live.
        _tick(seed, old["id"], user["id"])
        assert _live_action_row(seed, extraction_key)["status"] == "done"

        # Pass 2: the model claims continuity with an echo of pass 1's real opening words.
        claim = {"id": "A1", "starts": "Platform initial login using"}
        extraction2, _, out = _run_pass(
            monkeypatch, capsys, folder, session_base, extraction_key,
            _payload(PASS2, claim))

        claims = extraction2["continuity"]["claims"]
        assert len(claims) == 1
        assert claims[0]["outcome"] == "accepted"
        assert claims[0]["prior_item_id"] == str(old["item_id"])

        new = _live_action_row(seed, extraction_key)
        assert new["id"] != old["id"], "must be the NEW row, not the old one"
        assert new["stable_id"] == old_stable_id, "stable_id must survive the reword"
        assert new["status"] == "done", "the tick must survive the reword"

        methods = _carried_by_method(out)
        assert methods.get("item_id") == 1, methods

        accepted = seed.execute(
            "SELECT auto_outcome FROM decision_records WHERE kind='item_continuity' "
            "AND input_key=%s", (extraction_key,)).fetchall()
        assert [r[0] for r in accepted] == ["accepted"], accepted
    finally:
        _cleanup(seed, co, site, user)
        seed.close()


# ---------------------------------------------------------------------------
# 2. A claim that fails the echo does not carry the tick.
# ---------------------------------------------------------------------------

def test_a_claim_that_fails_the_echo_does_not_carry_the_tick(monkeypatch, capsys,
                                                              migrated_db_url):
    tag = uuid.uuid4().hex[:8]
    seed = get_connection(migrated_db_url, autocommit=True)
    co = site = user = None
    try:
        co, site, user, folder, company_name = _seed_identity(seed, tag)
        folder2, session_base, fake_s3 = _s3_and_seg(tag)
        assert folder2 == folder
        extraction_key = f"extractions/{folder}/{DATE}/{session_base}.json"
        _wire(monkeypatch, fake_s3, company_name, migrated_db_url)
        monkeypatch.setattr(les, "DECLARE_CONTINUITY", True)

        PASS1 = "Platform initial login using temporary password then change to own password per PDF"
        PASS2 = "Platform login via the office"

        _run_pass(monkeypatch, capsys, folder, session_base, extraction_key, _payload(PASS1))
        old = _live_action_row(seed, extraction_key)
        _tick(seed, old["id"], user["id"])

        # "Platform login via" is NOT the real opening of pass 1's item (that is "Platform
        # initial login using") -- the echo check must reject this claim.
        claim = {"id": "A1", "starts": "Platform login via"}
        extraction2, _, out = _run_pass(
            monkeypatch, capsys, folder, session_base, extraction_key,
            _payload(PASS2, claim))

        claims = extraction2["continuity"]["claims"]
        assert len(claims) == 1
        assert claims[0]["outcome"] == "rejected"
        assert claims[0]["guard"] == "echo"

        new = _live_action_row(seed, extraction_key)
        assert new["status"] != "done", "the tick must NOT have carried onto the new row"

        assert _orphaned_count(out) == 1

        rejected = seed.execute(
            "SELECT auto_outcome, output->>'guard' FROM decision_records "
            "WHERE kind='item_continuity' AND input_key=%s", (extraction_key,)).fetchall()
        assert rejected == [("rejected", "echo")], rejected
    finally:
        _cleanup(seed, co, site, user)
        seed.close()


# ---------------------------------------------------------------------------
# 3. C1 regression: a three-pass chain with a text-carried middle step.
# ---------------------------------------------------------------------------

def test_three_pass_chain_with_a_text_carried_middle_step(monkeypatch, capsys,
                                                           migrated_db_url):
    tag = uuid.uuid4().hex[:8]
    seed = get_connection(migrated_db_url, autocommit=True)
    co = site = user = None
    try:
        co, site, user, folder, company_name = _seed_identity(seed, tag)
        folder2, session_base, fake_s3 = _s3_and_seg(tag)
        assert folder2 == folder
        extraction_key = f"extractions/{folder}/{DATE}/{session_base}.json"
        _wire(monkeypatch, fake_s3, company_name, migrated_db_url)
        monkeypatch.setattr(les, "DECLARE_CONTINUITY", True)

        PASS1 = "Confirm crane booking for Thursday"
        PASS2 = "Confirm the crane booking for Thursday"          # one word added
        PASS3 = "Arrange crane hire before the Thursday concrete pour"

        # Premise checks -- the same carry_forward._key + difflib ratio the fuzzy pass
        # itself uses, so the two texts really do sit either side of the 0.90 floor.
        ratio_12 = difflib.SequenceMatcher(
            None, carry_forward._key(PASS1), carry_forward._key(PASS2)).ratio()
        assert 0.90 <= ratio_12 < 1.0, ratio_12
        ratio_23 = difflib.SequenceMatcher(
            None, carry_forward._key(PASS2), carry_forward._key(PASS3)).ratio()
        assert ratio_23 < 0.90, ratio_23

        # Pass 1: fresh id U1.
        _run_pass(monkeypatch, capsys, folder, session_base, extraction_key, _payload(PASS1))
        pass1_row = _live_action_row(seed, extraction_key)
        stable_id_S1 = pass1_row["stable_id"]
        _tick(seed, pass1_row["id"], user["id"])

        # Pass 2: no claim -- >=0.90 similar but not exact, so the extractor gives it a
        # FRESH item_id U (not U1), and only the fuzzy TEXT pass can carry the tick/stable_id.
        _, _, out2 = _run_pass(
            monkeypatch, capsys, folder, session_base, extraction_key, _payload(PASS2))
        pass2_row = _live_action_row(seed, extraction_key)
        assert pass2_row["item_id"] is not None
        assert pass2_row["item_id"] != pass1_row["item_id"], (
            "pass 2 must NOT inherit pass 1's item_id -- no claim was made")
        assert pass2_row["stable_id"] == stable_id_S1, (
            "the fuzzy text pass must still carry stable_id across the reword")
        assert pass2_row["status"] == "done", "the tick must survive the fuzzy text carry"
        methods2 = _carried_by_method(out2)
        assert methods2.get("fuzzy") == 1, methods2
        assert methods2.get("item_id", 0) == 0, methods2
        item_id_U = pass2_row["item_id"]

        # Pass 3: reworded far below 0.90, but the model claims continuity with pass 2's
        # own alias -- an accepted claim, echoing pass 2's real opening words.
        claim = {"id": "A1", "starts": "Confirm the crane booking"}
        extraction3, _, out3 = _run_pass(
            monkeypatch, capsys, folder, session_base, extraction_key,
            _payload(PASS3, claim))

        claims3 = extraction3["continuity"]["claims"]
        assert len(claims3) == 1
        assert claims3[0]["outcome"] == "accepted"
        assert claims3[0]["prior_item_id"] == str(item_id_U)
        assert claims3[0]["new_item_id"] == str(item_id_U), "the new item must inherit U"

        pass3_row = _live_action_row(seed, extraction_key)
        assert pass3_row["item_id"] == item_id_U, "the new item must inherit U"
        assert pass3_row["stable_id"] == stable_id_S1, "stable_id must survive to pass 3"
        assert pass3_row["status"] == "done", "the tick must survive to pass 3"
        assert pass3_row["carried_from"] == pass2_row["id"], (
            "pass 0 (item_id pairing) must be what paired this row with pass 2's, not fuzzy "
            "text -- pass 3's wording is far below the fuzzy floor")

        methods3 = _carried_by_method(out3)
        assert methods3.get("item_id") == 1, methods3
        assert methods3.get("fuzzy", 0) == 0, (
            "pass 3's text is far below the fuzzy floor -- only item_id pairing could have "
            "carried this")

        # What the first design got wrong: no step may ever have written an item_id's
        # value into the stable_id column.
        item_ids = {pass1_row["item_id"], pass2_row["item_id"], pass3_row["item_id"]}
        stable_ids = {pass1_row["stable_id"], pass2_row["stable_id"], pass3_row["stable_id"]}
        assert not (item_ids & stable_ids), (item_ids, stable_ids)
    finally:
        _cleanup(seed, co, site, user)
        seed.close()


# ---------------------------------------------------------------------------
# 4. Flag off: unchanged end to end (Track B alone, no item_id, no continuity records).
# ---------------------------------------------------------------------------

def test_flag_off_is_unchanged_end_to_end(monkeypatch, capsys, migrated_db_url):
    tag = uuid.uuid4().hex[:8]
    seed = get_connection(migrated_db_url, autocommit=True)
    co = site = user = None
    try:
        co, site, user, folder, company_name = _seed_identity(seed, tag)
        folder2, session_base, fake_s3 = _s3_and_seg(tag)
        assert folder2 == folder
        extraction_key = f"extractions/{folder}/{DATE}/{session_base}.json"
        _wire(monkeypatch, fake_s3, company_name, migrated_db_url)
        monkeypatch.setattr(les, "DECLARE_CONTINUITY", False)

        VERBATIM = "Confirm crane booking for Thursday"

        extraction1, _, _ = _run_pass(
            monkeypatch, capsys, folder, session_base, extraction_key, _payload(VERBATIM))
        assert "continuity" not in extraction1
        old = _live_action_row(seed, extraction_key)
        assert old["item_id"] is None
        _tick(seed, old["id"], user["id"])

        # Same text, verbatim -- Track B's exact-text pass, unaffected by continuity.
        extraction2, _, out2 = _run_pass(
            monkeypatch, capsys, folder, session_base, extraction_key, _payload(VERBATIM))
        assert "continuity" not in extraction2

        new = _live_action_row(seed, extraction_key)
        assert new["item_id"] is None
        assert new["stable_id"] == old["stable_id"]
        assert new["status"] == "done"

        methods2 = _carried_by_method(out2)
        assert methods2.get("exact") == 1, methods2
        assert methods2.get("item_id", 0) == 0, methods2

        records = seed.execute(
            "SELECT count(*) FROM decision_records WHERE kind='item_continuity' "
            "AND input_key=%s", (extraction_key,)).fetchone()
        assert records == (0,), records
    finally:
        _cleanup(seed, co, site, user)
        seed.close()
