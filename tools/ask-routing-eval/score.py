"""Ask routing eval: pure scoring (plan 2026-10-16 phase 4, Task 2; spec §9).

Stdlib only, no I/O. `run.py` hands every turn here as

    result = normalize(channel, terminal)   # the turn's terminal state
    route  = the turn's ASK_ROUTE JSON, or None when no line joined

and calls `score_turn(item, result, route)` per turn and `gate(records)` once.

Channel shapes (why `normalize` exists):

* screen: `/api/ask/result` returns `{"status": "done", "result": <the answer
  dict>}` with `citations`, `from_web` and `web_offer`.
* voice: the turn state is `{"status": "done", "answerText", "basis", "web",
  ...}`. It carries no citations and no `web_offer`, so the citation checks
  read `route.refs_r` and the offer is recognised by the fixed sentence the
  orchestrator appends (`voice_web.OFFER_ALSO` / `OFFER_NO_RECORDS`; a unit test
  pins these copies to the real ones).
"""
from __future__ import annotations

import re
from datetime import date

# Copies of voice_web's offer sentences (pinned by a test, not imported: this
# module is stdlib only).
OFFER_ALSO = "Want me to check online as well?"
OFFER_NO_RECORDS = "Your site records don't cover that. Want me to search online?"

PROJECT_GROUPS = ("project_day", "project_empty_day", "person_or_latest", "spoken_date")
# Groups that must pass on every repeat (spec §9: "every project and empty-day
# item passes 3/3"; person_or_latest is a project question too).
ALL_REPS_GROUPS = ("project_day", "project_empty_day", "person_or_latest")
# Groups that must pass on at least two thirds of the repeats. spoken_date is a
# project question, but resolving "3 September" is the judge's own reasoning
# (query_slots #1133 anchors only ISO and "last <weekday>"), so it gets the
# model-judgement tier rather than 3/3.
TWO_THIRDS_GROUPS = ("general", "mixed", "spoken_date")
# Spec §9 names no gate for these; they are reported per item, not gated.
REPORTED_GROUPS = ("explicit_web", "scoped_general", "web_cap")
WEB_GROUPS = ("general", "mixed", "explicit_web", "web_cap")

P50_PROJECT_MS = 7000      # project, screen
P50_GENERAL_MS = 25000     # the web groups, both channels
MAX_GUARD_FIRES = 1

_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December")

_NOTHING = re.compile(
    r"\b(?:nothing\s+(?:was\s+)?(?:recorded|logged|captured|on\s+record)"
    r"|no\s+(?:site\s+)?(?:records?|recordings?|activity|entries)"
    r"|(?:didn't|did\s+not|wasn't|was\s+not|weren't|were\s+not)\s+"
    r"(?:any(?:thing)?\s+)?(?:record|recorded|logged|captured)"
    r"|there\s+(?:is|are|was|were)\s+no\b)", re.I)

# A ref token the client should never see: [R1], [W2], [R1-R3], [W1, R2].
_LEAKED_REF = re.compile(r"\[\s*[RW]\d+")
_NUMERIC = re.compile(r"\[(\d+)\]")


def _ordinal(n):
    if 10 <= n % 100 <= 20:
        return "%dth" % n
    return "%d%s" % (n, {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))


def date_forms(iso):
    """The spellings of one ISO date an answer may use."""
    d = date.fromisoformat(iso)
    month, short = _MONTHS[d.month - 1], _MONTHS[d.month - 1][:3]
    day, ordn = str(d.day), _ordinal(d.day)
    return [iso, "%s %s" % (day, month), "%s %s" % (month, day),
            "%s %s" % (ordn, month), "%s %s" % (month, ordn),
            "%s of %s" % (ordn, month), "%s %s" % (day, short), "%s %s" % (short, day),
            "the %s" % ordn]


def mentions_date(text, iso):
    if not text or not iso:
        return False
    low = text.lower()
    for form in date_forms(iso):
        f = form.lower()
        if f == iso:
            if f in low:
                return True
            continue
        # Word-bounded so "3 September" does not match inside "23 September".
        if re.search(r"(?<![\w])%s(?![\w])" % re.escape(f), low):
            return True
    return False


def says_nothing_recorded(text):
    return bool(text) and bool(_NOTHING.search(text))


def normalize(channel, terminal):
    """One turn's terminal state as the scorer reads it (see the module doc)."""
    terminal = terminal if isinstance(terminal, dict) else {}
    status = terminal.get("status")
    if channel == "voice":
        answer = terminal.get("answerText") or ""
        stripped = answer.rstrip()
        return {"channel": "voice", "status": status, "answer": answer,
                "citations": None, "from_web": bool(terminal.get("web")),
                "web_offer": stripped.endswith(OFFER_ALSO)
                or stripped.endswith(OFFER_NO_RECORDS),
                "error": terminal.get("error")}
    res = terminal.get("result") if isinstance(terminal.get("result"), dict) else {}
    return {"channel": "screen", "status": status, "answer": res.get("answer") or "",
            "citations": list(res.get("citations") or []),
            "from_web": bool(res.get("from_web")), "web_offer": bool(res.get("web_offer")),
            "error": terminal.get("error") or res.get("error")}


