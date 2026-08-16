"""Lambda: voiceprint-writer — the in-VPC half of the voiceprint chain.

The embedder holds the model and the audio; this holds the database. The split is not tidiness:
the embedder runs on **python3.12** because that is where onnxruntime comes from
(`fieldsight-vad-layer` is cp312-only) and **`PsycopgLayer` is cp311-only**, so one function
cannot have both. Giving the embedder a connection anyway is what made it raise
`ModuleNotFoundError` on every invocation while the deploy stayed green.

**Invoked directly by the embedder.** Non-VPC → in-VPC is permitted (BUG-43 note 4: the callee
is only a target and initiates nothing outward), and the note warns by name against forcing an
S3 hop where a direct invoke belongs, since BUG-33 makes every new S3 trigger hand-wired. The
`Matcher → SuggestionWriter` pair runs exactly this way.

That choice is also what keeps the enrolment vector **out of S3 entirely**: it travels in the
invoke payload and lands in the column that already requires consent. The design reviews chased
that vector through two homes — an embedding cache, then a request artifact — and it only
stopped moving once the storage turned out not to need to exist.

Event shapes:

    {"op": "propagation", "company_id", "session_base", "correction_ref"?,
     "voiceprint_id"?, "cluster_threshold"?,
     "results": [{"turn_ref", "state", "cluster_ref", "score"?, "margin"?,
                  "label_disagreement"?, "asserted"?}, ...]}   -> {"written": N}

    {"op": "enrol", "company_id", "voiceprint_id", "embedding", "s3_key", "window",
     "correction_ref"?, "created_by"?}                          -> {"stored": 0|1}

Spec: docs/superpowers/specs/2026-08-13-speaker-correction-propagation.md
Plan: docs/superpowers/plans/2026-08-13-correction-propagation-implementation.md (P4)
"""
import logging

from db.connection import get_connection
from repositories.voiceprints import (EnrolmentBelongsToSomebodyElse, add_sample,
                                      live_turn_names, profiles_for_matching,
                                      record_turn_name, rejected_names)
from turn_name_overlay import _SOURCE_RANK

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _require(event, key):
    value = (event or {}).get(key)
    if not value:
        raise ValueError(f"{key} is required — an absent one would write rows nobody can "
                         f"scope, and this table is read per company")
    return value


def _propagation(event):
    """One row per turn, replacing whatever was live for it.

    `record_turn_name` supersedes then inserts inside this transaction, so a second run
    arriving concurrently replaces rather than colliding with the partial unique index.

    Two things about `voiceprint_id` that look like details and are not:

    * propagated rows carry **None**. `confirmations_count` treats a confirmed row as an
      independent *human* confirmation and promotes the profile after N of them; a machine
      row with a profile id would let the system promote profiles from its own output.
    * the turn the user actually asserted is written `source='correction'` — the one row a
      person vouched for, and what the promotion count is supposed to count.

      It carries `voiceprint_id` only when the caller supplies one, and today nothing does:
      a correction creates no profile (that is Phase 4, and consent is its precondition), so
      every asserted row has a NULL id and `confirmations_count` is structurally zero. That
      is the safe direction — promotion can only under-count — but it is two halves waiting
      to disagree, so it is written down here rather than discovered when Phase 4 lands.
    """
    company_id = _require(event, "company_id")
    session_base = _require(event, "session_base")
    tau = event.get("cluster_threshold")
    correction_ref = event.get("correction_ref")
    vp_id = event.get("voiceprint_id")

    written = 0
    declined = 0
    with get_connection() as conn:
        for r in event.get("results") or []:
            asserted = bool(r.get("asserted"))
            row = record_turn_name(
                conn, company_id,
                session_base=session_base,
                turn_ref=r["turn_ref"],
                state=r["state"],
                source="correction" if asserted else "correction_propagation",
                correction_ref=correction_ref,
                cluster_ref=r.get("cluster_ref"),
                cluster_threshold=tau,
                # The asserted row may carry one from the embedder (so a withdrawal can
                # reach its name when the enrolment was refused); a propagated row never
                # does, which is what keeps confirmations_count counting humans.
                voiceprint_id=(r.get("voiceprint_id") or vp_id) if asserted else None,
                score=r.get("score"),
                margin=r.get("margin"),
                label_disagreement=r.get("label_disagreement"),
                # Carried all the way from the correction body. Without a column to land in
                # it was dropped here without a word, and the row named nobody.
                display_name=r.get("display_name"))
            # None means the write was DECLINED, not that it failed: a live row from a
            # stronger source holds the turn. Counting it as written would report a
            # propagation that covered turns it never touched.
            if row is None:
                declined += 1
            else:
                written += 1

        # One gesture, two effects, ONE transaction. The names describe this meeting; the
        # sample is what makes the person recognisable in the next one, and §6 requires a
        # withdrawal to reach "everything it justified" — which is only enumerable because
        # `correction_ref` travels with the sample.
        enrol = event.get("enrol")
        enrol_refusal = None
        if enrol:
            if not enrol.get("embedding"):
                raise ValueError(
                    "enrolment carries no embedding; storing a blank would create a profile "
                    "that matches nothing and explains nothing")
            window = enrol.get("window") or (None, None)
            try:
                add_sample(conn, company_id, enrol["voiceprint_id"], enrol["embedding"],
                           source="correction", s3_key=enrol.get("s3_key"),
                           window=(window[0], window[1]),
                           created_by=enrol.get("created_by"),
                           correction_ref=correction_ref)
            except EnrolmentBelongsToSomebodyElse as exc:
                # Caught, not propagated: the names describe THIS meeting and were earned by
                # the user's own assertion. Letting a refused enrolment roll them back would
                # punish the gesture for the half of it that needs consent, which is the
                # half the API already reports separately.
                enrol_refusal = {"reason": "closer-to-another-profile",
                                 "own": exc.own, "bestOther": exc.best_other,
                                 "nearestOtherId": str(exc.nearest_other_id)}
                logger.warning("enrolment refused for %s: %s", enrol["voiceprint_id"], exc)
        inherited = _inherit_labels(conn, company_id, session_base,
                                    event.get("label_map"))
    enrolled = bool(event.get("enrol")) and enrol_refusal is None
    logger.info("propagation: %d rows for %s (tau=%s, enrolled=%s, refused=%s)",
                written, session_base, tau, enrolled,
                (enrol_refusal or {}).get("reason"))
    return {"written": written, "declined": declined, "inherited": inherited,
            "enrolled": enrolled, "enrolRefused": enrol_refusal}


