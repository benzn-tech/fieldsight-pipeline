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
