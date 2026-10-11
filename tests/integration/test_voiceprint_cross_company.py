"""Integration: project-owned tenancy P5 -- voiceprints on another company's site.

The candidate set on a B site for an A recorder is B's enrolled prints plus the recorder's
own print (read from A) and NOBODY else from A. Consent: the SITE company's basis gates
matching on its site; the recorder's own print also needs their HOME company's basis.
Real Postgres, because a connection double records SQL and never parses it.
"""
import contextlib
import json

import pytest

from repositories import voiceprints as vp

pytestmark = pytest.mark.integration


def _vec(i):
    v = [0.0] * 192
    v[i] = 1.0
    return "[" + ",".join(str(x) for x in v) + "]"


def _company(db, basis="attestation"):
    return db.execute(
        "INSERT INTO companies (name, voiceprint_consent_basis) "
        "VALUES ('VP Co ' || gen_random_uuid()::text, %s) RETURNING id", (basis,)).fetchone()[0]


def _user(db, cid, first):
    email = f"{first}.{cid}@example.com".lower()
    return db.execute(
        "INSERT INTO users (company_id, cognito_sub, first_name, last_name, email) "
        "VALUES (%s, %s, %s, 'T', %s) RETURNING id", (cid, "sub-" + email, first, email)
    ).fetchone()[0]


def _print(db, cid, name, user_id, vec_i, consent=True, status="confirmed"):
    pid = db.execute(
        "INSERT INTO speaker_voiceprints (company_id, display_name, status, user_id, consent_at) "
        "VALUES (%s, %s, %s, %s, CASE WHEN %s THEN now() END) RETURNING id",
        (cid, name, status, user_id, consent)).fetchone()[0]
    db.execute(
        "INSERT INTO speaker_voiceprint_samples (company_id, voiceprint_id, embedding, source, "
        " s3_key, window_start_s, window_end_s) VALUES (%s, %s, %s::vector, 'enrolment', 'k', 0, 10)",
        (cid, pid, _vec(vec_i)))
    return pid


@pytest.fixture
def world(db, monkeypatch):
    vw = pytest.importorskip("lambda_voiceprint_writer", reason="requires psycopg")
    a, b = _company(db), _company(db)
    eve, adam = _user(db, a, "Eve"), _user(db, a, "Adam")     # A employees
    bob = _user(db, b, "Bob")
    w = {"vw": vw, "a": a, "b": b, "eve": eve, "adam": adam,
         "p_eve": _print(db, a, "Eve", eve, 1), "p_adam": _print(db, a, "Adam", adam, 2),
         "p_bob": _print(db, b, "Bob", bob, 3)}

    @contextlib.contextmanager
    def conn():
        yield db
    monkeypatch.setattr(vw, "get_connection", conn)
    monkeypatch.setattr(vw, "company_floor", lambda c, cid: None)
    return w


def _ids(reply):
    return {p["person_key"] for p in reply["profiles"]}


def test_b_site_candidates_are_b_enrolled_plus_the_recorder_and_not_another_a_employee(world):
    w = world
    reply = w["vw"].lambda_handler({"op": "profiles", "company_id": str(w["b"]),
                                    "home_company_id": str(w["a"]),
                                    "recorder_user_id": str(w["eve"])}, None)
    assert _ids(reply) == {str(w["p_bob"]), str(w["p_eve"])}
    assert str(w["p_adam"]) not in _ids(reply)


def test_home_site_request_is_unchanged_company_wide(world):
    w = world
    for extra in ({}, {"home_company_id": str(w["a"]), "recorder_user_id": str(w["eve"])}):
        reply = w["vw"].lambda_handler({"op": "profiles", "company_id": str(w["a"]), **extra}, None)
        assert _ids(reply) == {str(w["p_eve"]), str(w["p_adam"])}


def test_old_format_request_without_the_new_fields_still_works(world):
    w = world
    reply = w["vw"].lambda_handler({"op": "profiles", "company_id": str(w["b"])}, None)
    assert _ids(reply) == {str(w["p_bob"])}


def test_site_company_consent_off_means_no_matching_on_its_site(db, world):
    w = world
    db.execute("UPDATE companies SET voiceprint_consent_basis=NULL WHERE id=%s", (w["b"],))
    reply = w["vw"].lambda_handler({"op": "profiles", "company_id": str(w["b"]),
                                    "home_company_id": str(w["a"]),
                                    "recorder_user_id": str(w["eve"])}, None)
    assert reply["profiles"] == []


def test_home_company_consent_off_excludes_only_the_recorders_own_print(db, world):
    w = world
    db.execute("UPDATE companies SET voiceprint_consent_basis=NULL WHERE id=%s", (w["a"],))
    reply = w["vw"].lambda_handler({"op": "profiles", "company_id": str(w["b"]),
                                    "home_company_id": str(w["a"]),
                                    "recorder_user_id": str(w["eve"])}, None)
    assert _ids(reply) == {str(w["p_bob"])}


def test_withdrawn_or_unconsented_recorder_print_is_not_a_candidate(db, world):
    w = world
    db.execute("UPDATE speaker_voiceprints SET consent_at=NULL WHERE id=%s", (w["p_eve"],))
    reply = w["vw"].lambda_handler({"op": "profiles", "company_id": str(w["b"]),
                                    "home_company_id": str(w["a"]),
                                    "recorder_user_id": str(w["eve"])}, None)
    assert _ids(reply) == {str(w["p_bob"])}


def test_missing_recorder_user_id_never_means_every_a_employee(world):
    w = world
    reply = w["vw"].lambda_handler({"op": "profiles", "company_id": str(w["b"]),
                                    "home_company_id": str(w["a"])}, None)
    assert _ids(reply) == {str(w["p_bob"])}


def test_recorder_is_on_the_roster_by_construction(db, world, monkeypatch):
    w = world
    site = db.execute("INSERT INTO sites (company_id, name) VALUES (%s, 'S') RETURNING id",
                      (w["b"],)).fetchone()[0]
    monkeypatch.setattr(w["vw"].site_attendance, "on_roster_profile_ids",
                        lambda *a, **k: {str(w["p_bob"])})
    reply = w["vw"].lambda_handler({"op": "profiles", "company_id": str(w["b"]),
                                    "site_id": str(site), "date": "2026-10-09",
                                    "home_company_id": str(w["a"]),
                                    "recorder_user_id": str(w["eve"])}, None)
    by = {p["person_key"]: p["on_roster"] for p in reply["profiles"]}
    assert by[str(w["p_eve"])] is True and by[str(w["p_bob"])] is True


def test_withdrawing_the_home_print_supersedes_names_it_put_on_a_foreign_site(db, world):
    """The name written on B's site carries B's company id; the withdrawal runs in A."""
    w = world
    row = vp.record_turn_name(db, w["b"], session_base="sid" + "e" * 32, turn_ref="f.wav@1.0",
                              state="confirmed", source="voiceprint_match",
                              voiceprint_id=w["p_eve"], display_name="Eve")
    assert row is not None
    vp.withdraw(db, w["a"], w["p_eve"])
    live = db.execute("SELECT count(*) FROM speaker_turn_names WHERE voiceprint_id=%s "
                      "AND superseded_at IS NULL", (w["p_eve"],)).fetchone()[0]
    assert live == 0
