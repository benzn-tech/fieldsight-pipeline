"""The downloaded report has a name a person can read -- in any script.

Every report anybody downloaded arrived as a uuid, because the browser takes
the filename from the last segment of the presigned URL and the S3 key ends in
the requestId. These tests pin the name that Content-Disposition now supplies.

THE test is `a chinese name survives`. If only one test here survives, keep that
one: ASCII normalisation has already erased Chinese from this codebase once,
and an all-English suite did not notice.
"""
import re

import pytest

import report_download_name as dn


# ---- THE test ---------------------------------------------------------------

def test_a_chinese_name_survives_into_the_header():
    name = dn.display_name("林本_UCPK2", "personal-meeting", 3, "2026-09-10")
    assert "林本" in name

    header = dn.content_disposition(name)
    # The real name rides on filename*, percent-encoded as UTF-8.
    star = re.search(r"filename\*=UTF-8''(\S+)$", header).group(1)
    from urllib.parse import unquote
    assert unquote(star) == name
    assert "林本" in unquote(star)


def test_the_ascii_fallback_degrades_but_never_vanishes():
    """The half that the old trap would have got wrong."""
    name = dn.display_name("林本", None, None, "2026-09-10")
    header = dn.content_disposition(name)
    fallback = re.search(r'filename="([^"]*)"', header).group(1)
    assert fallback.endswith(".docx")
    stem = fallback[: -len(".docx")]
    assert stem, "the fallback stem must never be empty"
    assert stem not in ("_", "__", "."), "a stem of separators carries nothing"
    assert "2026-09-10" in fallback, "the parts that ARE ascii must be kept"


def test_a_name_that_is_entirely_non_ascii_still_has_an_ascii_stem():
    header = dn.content_disposition("林本.docx")
    fallback = re.search(r'filename="([^"]*)"', header).group(1)
    assert fallback != ".docx", "a client honouring this would save a dotfile"
    assert fallback.endswith(".docx")
    assert len(fallback) > len(".docx")


# ---- the name itself --------------------------------------------------------

def test_who_then_what_then_when():
    assert dn.display_name("Ben_UCPK2", "personal-meeting", 3, "2026-09-10") == \
        "Ben_UCPK2_personal-meeting-v3_2026-09-10.docx"


def test_no_template_named_is_just_a_report_not_a_none():
    name = dn.display_name("Ben_UCPK2", None, None, "2026-09-10")
    assert name == "Ben_UCPK2_report_2026-09-10.docx"
    assert "None" not in name


def test_the_date_is_the_day_the_report_is_about():
    """Two people who ran the same day's report on different days hold files
    whose names agree."""
    monday = dn.display_name("Ben_UCPK2", "personal-meeting", 3, "2026-09-10")
    tuesday = dn.display_name("Ben_UCPK2", "personal-meeting", 3, "2026-09-10")
    assert monday == tuesday


def test_template_label_carries_the_version():
    assert dn.template_label("personal-meeting", 3) == "personal-meeting-v3"
    assert dn.template_label("personal-meeting", None) == "personal-meeting"
    assert dn.template_label(None, None) == "report"
    assert dn.template_label("", 3) == "report"


# ---- nothing here may break a filesystem or a header ------------------------

@pytest.mark.parametrize("bad", ["a/b", "a\\b", "a:b", "a*b", "a?b", 'a"b',
                                 "a<b", "a>b", "a|b"])
def test_path_and_shell_characters_never_reach_the_name(bad):
    name = dn.display_name(bad, None, None, "2026-09-10")
    for ch in '/\\:*?"<>|':
        assert ch not in name


def test_a_newline_in_a_folder_cannot_break_out_of_the_header():
    """Header injection. A folder name is data from the database, not a literal."""
    header = dn.content_disposition(
        dn.display_name("Ben\r\nX-Evil: yes", None, None, "2026-09-10"))
    assert "\r" not in header and "\n" not in header


def test_the_header_is_pure_ascii_whatever_the_name_is():
    header = dn.content_disposition(dn.display_name("林本_工地", "报告", 1, "2026-09-10"))
    header.encode("ascii")          # raises if anything non-ascii slipped through


