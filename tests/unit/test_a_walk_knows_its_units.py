"""A multi-unit walk knows which unit he is in (spec 2026-11-03).

"Unit two", "U2" and "unit-2" are one unit; "the unit" and "lot of studs" are
none. A single mention of "room 102" does not make a check a walk; an opening
list, a count, or entering two different units does. Each entered unit owns the
time until the next one or the end of the recorded stretch, and time before
the first unit is General -- never guessed into a unit.
"""
import units


def test_spoken_forms_of_one_unit_are_one_unit():
    for said in ("unit two", "Unit 2", "U2", "unit-2"):
        assert units.normalize_unit(said) == "Unit 2", said
    assert units.normalize_unit("lot one fifty") == "Lot 150"
    assert units.normalize_unit("lot one hundred and fifty four") == "Lot 154"
    assert units.normalize_unit("room one oh two") == "Room 102"
    assert units.normalize_unit("Room 102") == "Room 102"
    assert units.normalize_unit("now in townhouse twenty three") == "Townhouse 23"
    assert units.normalize_unit("apartment nine hundred ninety nine") == "Apartment 999"


def test_words_without_a_number_are_not_a_unit():
    for said in ("the unit", "lot of studs", "", None, "U-turn"):
        assert units.normalize_unit(said) is None, said


def test_the_opening_list_is_normalized_and_names_win_over_count():
    got = units.opening_units(["Lot 150", "lot one fifty one", "LOT 150", "the lot"], 5)
    assert got == {"names": ["Lot 150", "Lot 151"], "count": 2}
    assert units.opening_units([], 5) == {"names": [], "count": 5}
    assert units.opening_units(None, None) == {"names": [], "count": None}


def _mark(unit, at, at_s=None):
    m = {"unit": unit, "at": at, "quote": "now in " + unit.lower()}
    if at_s:
        m["at_s"] = at_s
    return m


def test_one_room_mention_is_not_a_walk():
    w = {"units": {"names": [], "count": None}, "entered": [_mark("Room 102", "10:41")]}
    assert units.is_multi_unit(w) is False
    assert units.is_multi_unit({"start_at": "10:00:00", "end_at": "10:30:00"}) is False


def test_two_entered_units_make_a_walk():
    w = {"units": {"names": [], "count": None},
         "entered": [_mark("Unit 1", "10:41"), _mark("Unit 2", "10:52")]}
    assert units.is_multi_unit(w) is True
    same = {"entered": [_mark("Unit 2", "10:41"), _mark("U2", "10:52")]}
    assert units.is_multi_unit(same) is False


def test_a_count_makes_a_walk():
    assert units.is_multi_unit({"units": {"names": [], "count": 5}, "entered": []}) is True
    assert units.is_multi_unit({"units": {"names": [], "count": 1}, "entered": []}) is False
    # R8: one unit named in the opening is an ordinary check; two are a walk
    assert units.is_multi_unit({"units": {"names": ["Lot 150"], "count": None}}) is False
    assert units.is_multi_unit({"units": {"names": ["Lot 150", "Lot 151"], "count": None}}) is True


def test_a_letter_after_the_number_is_part_of_the_unit():
    """R7: "Unit 2A" and "Unit 2B" are two units; "unit 2 a crack" is Unit 2."""
    assert units.normalize_unit("unit 2a") == "Unit 2A"
    assert units.normalize_unit("U2B") == "Unit 2B"
    assert units.normalize_unit("Unit 2A") == "Unit 2A"
    assert units.normalize_unit("unit 2 a crack in the plate") == "Unit 2"
    assert units.normalize_unit("lot 150th") == "Lot 150"
    w = {"entered": [_mark("Unit 2A", "10:41"), _mark("Unit 2B", "10:52")]}
    assert units.is_multi_unit(w) is True


def test_a_room_name_without_a_number_is_not_an_entered_unit():
    w = {"start_at": "10:00:00", "end_at": "10:30:00",
         "entered": [_mark("Kitchen", "10:05"), _mark("Bathroom", "10:15")]}
    assert units.is_multi_unit(w) is False
    assert units.unit_stretches(w) == [{"unit": None, "from": "10:00:00", "to": "10:30:00"}]


def test_stretches_close_at_the_next_unit_and_at_a_segment_end():
    """Two recorded stretches of one check. Unit 2 ends with the first stretch;
    the second stretch is General until he says he is in Unit 3 -- it is not
    carried over from Unit 2."""
    w = {"start_at": "10:00:00", "end_at": "11:20:00",
         "segments": [{"from": "10:00:00", "to": "10:30:00"},
                      {"from": "11:00:00", "to": "11:20:00"}],
         "units": {"names": [], "count": 3},
         "entered": [_mark("Unit 3", "11:10", "11:10:30"),
                     _mark("Unit 1", "10:05", "10:05:00"),
                     _mark("Unit 2", "10:15")]}          # no at_s: at + ":00"
    assert units.unit_stretches(w) == [
        {"unit": None, "from": "10:00:00", "to": "10:05:00"},
        {"unit": "Unit 1", "from": "10:05:00", "to": "10:15:00"},
        {"unit": "Unit 2", "from": "10:15:00", "to": "10:30:00"},
        {"unit": None, "from": "11:00:00", "to": "11:10:30"},
        {"unit": "Unit 3", "from": "11:10:30", "to": "11:20:00"},
    ]


