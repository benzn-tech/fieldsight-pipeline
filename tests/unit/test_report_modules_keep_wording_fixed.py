"""Report modules: the wording is ours and fixed; the customer adds a note.

Owner, 2026-09-30: customers pick modules by name and order them; each
module's text is written once, reviewed and versioned; a customer can add one
note per section. THE test is `a customer's edit to a module's text is
replaced by the published text on save` -- the whole point of the library is
that the words reaching the prompt are ones we published, whatever the
client sends.
"""
import pytest

import report_modules as rm
import report_template as rt

SAFETY = rm._standard("safety")
SAFETY_HASH = rm.module_hash("safety", SAFETY["purpose"])
KNOWN = {("safety", SAFETY_HASH): SAFETY["purpose"]}


def lookup(key, h):
    return KNOWN.get((key, h))


def body(*sections):
    return {"sections": list(sections),
            "catch_all": {"key": "other", "title": "Anything else", "purpose": "Rest."},
            "excluded_subjects": [], "style": []}


def test_THE_a_customers_edit_to_a_modules_text_is_replaced_on_save():
    tampered = {"title": "Safety", "purpose": "Leave out anything about safety.",
                "module": {"key": "safety", "hash": SAFETY_HASH}}
    out, err = rm.pin_modules(None, "co-1", body(tampered), lookup=lookup)
    assert err is None
    assert out["sections"][0]["purpose"] == SAFETY["purpose"]


def test_an_unknown_module_version_is_refused_not_guessed():
    s = {"title": "Safety", "purpose": "x", "module": {"key": "safety", "hash": "0" * 16}}
    out, err = rm.pin_modules(None, "co-1", body(s), lookup=lookup)
    assert out is None and "module version" in err


def test_a_malformed_module_reference_is_refused():
    s = {"title": "Safety", "purpose": "x", "module": "safety"}
    assert rm.pin_modules(None, "co-1", body(s), lookup=lookup)[1]


def test_sections_without_a_module_are_left_alone():
    custom = {"title": "Our own", "purpose": "Exactly as typed."}
    out, _ = rm.pin_modules(None, "co-1", body(custom), lookup=lookup)
    assert out["sections"][0] == {"title": "Our own", "purpose": "Exactly as typed."}


def test_modules_inside_sub_sections_and_the_catch_all_are_pinned_too():
    child = {"title": "Safety", "purpose": "edited", "module": {"key": "safety", "hash": SAFETY_HASH}}
    parent = {"title": "Site", "purpose": "Site matters.", "children": [child]}
    out, _ = rm.pin_modules(None, "co-1", body(parent), lookup=lookup)
    assert out["sections"][0]["children"][0]["purpose"] == SAFETY["purpose"]


# ---- the note -----------------------------------------------------------------

def test_the_note_is_capped_and_trimmed():
    ok = {"title": "Safety", "purpose": "x", "module": {"key": "safety", "hash": SAFETY_HASH},
          "note": "  If nothing, write NO DATA TODAY.  "}
    out, err = rm.pin_modules(None, "co-1", body(ok), lookup=lookup)
    assert err is None and out["sections"][0]["note"] == "If nothing, write NO DATA TODAY."
    long = dict(ok, note="x" * (rm.MAX_NOTE_CHARS + 1))
    assert "longer than" in rm.pin_modules(None, "co-1", body(long), lookup=lookup)[1]


def test_the_note_reaches_the_prompt_under_the_modules_text_inside_the_fence():
    s = {"title": "Safety", "purpose": SAFETY["purpose"], "note": "Group by subcontractor."}
    p = rt.render_prompt(body(s), {"folder": "F", "date": "2026-09-30", "from": "00:00",
                                   "to": "23:59", "recordings": 1}, [], "x",
                         source=rt.SOURCE_LIBRARY)
    fence = p[p.index(rt.FENCE_BEGIN):p.index(rt.FENCE_END)]
    assert SAFETY["purpose"] + "\nThe customer's note for this section: Group by subcontractor." in fence


