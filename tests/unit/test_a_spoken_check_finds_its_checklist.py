"""A spoken check finds the company's checklist template by what KIND of check
it is (voice-triggered checklists, owner 2026-09-30).

THE test is `pre-pour finds the pre-pour checklist and steel finds steel`,
with the names the owner used on prod 2026-10-05.
"""
import pytest

import inspection_match as im

CHECK = {"kind": "checklist", "title": "Checks", "items": ["Formwork", "Cover"]}


def tpl(name, scope="org", sections=(CHECK,)):
    return {"id": name, "name": name, "scope": scope, "body": {"sections": list(sections)}}


TEMPLATES = [tpl("Pre-pour Inspection Checklist"), tpl("Reinforcing Steel Inspection"),
             tpl("Fire Stopping QA"),
             tpl("Concrete Pour Summary", sections=({"title": "Summary"},))]   # not a checklist


def found(kind, name=""):
    t, s = im.match({"kind": kind, "name": name}, TEMPLATES)
    return (t["name"] if t else None), round(s, 2)


def test_THE_pre_pour_finds_the_pre_pour_checklist_and_steel_finds_steel():
    assert found("pre-pour", "level one pre-pour inspections") == ("Pre-pour Inspection Checklist", 1.0)
    assert found("steel installation", "level two steel installation inspection") == \
        ("Reinforcing Steel Inspection", 0.5)


def test_a_misheard_word_still_matches():
    assert found("pre-poor")[0] == "Pre-pour Inspection Checklist"
    assert found("fire stoping")[0] == "Fire Stopping QA"


def test_a_check_no_template_names_matches_nothing_and_says_how_close_it_came():
    assert found("inspection", "Tikaha room inspections") == (None, 0.0)
    assert found("scaffold") == (None, 0.0)


def test_only_checklist_templates_are_candidates():
    assert found("concrete summary")[0] is None


def test_the_generic_words_never_match_on_their_own():
    assert im.words("Level 2 Inspection Checklist for the room") == []
    assert im.words("pre-pour check on level 3") == ["pre", "pour"]


def test_the_org_template_wins_a_tie_with_a_personal_copy():
    ts = [tpl("Pre-pour", scope="personal"), tpl("Pre-pour", scope="org")]
    t, _ = im.match({"kind": "pre-pour"}, ts)
    assert t["scope"] == "org"


def test_the_more_specific_template_wins():
    ts = [tpl("Steel and Concrete General Checklist"), tpl("Steel Checklist")]
    assert im.match({"kind": "steel"}, ts)[0]["name"] == "Steel Checklist"


def test_item_writer_stores_each_check_with_its_match(monkeypatch):
    lam = pytest.importorskip("lambda_item_writer")
    stored = {}
    monkeypatch.setattr(lam.inspection_windows, "checklist_templates", lambda c, co, u: TEMPLATES)
    monkeypatch.setattr(lam.inspection_windows, "replace_for_session",
                        lambda c, co, f, d, s, rows: stored.update(rows=rows, session=s))
    rows = lam._store_inspections(None, "c-1", "u-1", "Ben_Lin_Test", "2026-10-05", "sid1", [
        {"name": "L1 pre-pour", "kind": "pre-pour", "start_at": "10:58", "start_at_s": "10:59:09",
         "end_at": "11:02", "end_at_s": "11:02:14", "end_source": "next_check"},
        {"name": "Tikaha room inspections", "kind": "inspection", "start_at": "11:03",
         "start_at_s": None, "end_at": None, "end_source": "recording_stop"}])
    assert stored["session"] == "sid1"
    assert [(r["template_id"], r["start_at"], r["end_at"]) for r in rows] == [
        ("Pre-pour Inspection Checklist", "10:59:09", "11:02:14"), (None, "11:03:00", None)]


def test_item_writer_calls_it_after_the_markers():
    lam = pytest.importorskip("lambda_item_writer")
    src = open(lam.__file__, encoding="utf-8").read()
    marks = src.index("location_markers.replace_for_session(")
    insp = src.index("_store_inspections(conn, owner_company_id, user_id, user_folder, date,")
    assert marks < insp


def test_the_report_dialog_reads_the_days_checks(monkeypatch):
    import json
    import uuid
    org = pytest.importorskip("lambda_org_api")
    seen = {}
    monkeypatch.setattr(org, "_resolve_org_media_folder",
                        lambda conn, caller, user, what: (user or "self", None))
    monkeypatch.setattr(org.inspection_windows, "for_day",
                        lambda conn, co, f, d: seen.update(co=co, f=f, d=d) or [
                            {"id": uuid.UUID(int=1), "session": "sid1", "name": "L1 pre-pour",
                             "kind": "pre-pour", "start_at": "10:59:09", "end_at": "11:02:14",
                             "end_source": "next_check", "start_quote": "q", "end_quote": None,
                             "template_id": uuid.UUID(int=2), "match_score": 1.0,
                             "template_name": "Pre-pour Inspection Checklist"}])
    caller = {"id": "u", "company_id": "c-1", "global_role": "gm"}
    res = org.get_day_inspections(None, caller, "2026-10-05",
                                  {"queryStringParameters": {"user": "Ben_Lin_Test"}})
    body = json.loads(res["body"])
    assert res["statusCode"] == 200 and seen == {"co": "c-1", "f": "Ben_Lin_Test", "d": "2026-10-05"}
    assert body["inspections"][0]["template_name"] == "Pre-pour Inspection Checklist"
    assert body["inspections"][0]["template_id"] == str(uuid.UUID(int=2))
    assert org.get_day_inspections(None, caller, "05-10-2026", {})["statusCode"] == 400
    src = open(org.__file__, encoding="utf-8").read()
    assert 're.match(r"^/days/([^/]+)/inspections$", route)' in src


def test_a_full_checklists_section_titles_do_not_capture_other_checks():
    """"Concrete Pre-pour Inspection Checklist" has a "Safety & Environment"
    section; a safety inspection is not a pre-pour check."""
    full = tpl("Concrete Pre-pour Inspection Checklist", sections=(
        dict(CHECK, title="C. Reinforcement"), dict(CHECK, title="F. Safety & Environment")))
    assert im.match({"kind": "safety"}, [full])[0] is None
    assert im.match({"kind": "pre-pour"}, [full])[0]["name"] == full["name"]


def test_a_generic_name_falls_back_to_its_section_titles():
    qa = tpl("QA Checklists", sections=(dict(CHECK, title="Pre-pour"),))
    assert im.match({"kind": "pre-pour"}, [qa])[0]["name"] == "QA Checklists"