def _enrol(event):
    """Store one embedded window, or store nothing and say so.

    A `refused` result is the embedder declining a window it could not judge as one voice.
    Storing it anyway would poison a profile permanently — a profile cannot be un-poisoned,
    only the contributing sample deleted — so the refusal is honoured here rather than being
    treated as a missing field.
    """
    company_id = _require(event, "company_id")
    if event.get("status") == "refused":
        logger.warning("enrol refused upstream (%s); nothing stored",
                       event.get("reason"))
        return {"stored": 0, "reason": event.get("reason")}

    embedding = event.get("embedding")
    if not embedding:
        raise ValueError("enrol result carries no embedding and was not marked refused — "
                         "storing a blank would create a profile that matches nothing and "
                         "explains nothing")
    window = event.get("window") or (None, None)
    with get_connection() as conn:
        try:
            add_sample(conn, company_id, _require(event, "voiceprint_id"), embedding,
                       source="correction", s3_key=event.get("s3_key"),
                       window=(window[0], window[1]),
                       created_by=event.get("created_by"),
                       correction_ref=event.get("correction_ref"))
        except EnrolmentBelongsToSomebodyElse as exc:
            # Same refusal as the propagation path, and it has to be spelled out twice
            # because the two paths report different shapes. What must NOT differ is the
            # decision, which is why the rule itself lives in `add_sample`.
            logger.warning("enrol refused: %s", exc)
            return {"stored": 0, "reason": "closer-to-another-profile",
                    "own": exc.own, "bestOther": exc.best_other,
                    "nearestOtherId": str(exc.nearest_other_id)}
    return {"stored": 1}


def _profiles(event):
    """The company's matchable profiles, handed to the non-VPC embedder.

    **This is the only way voice vectors may reach the matcher.** They travel in the
    RequestResponse *result* of a synchronous invoke and nowhere else — not through S3, not
    through an async payload that would sit in Lambda's queue and any DLQ. The same defect
    has relocated four times already, each time into a store nobody thought to sweep, so the
    rule is not "avoid S3" but "the vectors are never at rest outside the column that
    requires consent".

    `profiles_for_matching` stays in this half for the reason its own docstring gives: a
    withdrawn profile that still matches is not a withdrawal, and that filter must have
    exactly one home.
    """
    company_id = _require(event, "company_id")
    with get_connection() as conn:
        rows = profiles_for_matching(conn, company_id, site_id=event.get("site_id"))
    return {"profiles": [{"person_key": str(r["id"]),
                          "display_name": r["display_name"],
                          "status": r["status"],
                          "embedding": r["embedding"]} for r in rows]}