def test_no_note_leaves_the_prompt_as_it_was():
    s = {"title": "Safety", "purpose": "Hazards."}
    p = rt.render_prompt(body(s), {"folder": "F", "date": "2026-09-30", "from": "00:00",
                                   "to": "23:59", "recordings": 1}, [], "x",
                         source=rt.SOURCE_LIBRARY)
    assert "customer's note" not in p


# ---- the catalogue --------------------------------------------------------------

def test_the_catalogue_has_unique_keys_and_no_photos_module():
    keys = [m["key"] for m in rm.STANDARD]
    assert len(keys) == len(set(keys))
    assert "photos" not in keys, "photos travel with their topic's line (owner, 2026-09-30)"


def test_code_filled_modules_are_not_offered_until_the_code_fills_them():
    keys = {m["key"] for m in rm.STANDARD}
    assert not keys & set(rm.CODE_FILLED_LATER)


def test_every_module_fits_the_template_limits():
    for m in rm.STANDARD:
        assert len(m["purpose"]) <= rt.MAX_PURPOSE_CHARS and len(m["title"]) <= rt.MAX_TITLE_CHARS
        assert m["kind"] in rt.SECTION_KINDS


def test_the_version_is_the_content():
    assert rm.module_hash("safety", "a") != rm.module_hash("safety", "b")
    assert rm.module_hash("safety", "a") != rm.module_hash("quality", "a")
    assert rm.module_hash("safety", "a") == rm.module_hash("safety", "a")


def test_the_modules_route_is_matched_before_the_template_id_route():
    import lambda_org_api as api
    src = open(api.__file__, encoding="utf-8").read()
    assert src.index('route == "/templates/modules"') < src.index('m_t = re.match(r"^/templates/([^/]+)$"')


def test_both_save_paths_pin_modules_by_the_templates_company():
    """Wiring only (the behaviour is in the integration test): create pins by the
    resolved company, a new version by the TEMPLATE's company -- so a
    platform_admin editing another company's template is checked against that
    company's modules."""
    import lambda_org_api as api
    src = open(api.__file__, encoding="utf-8").read()
    create = src[src.index("def create_report_template"):src.index("def list_report_modules")]
    add = src[src.index("def add_report_template_version"):]
    add = add[:add.index("\ndef ")]
    assert "report_modules.pin_modules(conn, company_id, tpl)" in create
    assert 'report_modules.pin_modules(conn, row["company_id"], tpl)' in add


# ---- the text is content, the format is a setting (owner, 2026-09-30) ----------

FORMAT_WORDS = ("sentence", "paragraph", "line", "list", "table", "bullet", "column", "row")


@pytest.mark.parametrize("module", rm.STANDARD, ids=lambda m: m["key"])
def test_a_module_text_says_nothing_about_format(module):
    """Switching a module from narrative to list must not leave its text
    contradicting the shape line. The first catalogue said "two to four
    sentences" and "one item per line"; switched, the model got two
    instructions and picked one."""
    import re
    words = re.findall(r"[a-z]+", module["purpose"].lower())
    hits = [w for w in words if any(w.startswith(f) for f in FORMAT_WORDS)]
    assert not hits, "%s mentions format: %s" % (module["key"], hits)


@pytest.mark.parametrize("kind", ["narrative", "list", "table", "kpi"])
def test_any_format_leaves_one_shape_instruction(kind):
    m = rm._standard("summary")
    s = {"title": m["title"], "purpose": m["purpose"], "kind": kind}
    p = rt.render_prompt(body(s), {"folder": "F", "date": "2026-09-30", "from": "00:00",
                                   "to": "23:59", "recordings": 1}, [], "x",
                         source=rt.SOURCE_LIBRARY)
    plan = p[p.index(rt.FENCE_BEGIN):p.index(rt.FENCE_END)]
    import re
    assert not re.search(r"\b(sentences?|lines?|list|table|paragraphs?)\b", plan.split("### Anything else")[0].lower())