def test_a_quote_cannot_close_the_quoted_filename_early():
    header = dn.content_disposition(dn.display_name('Ben"; rm -rf /', None, None, "2026-09-10"))
    assert len(re.findall(r'filename="[^"]*"', header)) == 1


# ---- degenerate input -------------------------------------------------------

def test_an_empty_folder_still_produces_a_usable_name():
    name = dn.display_name("", None, None, "2026-09-10")
    assert name == "report_2026-09-10.docx"


def test_everything_missing_is_still_a_file_not_an_extension():
    assert dn.display_name(None, None, None, None) == "report.docx"


def test_runs_of_separators_collapse():
    assert dn.display_name("Ben   __  Lin", None, None, "2026-09-10") == \
        "Ben_Lin_report_2026-09-10.docx"


def test_a_very_long_folder_cannot_produce_an_unusable_header():
    name = dn.display_name("B" * 400, "personal-meeting", 3, "2026-09-10")
    assert len(name) <= 120 + len(".docx")
    dn.content_disposition(name).encode("ascii")


def test_the_same_name_spelled_two_ways_gives_one_filename():
    """NFC: a combining sequence and its precomposed form are one name."""
    precomposed = dn.display_name("René", None, None, "2026-09-10")
    combining = dn.display_name("René", None, None, "2026-09-10")
    assert precomposed == combining


# ---- the name, not the id ---------------------------------------------------

def test_THE_test_a_library_template_is_named_not_uuided():
    """The first report generated from a Library template came out as
    `Ben_UCPK2_df153e1c-fef4-4c0a-bdb3-84fbb507c2ec-v3_2026-08-12.docx`, which
    is a database key with a date on the end. The id identifies the template to
    the system; the NAME is what the person who made it called it, and the
    filename is for them."""
    name = dn.display_name("Ben_UCPK2", "df153e1c-fef4-4c0a-bdb3-84fbb507c2ec", 3,
                           "2026-08-12", template_name="Site Daily Report")
    assert name == "Ben_UCPK2_Site_Daily_Report-v3_2026-08-12.docx"
    assert "df153e1c" not in name


def test_a_chinese_template_name_reaches_the_filename():
    """The slug rule keeps CJK -- it strips what filesystems refuse, not what
    is unfamiliar. The ASCII fallback in the header covers old clients."""
    name = dn.display_name("Ben_UCPK2", "df153e1c-fef4-4c0a-bdb3-84fbb507c2ec", 1,
                           "2026-08-12", template_name="每日工地报告")
    assert "每日工地报告" in name
    assert "df153e1c" not in name
    dn.content_disposition(name).encode("ascii")   # header stays ascii


def test_no_name_falls_back_to_the_id_rather_than_nothing():
    """Old artifacts carry no name. A uuid is a poor filename; an empty
    segment is a worse one."""
    name = dn.display_name("Ben_UCPK2", "df153e1c-fef4-4c0a-bdb3-84fbb507c2ec", 3,
                           "2026-08-12")
    assert "df153e1c" in name


def test_a_file_template_is_unaffected():
    """Their ids are slugs and were always readable."""
    assert dn.display_name("Ben_UCPK2", "personal-meeting", 3, "2026-08-12") == \
        "Ben_UCPK2_personal-meeting-v3_2026-08-12.docx"


def test_a_name_that_survives_nothing_falls_back_to_the_id():
    name = dn.display_name("Ben_UCPK2", "df153e1c-fef4-4c0a-bdb3-84fbb507c2ec", 2,
                           "2026-08-12", template_name="///")
    assert "df153e1c" in name


def test_a_template_named_after_its_own_id_still_does_not_print_a_uuid_twice():
    name = dn.display_name("Ben_UCPK2", "df153e1c-fef4-4c0a-bdb3-84fbb507c2ec", 2,
                           "2026-08-12",
                           template_name="df153e1c-fef4-4c0a-bdb3-84fbb507c2ec")
    assert name.count("df153e1c") == 1
