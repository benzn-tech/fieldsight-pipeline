from legacy_ticks_backfill import classify


def _row(sk, **kw):
    return {"PK": "ACTIONS#2026-08-17", "SK": sk, "action_text": "Fix the gate",
            "checked": True, "checked_by": "Ben", "checked_at": "2026-08-18T01:02:03Z", **kw}


def test_user_prefixed_sk():
    c = classify(_row("USER#Jarley_Trainor#TOPIC#0#ACTION#0"))
    assert (c["date"], c["folder"], c["topic"], c["action"], c["kind"]) == \
        ("2026-08-17", "Jarley_Trainor", 0, "0", "action")
    assert c["text"] == "Fix the gate" and c["checked"] is True and c["checked_by"] == "Ben"


def test_folderless_sk_has_no_folder():
    c = classify(_row("TOPIC#0#ACTION#3"))
    assert (c["folder"], c["topic"], c["action"], c["kind"]) == (None, 0, "3", "no_folder")


def test_flag_is_a_finding():
    assert classify(_row("TOPIC#0#ACTION#flag_0"))["kind"] == "finding"


def test_negative_topic_obs_is_a_finding():
    c = classify(_row("USER#Jarley_Trainor#TOPIC#-1#ACTION#obs_1"))
    assert c["kind"] == "finding" and c["topic"] == -1


def test_quality_is_a_finding():
    assert classify(_row("USER#Jarley_Trainor#TOPIC#2#ACTION#quality"))["kind"] == "finding"


def test_negative_topic_with_integer_action_is_a_finding():
    assert classify(_row("USER#J#TOPIC#-1#ACTION#4"))["kind"] == "finding"


def test_user_folder_attribute_fills_a_folderless_sk():
    c = classify(_row("TOPIC#1#ACTION#2", user_folder="Jarley_Trainor"))
    assert (c["folder"], c["kind"]) == ("Jarley_Trainor", "action")


def test_unchecked_row_and_text_fallback():
    r = _row("TOPIC#0#ACTION#0", user_folder="X", checked=False)
    del r["action_text"]
    r["text"] = "Alt"
    c = classify(r)
    assert c["checked"] is False and c["text"] == "Alt"