def test_without_segments_the_window_is_one_stretch():
    w = {"start_at": "10:00:00", "end_at": None,     # still recording
         "entered": [_mark("Lot 150", "10:00", "10:00:00"), _mark("Lot 151", "10:20", "10:20:05")]}
    st = units.unit_stretches(w)
    assert st == [{"unit": "Lot 150", "from": "10:00:00", "to": "10:20:05"},
                  {"unit": "Lot 151", "from": "10:20:05", "to": None}]
    assert units.unit_at(st, "13:00:00") == "Lot 151"


def test_a_mark_on_the_minute_is_not_lost_before_its_segment():
    """"now in unit one" at 10:00:20, but the model gave only "10:00"."""
    w = {"start_at": "10:00:07", "end_at": "10:30:00", "entered": [_mark("Unit 1", "10:00")]}
    assert units.unit_stretches(w) == [{"unit": "Unit 1", "from": "10:00:07", "to": "10:30:00"}]


def test_a_minute_mark_opens_a_stretch_in_one_segment_only():
    """"10:41" is inside the first segment; the second segment (after a gap)
    does not inherit Unit 1 through the minute clamp."""
    w = {"start_at": "10:40:00", "end_at": "10:50:00",
         "segments": [{"from": "10:40:00", "to": "10:41:05"},
                      {"from": "10:41:30", "to": "10:50:00"}],
         "entered": [_mark("Unit 1", "10:41")]}
    assert units.unit_stretches(w) == [
        {"unit": None, "from": "10:40:00", "to": "10:41:00"},
        {"unit": "Unit 1", "from": "10:41:00", "to": "10:41:05"},
        {"unit": None, "from": "10:41:30", "to": "10:50:00"},
    ]


def test_a_mark_with_seconds_before_a_segment_is_not_moved():
    w = {"start_at": "10:00:07", "end_at": "10:30:00",
         "entered": [_mark("Unit 1", "10:00", "10:00:03")]}
    assert units.unit_stretches(w) == [{"unit": None, "from": "10:00:07", "to": "10:30:00"}]


def test_before_the_first_unit_is_general():
    w = {"start_at": "10:00:00", "end_at": "10:30:00",
         "entered": [_mark("Unit 1", "10:05", "10:05:00"), _mark("Unit 2", "10:15", "10:15:00")]}
    st = units.unit_stretches(w)
    assert units.unit_at(st, "10:02:00") is None
    assert units.unit_at(st, "10:05:00") == "Unit 1"
    assert units.unit_at(st, "10:14:59") == "Unit 1"
    assert units.unit_at(st, "10:20") == "Unit 2"
    assert units.unit_at(st, "10:30:00") is None        # the window has ended
    assert units.unit_at(st, "09:00:00") is None


# --- walk identity across recordings (Task 5) ---------------------------------

CO, FOLDER, DAY, TPL = "c-1", "Ben_Lin_Test", "2026-11-03", "t-1"


def _window(start, names=None, count=None, entered=None, template_id=TPL):
    return {"start_at": start, "template_id": template_id,
            "units": {"names": names or [], "count": count}, "entered": entered or []}


def test_a_later_recording_without_an_opening_continues_the_walk():
    first = units.walk_key(CO, FOLDER, DAY, TPL, "10:00:00")
    second = units.walk_key(CO, FOLDER, DAY, TPL, "10:40:00")
    walks = [{"walk_key": first, "start_at": "10:00:00", "units": {}, "session": "sidA"},
             {"walk_key": second, "start_at": "10:40:00", "units": {}, "session": "sidB"},
             {"walk_key": "walk:later", "start_at": "12:00:00", "units": {}, "session": "sidC"}]
    got = units.assign_walk(_window("11:00:00"), walks, CO, FOLDER, DAY)
    assert got == second, "the latest walk that opened before this recording"


def test_a_new_opening_starts_a_new_walk():
    old = units.walk_key(CO, FOLDER, DAY, TPL, "10:00:00")
    walks = [{"walk_key": old, "start_at": "10:00:00", "units": {}, "session": "sidA"}]
    got = units.assign_walk(_window("11:00:00", names=["Lot 150", "Lot 151"]), walks, CO, FOLDER, DAY)
    assert got == units.walk_key(CO, FOLDER, DAY, TPL, "11:00:00") != old
    assert units.assign_walk(_window("11:30:00", count=3), walks, CO, FOLDER, DAY) == \
        units.walk_key(CO, FOLDER, DAY, TPL, "11:30:00")


