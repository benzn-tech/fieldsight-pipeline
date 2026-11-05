"""Track L Task 2: one place, spoken many ways, is one key.

Every example here is written for the test -- the same SHAPES Task 1 measured
in real speech, never the customers' own words.
"""
import pytest

from place_normalise import normalise, place_key, same_place


def _same(*texts):
    keys = {place_key(t) for t in texts}
    assert len(keys) == 1, keys


# --- the brief's cases -------------------------------------------------------

def test_level_one_spelled_four_ways_is_one_key():
    _same("Level one", "L1", "first floor", "一楼")
    assert normalise("L1")["label"] == "Level 1"
    assert normalise("一楼")["kind"] == "level"


def test_dotted_room_code_is_the_same_room():
    _same("Room 101", "Room 1.01", "rm 101", "room one oh one")
    assert normalise("Room 1.01")["label"] == "Room 101"


def test_a_qualifier_is_a_narrower_place_not_a_spelling():
    assert not same_place("Level 1", "Level 1 east")
    assert normalise("Level 1 east")["label"] == "Level 1 east"


def test_the_kitchen_is_other():
    got = normalise("  The   Kitchen ")
    assert got["kind"] == "other"
    assert got["value"] == "the kitchen"
    assert got["raw"] == "  The   Kitchen "


@pytest.mark.parametrize("bad", ["", "   ", "\t\n", None])
def test_empty_raises(bad):
    with pytest.raises(ValueError):
        normalise(bad)
    with pytest.raises(ValueError):
        place_key(bad)


# --- levels ------------------------------------------------------------------

@pytest.mark.parametrize("text", ["level two", "Level 2", "lvl 2", "L 2", "floor two",
                                  "2nd floor", "second floor", "storey 2", "二层", "二楼",
                                  "on level two"])
def test_level_two(text):
    got = normalise(text)
    assert (got["kind"], got["label"]) == ("level", "Level 2")


def test_ground_is_level_zero_as_photo_binding_has_it():
    _same("ground floor", "Ground level", "Level 0", "level zero", "level G")
    assert normalise("ground floor")["label"] == "Level 0"


def test_basement_is_below_ground():
    _same("basement", "Basement 1", "basement level one")
    _same("basement two", "Basement 2")
    assert not same_place("basement", "ground floor")
    assert not same_place("basement", "Level 1")
    assert normalise("basement")["kind"] == "level"
    _same("Level -1", "level minus one", "basement")
    _same("Level -2", "Basement 2")
    _same("unit-2", "Unit 2")


def test_chinese_levels():
    _same("三楼", "三层", "Level 3")
    _same("十一楼", "Level 11")


# --- level and zone together -------------------------------------------------

def test_level_and_zone_in_either_order_is_one_place():
    _same("level two, zone four", "zone four, level two", "Zone 4 Level 2")
    got = normalise("zone four, level two")
    assert got["label"] == "Level 2 Zone 4"
    assert got["kind"] == "zone"


def test_level_and_zone_is_narrower_than_the_level():
    assert not same_place("level two", "level two zone four")


@pytest.mark.parametrize("narrower", ["level two slab", "level two grid C4",
                                      "level two corridor", "level two area B"])
def test_level_with_a_qualifier_is_narrower(narrower):
    assert not same_place("level two", narrower)
    assert normalise(narrower)["label"].startswith("Level 2 ")


def test_a_preposition_does_not_narrow_but_a_noun_does():
    _same("level 2 near the lift", "level 2 by the lift", "Level 2 lift")
    assert not same_place("level 2 lift", "level 2")


def test_grid_line_forms():
    _same("grid c4", "Grid line C4", "gridline c4")
    assert normalise("grid c4")["label"] == "Grid C4"


# --- stairs, cores, blocks ---------------------------------------------------

def test_stairs_cores_blocks():
    assert normalise("stair core")["label"] == "Stair core"
    assert normalise("block b")["label"] == "Block B"
    _same("stairwell in block B", "block B stair", "Block B Stair")
    got = normalise("the stairwell in block B")
    assert got["kind"] == "block"
    assert not same_place("block B", "stairwell in block B")
    assert normalise("core two")["label"] == "Core 2"


def test_zone_letter_and_compass():
    got = normalise("zone b east")
    assert got["label"] == "Zone B east"
    assert got["kind"] == "zone"
    _same("east zone B", "zone b east")


# --- rooms and units (normalize_unit) ----------------------------------------

def test_units_reuse_normalize_unit():
    _same("Unit two", "U2", "unit-2")
    assert normalise("U2")["label"] == "Unit 2"
    assert normalise("unit 2a")["label"] == "Unit 2A"
    assert not same_place("Unit 2", "Room 2")


def test_lettered_room_code():
    _same("Room G02", "rm g02", "room G.02")
    assert normalise("room g02")["label"] == "Room G02"
    assert not same_place("Room G02", "Room 2")


def test_room_on_a_level():
    got = normalise("level one room 101")
    assert got["label"] == "Level 1 Room 101"
    assert got["kind"] == "room"


# --- not places in a building ------------------------------------------------

@pytest.mark.parametrize("text", ["Riverside Farm", "the clock tower", "inside", "level",
                                  "the first one", "zone", "lot of studs"])
def test_not_a_building_place_is_other(text):
    got = normalise(text)
    assert got["kind"] == "other"
    assert got["value"] == " ".join(text.split()).casefold()


# --- aliases -----------------------------------------------------------------