def flat_calls(route):
    out = []
    for rnd in (route or {}).get("calls") or []:
        if isinstance(rnd, list):
            out.extend(str(x) for x in rnd)
        else:
            out.append(str(rnd))
    return out


def _cites(result, route):
    """Citation count: the screen's cards; on voice (no cards) the R refs cited."""
    if result.get("citations") is None:
        return int(route.get("refs_r") or 0)
    return len(result["citations"])


def _check(why, ok, label):
    why.append(("ok: " if ok else "FAIL: ") + label)
    return ok


def score_turn(item, result, route):
    """{"verdict": "pass" | "fail" | "unscored", "why": [...]} for one turn."""
    if not isinstance(route, dict):
        return {"verdict": "unscored", "why": ["no ASK_ROUTE line joined on this turn"]}
    if not isinstance(result, dict) or result.get("status") != "done":
        st = (result or {}).get("status") if isinstance(result, dict) else None
        return {"verdict": "fail", "why": ["FAIL: turn ended with status=%s" % st]}

    group, expect = item.get("group"), item.get("expect") or {}
    channel = result.get("channel")
    answer = result.get("answer") or ""
    refs_r, refs_w = int(route.get("refs_r") or 0), int(route.get("refs_w") or 0)
    pol = route.get("policy")
    why = []
    checks = []

    if group == "project_day":
        checks.append(_check(why, _cites(result, route) >= 1, "at least one records citation"))
        checks.append(_check(why, not result.get("from_web"), "from_web is falsy"))
        checks.append(_check(why, refs_w == 0, "route.refs_w == 0"))
    elif group == "spoken_date":
        want = [str(d) for d in (expect.get("dates") or [])]
        checks.append(_check(why, "day_overview" in flat_calls(route),
                             "route.calls includes day_overview (the judge resolved the date)"))
        in_cards = any(str(c.get("report_date") or "") in want
                       for c in (result.get("citations") or []))
        checks.append(_check(why, in_cards or any(mentions_date(answer, d) for d in want),
                             "a card or the answer is from %s" % ",".join(want)))
        checks.append(_check(why, not result.get("from_web") and refs_w == 0, "no W"))
    elif group == "project_empty_day":
        lb = expect.get("latest_before")
        checks.append(_check(why, not result.get("from_web") and refs_w == 0, "no W"))
        named, nothing = mentions_date(answer, lb), says_nothing_recorded(answer)
        why.append("note: names latest_before %s=%s; says nothing recorded=%s"
                   % (lb, named, nothing))
        checks.append(_check(why, named or nothing,
                             "names latest_before or says nothing was recorded"))
        checks.append(_check(why, "day_overview" in flat_calls(route),
                             "route.calls includes day_overview"))
    elif group == "person_or_latest":
        want = expect.get("latest")
        checks.append(_check(why, _cites(result, route) >= 1, "at least one records citation"))
        in_cards = any(str(c.get("report_date") or "") == want
                       for c in (result.get("citations") or []))
        checks.append(_check(why, mentions_date(answer, want) or in_cards,
                             "expected date %s in the answer or a citation" % want))
    elif group == "general":
        if channel == "voice":
            ok = bool(result.get("web_offer")) or (
                pol in ("auto", "required") and bool(result.get("from_web")))
            checks.append(_check(why, ok, "voice: web offered (or web answer under %s)" % pol))
        else:
            checks.append(_check(why, pol in ("auto", "required"),
                                 "policy is auto/required (got %s)" % pol))
            checks.append(_check(why, bool(result.get("from_web")), "from_web is true"))
        why.append("note: refs_r=%d" % refs_r)
    elif group == "mixed":
        checks.append(_check(why, refs_r >= 1, "route.refs_r >= 1"))
        if channel == "voice" and pol == "offer":
            checks.append(_check(why, bool(result.get("web_offer")), "voice: offer appended"))
        else:
            checks.append(_check(why, refs_w >= 1, "route.refs_w >= 1"))
    elif group == "explicit_web":
        checks.append(_check(why, bool(route.get("web_ran")), "route.web_ran"))
    elif group == "scoped_general":
        checks.append(_check(why, pol == "never", "policy == never (got %s)" % pol))
        checks.append(_check(why, refs_w == 0, "route.refs_w == 0"))
    elif group == "web_cap":
        # Round 1's web run is not in route.calls, and a refused second request
        # still appears there as "web_search": the cap holding is the code
        # answering it `not_allowed`, so later requests are a note, not a fail.
        later = flat_calls({"calls": (route.get("calls") or [])[1:]}).count("web_search")
        checks.append(_check(why, bool(route.get("web_ran")), "route.web_ran"))
        why.append("note: web_search requested in judge rounds %d time(s)" % later)
    else:
        return {"verdict": "unscored", "why": ["unknown group %r" % group]}

    return {"verdict": "pass" if all(checks) else "fail", "why": why}


