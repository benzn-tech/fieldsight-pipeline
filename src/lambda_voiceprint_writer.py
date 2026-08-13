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
from repositories.voiceprints import add_sample, record_turn_name

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
    with get_connection() as conn:
        for r in event.get("results") or []:
            asserted = bool(r.get("asserted"))
            record_turn_name(
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
            written += 1

        # One gesture, two effects, ONE transaction. The names describe this meeting; the
        # sample is what makes the person recognisable in the next one, and §6 requires a
        # withdrawal to reach "everything it justified" — which is only enumerable because
        # `correction_ref` travels with the sample.
        enrol = event.get("enrol")
        if enrol:
            if not enrol.get("embedding"):
                raise ValueError(
                    "enrolment carries no embedding; storing a blank would create a profile "
                    "that matches nothing and explains nothing")
            window = enrol.get("window") or (None, None)
            add_sample(conn, company_id, enrol["voiceprint_id"], enrol["embedding"],
                       source="correction", s3_key=enrol.get("s3_key"),
                       window=(window[0], window[1]),
                       created_by=enrol.get("created_by"),
                       correction_ref=correction_ref)
    logger.info("propagation: %d rows for %s (tau=%s, enrolled=%s)",
                written, session_base, tau, bool(event.get("enrol")))
    return {"written": written, "enrolled": bool(event.get("enrol"))}


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
        add_sample(conn, company_id, _require(event, "voiceprint_id"), embedding,
                   source="correction", s3_key=event.get("s3_key"),
                   window=(window[0], window[1]),
                   created_by=event.get("created_by"),
                   correction_ref=event.get("correction_ref"))
    return {"stored": 1}


def lambda_handler(event, context):
    op = (event or {}).get("op")
    if op == "propagation":
        return _propagation(event)
    if op == "enrol":
        return _enrol(event)
    raise ValueError(f"unknown op {op!r} — expected 'propagation' or 'enrol'")
