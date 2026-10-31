"""The action-pack verifier seam (spec 2026-11-01 background action agent §4.4, §14 S6;
Task 12 implements it). Task 11's stage runner calls `verify` after Write and the web
snapshots; until Task 12 lands it raises NotImplementedError, so a claimed pack fails
`internal` (reachable only with ACTION_PACKS internal/on).

Contract (what action_pack_stages passes and expects):

    verify(draft, *, registry, items, sources, ctx, writer_model, deadline) -> {
        "conclusion": [{"text": str, "refs": [ref], "confidence": "supported"|"uncertain"}],
            # the verifier maps its own labels: weak -> "uncertain", unsupported -> dropped
        "actions": [{...the draft action..., "verifier": "supported"|"weak"|"unsupported"}],
            # code-check failures are dropped (or labelled unsupported); unsupported
            # actions are KEPT (stored for the eval, never applicable -- S15)
        "model": str,          # the verifier model actually used (after its fallbacks)
        "counts": {str: int},  # counts only, merged into run_trace["verify"]
        "tokens": int,         # optional, added to the run's token count
    }

  draft        -- {"conclusion": [...], "actions": [...]} as Write produced it (validated:
                  every ref is a citable ref of this run, close/update targets are in
                  `items`, `proposed` matches action_pack_view.APPLY_EDIT_FIELDS)
  registry     -- {ref: entry}: every citable ref with its full excerpt as the tool
                  returned it (entry keys: ref, kind, provenance, excerpt, and the source
                  fields of repositories/action_packs.py)
  items        -- {stable_id: items-tool row} (latest read wins)
  sources      -- {ref: source row} for the cited refs, web_source rows already carrying
                  snapshot_s3_key (None = no snapshot: cap the claims at weak, §6)
  writer_model -- the writer model actually used (S6: the verifier must differ in family)
  deadline     -- absolute ctx.clock() deadline of the verify stage

Failures raise a lambda_action_agent.StageError subclass (the pack's failure reason); S6's
"no remaining verifier of a different family" is reason `model` per the Task 12 brief.
"""


def verify(draft, *, registry, items, sources, ctx, writer_model, deadline):
    raise NotImplementedError("the action-pack verifier lands in Task 12")