def _match_names(event):
    """Names a matcher inferred, with no human behind them.

    `source='voiceprint_match'`, never `'correction'`. `confirmations_count` counts rows
    whose source is `'correction'` and promotes a profile after enough of them — so writing
    machine output under that source would let the system promote its own profiles from its
    own guesses, and the promotion would then justify more confident guesses.

    These rows DO carry `voiceprint_id`, unlike propagated ones. §6 requires a withdrawal to
    reach everything the profile justified, and `withdraw` supersedes turn names by exactly
    that column. A matched name with no id is a name the withdrawal cannot find.
    """
    company_id = _require(event, "company_id")
    session_base = _require(event, "session_base")
    written = 0
    declined = 0
    with get_connection() as conn:
        for r in event.get("results") or []:
            if r.get("status") not in ("confirmed", "tentative") or not r.get("person_key"):
                continue
            row = record_turn_name(
                conn, company_id,
                session_base=session_base,
                turn_ref=r["turn_ref"],
                state=r["status"],
                source="voiceprint_match",
                voiceprint_id=r["person_key"],
                display_name=r.get("display_name"),
                score=r.get("score"),
                margin=r.get("margin"))
            if row is None:
                # A human already named this turn. The match is not wrong to have run; it is
                # wrong to overwrite, and reporting the difference is what keeps "the matcher
                # found nothing" apart from "the matcher deferred to somebody".
                declined += 1
            else:
                written += 1
        inherited = _inherit_labels(conn, company_id, session_base,
                                    event.get("label_map"))
    logger.info("match: %d names for %s (%d declined to a stronger source)",
                written, session_base, declined)
    return {"written": written, "declined": declined, "inherited": inherited}


def _inherit_labels(conn, company_id, session_base, label_map):
    """Spread settled names across the transcriber's own speaker groups.

    The transcriber already said "these turns are one voice". For turns under the 3 s floor
    that is the ONLY evidence there is — an embedding of a short turn is not weak, it is
    actively misleading (Phase 0's one miss was 2.1 s and scored its own speaker lowest). So
    this tier carries no duration floor, because the evidence it uses is not acoustic.

    Runs here rather than in the matcher for one reason: `_split_for_budget` chops a long
    session into independent runs on cumulative duration, with no idea that two turns share a
    label. A group's one long turn can land in run 1 and its short turns in run 2, which then
    sees nothing named and inherits nothing, silently. The label map arrives whole in every
    run, so the outcome does not depend on where the split fell.

    Three things that are not decoration:

    * **provenance travels.** An inherited row carries the `voiceprint_id` / `correction_ref`
      of the row it came from, or `withdraw` returns 200 while the inherited names stay on
      the transcript — the exact failure the withdrawal fix was written for, through a new
      door.
    * **a rejection is honoured.** A name a person took off this session is never re-derived,
      or the user deletes it again after every run with nothing logged.
    * **grouping is on (source_filename, label).** Speaker labels are per transcription call;
      batching merges namespaces across chunks on purpose, and every segment of a batch
      carries the batch object's name — so the filename IS the call identity. Grouping on the
      label alone would merge two calls' `spk_0` into one person.
    """
    if not label_map:
        return 0
    groups = {}
    for t in label_map:
        label = t.get("speaker_label")
        ref = t.get("turn_ref")
        # An absent label is not a group. `speaker` is coerced to "spk_0" for display, and
        # keying on that would make an undiarised file one speaker from end to end.
        if not label or not ref:
            continue
        groups.setdefault((t.get("source_filename"), label), []).append(ref)

    live = {r["turn_ref"]: r for r in live_turn_names(conn, company_id, session_base)}
    refused = rejected_names(conn, company_id, session_base)
    written = 0
    for refs in groups.values():
        named = [live[r] for r in refs if r in live and live[r].get("display_name")]
        if not named:
            continue
        best = max(named, key=lambda r: _SOURCE_RANK.get(r.get("source"), -1))
        if best.get("source") == "label_inheritance":
            # Nothing to spread: the group's only name is one this tier wrote itself.
            continue
        name = best["display_name"]
        if name in refused:
            logger.info("inheritance: %r was rejected for %s; not re-derived",
                        name, session_base)
            continue
        for ref in refs:
            if ref in live:
                continue
            row = record_turn_name(
                conn, company_id, session_base=session_base, turn_ref=ref,
                state="tentative", source="label_inheritance",
                display_name=name,
                # Provenance of the row it inherited FROM, so a withdrawal reaches it.
                voiceprint_id=best.get("voiceprint_id"),
                correction_ref=best.get("correction_ref"))
            if row is not None:
                written += 1
    logger.info("inheritance: %d turns named from transcriber labels in %s",
                written, session_base)
    return written


def lambda_handler(event, context):
    op = (event or {}).get("op")
    if op == "propagation":
        return _propagation(event)
    if op == "enrol":
        return _enrol(event)
    if op == "profiles":
        return _profiles(event)
    if op == "match_names":
        return _match_names(event)
    raise ValueError(f"unknown op {op!r} — expected 'propagation', 'enrol', 'profiles' or "
                     f"'match_names'")