def test_the_key_is_stable_across_re_extraction():
    w = _window("10:00:00", names=["Unit 1", "Unit 2"])
    a = units.assign_walk(dict(w), [], CO, FOLDER, DAY)
    b = units.assign_walk(dict(w), [], CO, FOLDER, DAY)
    assert a == b and a.startswith("walk:") and len(a) == len("walk:") + 40
    import uuid
    u = uuid.UUID(int=7)
    assert units.walk_key(u, FOLDER, DAY, u, "10:00:00") == units.walk_key(str(u), FOLDER, DAY, str(u), "10:00:00")


def test_the_key_belongs_to_one_company():
    assert units.walk_key("c-1", FOLDER, DAY, TPL, "10:00:00") != units.walk_key("c-2", FOLDER, DAY, TPL, "10:00:00")


def test_an_ordinary_check_has_no_walk():
    assert units.assign_walk(_window("10:00:00"), [], CO, FOLDER, DAY) is None
    assert units.assign_walk(_window("10:00:00", names=["Room 102"]), [], CO, FOLDER, DAY) is None, \
        "one name in the opening is an ordinary check"
    assert units.assign_walk(_window("10:00:00", names=["Lot 1", "Lot 2"], template_id=None),
                             [], CO, FOLDER, DAY) is None, "no checklist, no walk"


def test_entering_two_units_without_an_opening_or_open_walk_starts_a_walk():
    w = _window("10:00:00", entered=[_mark("Unit 1", "10:01"), _mark("Unit 2", "10:05")])
    assert units.assign_walk(w, [], CO, FOLDER, DAY) == units.walk_key(CO, FOLDER, DAY, TPL, "10:00:00")


def test_a_walk_that_opened_later_is_not_continued():
    walks = [{"walk_key": "walk:x", "start_at": "12:00:00", "units": {}, "session": "sidB"}]
    assert units.assign_walk(_window("11:00:00"), walks, CO, FOLDER, DAY) is None


def test_item_writer_keys_the_walk_from_other_recordings_only(monkeypatch):
    import pytest
    lam = pytest.importorskip("lambda_item_writer")
    tpl = {"id": "t-1", "name": "Framing Checklist", "scope": "org",
           "body": {"sections": [{"kind": "checklist", "items": ["Studs"]}]}}
    stored, asked = {}, []
    monkeypatch.setattr(lam.inspection_windows, "checklist_templates", lambda c, co, u: [tpl])
    monkeypatch.setattr(lam.inspection_match, "match", lambda w, ts: (tpl, 1.0))
    old = units.walk_key("c-1", FOLDER, DAY, "t-1", "09:00:00")
    mine = units.walk_key("c-1", FOLDER, DAY, "t-1", "09:30:00")

    def open_walks(c, co, f, d, t):
        asked.append(t)
        return [{"walk_key": old, "start_at": "09:00:00", "units": {}, "session": "sidA"},
                {"walk_key": mine, "start_at": "09:30:00", "units": {}, "session": "sidB"}]
    monkeypatch.setattr(lam.inspection_windows, "open_walks", open_walks)
    monkeypatch.setattr(lam.inspection_windows, "replace_for_session",
                        lambda c, co, f, d, s, rows: stored.update(rows=rows))

    class Conn:
        def transaction(self):
            import contextlib
            return contextlib.nullcontext()
    lam._store_inspections(Conn(), "c-1", "u-1", FOLDER, DAY, "sidB", [
        {"name": "framing", "kind": "framing", "start_at": "10:00", "start_at_s": "10:00:05"}])
    assert asked == ["t-1"]
    assert stored["rows"][0]["walk_key"] == old, "its own old row is about to be replaced"


def test_item_writer_keeps_the_windows_when_walk_assignment_fails(monkeypatch):
    import pytest
    lam = pytest.importorskip("lambda_item_writer")
    tpl = {"id": "t-1", "name": "Framing Checklist", "scope": "org", "body": {}}
    stored = {}
    monkeypatch.setattr(lam.inspection_windows, "checklist_templates", lambda c, co, u: [tpl])
    monkeypatch.setattr(lam.inspection_match, "match", lambda w, ts: (tpl, 1.0))

    def boom(*a):
        raise RuntimeError("db down")
    monkeypatch.setattr(lam.inspection_windows, "open_walks", boom)
    monkeypatch.setattr(lam.inspection_windows, "replace_for_session",
                        lambda c, co, f, d, s, rows: stored.update(rows=rows))

    class Conn:
        def transaction(self):
            import contextlib
            return contextlib.nullcontext()
    lam._store_inspections(Conn(), "c-1", "u-1", FOLDER, DAY, "sidB", [
        {"name": "framing", "kind": "framing", "start_at": "10:00",
         "units": {"names": ["Lot 1", "Lot 2"], "count": 2}}])
    assert [r["walk_key"] for r in stored["rows"]] == [None]