# ------------------------------------------------------------------------- gate

def guard_fired(route):
    """The judge's raw verdict, not the client answer (Review Focus 4)."""
    route = route or {}
    return bool(route.get("guard")) or route.get("fallback") == "guard"


def leaked_refs(result):
    """Ref tokens in the client answer: [R1]/[W1] anywhere, or on the screen a
    [n] with no card behind it; on voice any [n] at all."""
    if not isinstance(result, dict):
        return 0
    answer = result.get("answer") or ""
    n = len(_LEAKED_REF.findall(answer))
    cards = result.get("citations")
    for m in _NUMERIC.finditer(answer):
        if cards is None or int(m.group(1)) > len(cards) or int(m.group(1)) < 1:
            n += 1
    return n


def _median(values):
    vals = sorted(values)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _ms_total(route):
    ms = (route or {}).get("ms") or {}
    v = ms.get("total")
    return v if isinstance(v, (int, float)) else None


def gate(records):
    """Spec §9 gates over a whole run. `records`: one dict per turn with keys
    `item`, `channel`, `result` (normalized), `route` (or None) and `verdict`
    (from score_turn). Returns {"pass": bool, "lines": [...]}; every line
    starts PASS / FAIL / INFO, and per-item lines follow the gate lines."""
    lines, ok = [], True

    def line(passed, text, gated=True):
        nonlocal ok
        if gated and not passed:
            ok = False
        lines.append(("PASS " if passed else "FAIL ") + text if gated else "INFO " + text)

    per = {}
    for r in records:
        key = (r["item"]["id"], r["channel"])
        per.setdefault(key, {"item": r["item"], "verdicts": []})
        per[key]["verdicts"].append(r["verdict"]["verdict"])

    unscored = sum(1 for r in records if r["verdict"]["verdict"] == "unscored")
    line(unscored == 0, "unscored turns: %d (must be 0)" % unscored)

    for (iid, ch), v in sorted(per.items()):
        group = v["item"].get("group")
        n = len(v["verdicts"])
        passed = v["verdicts"].count("pass")
        if group in ALL_REPS_GROUPS:
            line(passed == n and n > 0, "%s/%s [%s] %d/%d (needs all)" % (iid, ch, group,
                                                                         passed, n))
        elif group in TWO_THIRDS_GROUPS:
            line(n > 0 and passed * 3 >= 2 * n,
                 "%s/%s [%s] %d/%d (needs >= 2/3)" % (iid, ch, group, passed, n))
        else:
            line(passed == n, "%s/%s [%s] %d/%d (reported, not gated)"
                 % (iid, ch, group, passed, n), gated=False)

    leaked = sum(leaked_refs(r.get("result")) for r in records)
    line(leaked == 0, "bad refs reaching the client: %d (must be 0)" % leaked)
    raw_bad = sum(int((r.get("route") or {}).get("bad_refs") or 0) for r in records)
    line(True, "route.bad_refs stripped before the client: %d" % raw_bad, gated=False)

    fires = [r for r in records if guard_fired(r.get("route"))]
    on_project = [r for r in fires if r["item"].get("group") in PROJECT_GROUPS]
    line(len(fires) <= MAX_GUARD_FIRES,
         "guard fires: %d in total (must be <= %d)" % (len(fires), MAX_GUARD_FIRES))
    line(not on_project, "guard fires on project items: %d (must be 0)" % len(on_project))

    proj = [_ms_total(r.get("route")) for r in records
            if r["item"].get("group") in PROJECT_GROUPS and r["channel"] == "screen"]
    proj = [v for v in proj if v is not None]
    p50 = _median(proj)
    if p50 is None:
        line(True, "p50 latency project/screen: n/a (no turns)", gated=False)
    else:
        line(p50 <= P50_PROJECT_MS, "p50 latency project/screen: %.1f s (must be <= %.0f s)"
             % (p50 / 1000.0, P50_PROJECT_MS / 1000.0))
    gen = [_ms_total(r.get("route")) for r in records if r["item"].get("group") in WEB_GROUPS]
    gen = [v for v in gen if v is not None]
    p50 = _median(gen)
    if p50 is None:
        line(True, "p50 latency general: n/a (no turns)", gated=False)
    else:
        line(p50 <= P50_GENERAL_MS, "p50 latency general: %.1f s (must be <= %.0f s)"
             % (p50 / 1000.0, P50_GENERAL_MS / 1000.0))

    return {"pass": ok, "lines": lines}