def test_alias_pins_a_name_to_a_place():
    aliases = {"the new block": "Block C"}
    assert place_key("The new  block", aliases=aliases) == place_key("block c")
    assert same_place("the new block", "Block C", aliases=aliases)
    assert place_key("the old block", aliases=aliases) == place_key("the old block")


def test_alias_tolerates_the_stored_prefix_and_is_one_hop():
    aliases = {"place:north wing": "place:Block A", "block a": "Block Z"}
    assert place_key("north wing", aliases=aliases) == place_key("Block A")


def test_no_aliases_is_no_change():
    assert place_key("Level 1", aliases=None) == place_key("Level 1", aliases={})


# --- labels are fixed points ---------------------------------------------------

@pytest.mark.parametrize("text", [
    "Level one", "L1", "first floor", "一楼", "ground floor", "basement", "basement two",
    "Room 1.01", "room g02", "U2", "unit 2a", "level two, zone four",
    "zone four, level two", "level two slab", "level two grid C4", "level 1 east",
    "stair core", "the stairwell in block B", "core two", "zone b east",
    "level one room 101", "level two area B", "the kitchen", "Riverside Farm", "inside",
])
def test_place_key_is_idempotent_on_labels(text):
    got = normalise(text)
    assert place_key(got["label"]) == place_key(text)
    assert normalise(got["label"])["label"] == got["label"]


# --- fix round 1: two different places never share a key ---------------------

@pytest.mark.parametrize("two, one", [
    ("level 2 to level 3", "Level 2"),
    ("level two and level three", "Level 2"),
    ("room 101 and room 102", "room 101"),
    ("zone A zone B", "zone A"),
])
def test_two_values_for_one_part_is_other_not_either(two, one):
    assert normalise(two)["kind"] == "other"
    assert not same_place(two, one)


def test_the_same_part_said_twice_is_still_that_place():
    _same("level 2 level 2", "Level 2")


@pytest.mark.parametrize("a, b", [
    ("Room 1.01", "Room 10.1"),
    ("Room 1.10", "Room 11.0"),
    ("Room 1.1", "Room 11"),
    ("Room 1.1", "Room 101"),
])
def test_dotted_codes_merge_only_with_two_digits_after_the_dot(a, b):
    assert not same_place(a, b)


def test_dotted_code_with_two_digits_is_the_plain_code():
    _same("Room 1.10", "Room 110")
    assert normalise("Room 1.1")["label"] == "Room 1.1"


@pytest.mark.parametrize("a, b", [
    ("Level 2 A", "Level 2"),
    ("Level 2 I", "Level 2"),
    ("Room 2 A", "Room 2"),
    ("Level 2 M", "Level 2"),
    ("Level 2 A Zone 4", "Level 2 Zone 4 A"),
])
def test_a_lone_letter_after_a_part_is_a_code_letter(a, b):
    assert not same_place(a, b)


@pytest.mark.parametrize("text, label", [("Room L2", "Room L2"), ("Room U2", "Room U2"),
                                         ("rm l2", "Room L2")])
def test_a_code_after_a_room_word_belongs_to_the_room(text, label):
    got = normalise(text)
    assert (got["kind"], got["label"]) == ("room", label)
    assert not same_place(text, "Level 2")
    assert not same_place(text, "Unit 2")


@pytest.mark.parametrize("text", ["the level ground", "on level ground",
                                  "floor 3 metres from wall", "floor 3 m from the wall",
                                  "level 2 mm out"])
def test_an_adjective_or_a_measurement_is_not_a_level(text):
    assert normalise(text)["kind"] == "other"


def test_level_g_is_still_ground():
    _same("level G", "Level 0")


def test_chinese_basements_are_the_english_basement():
    _same("地下一层", "负一楼", "basement 1", "Basement")
    _same("地下二层", "Basement 2")
    assert not same_place("地下一层", "一楼")


@pytest.mark.parametrize("text", ["Room 1.1", "Room 10.1", "Level 2 A", "Room 2 A", "Room L2",
                                  "Level 2 A Zone 4", "地下一层", "level 2 to level 3"])
def test_fix_round_1_labels_are_fixed_points(text):
    got = normalise(text)
    assert place_key(got["label"]) == place_key(text)


# --- fix round 2 ---------------------------------------------------------------

def test_a_letter_code_is_never_a_measurement():
    keys = {place_key(t) for t in ("level 2 zone A m", "level 2 zone I m", "level 2 zone")}
    assert len(keys) == 3
    assert normalise("level 2 zone A m")["kind"] == "zone"


@pytest.mark.parametrize("measured, place", [
    ("basement 2 m from the wall", "basement 2 from the wall"),
    ("stair 3 m from the wall", "stair 3 from the wall"),
    ("basement 2 m", "basement 2"),
    ("stair 2 m", "stair 2"),
])
def test_a_measured_basement_or_stair_is_other(measured, place):
    assert normalise(measured)["kind"] == "other"
    assert not same_place(measured, place)


@pytest.mark.parametrize("two, one", [
    ("zone A and B", "zone A"),
    ("level 2 and 3", "Level 2"),
    ("level 2 to 3", "Level 2"),
    ("room 101 and 102", "room 101"),
    ("level 2 & 3", "Level 2"),
    ("level 2 through 3", "Level 2"),
    ("level 2到3", "Level 2"),
    ("level 2和3", "Level 2"),
    ("level 2至3", "Level 2"),
])
def test_an_elliptical_second_place_is_other(two, one):
    assert normalise(two)["kind"] == "other"
    assert not same_place(two, one)


def test_a_connector_before_another_part_is_still_one_place():
    _same("level 2 and zone 4", "Level 2 Zone 4")
