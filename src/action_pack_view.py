"""Action packs: the pure half of org-api's pack routes and internal tasks (spec
docs/superpowers/specs/2026-11-01-background-action-agent-design.md §7, §10, §14 S1, S9,
S13, S15). No database, no AWS: org-api (lambda_org_api.action_packs_route and the
action_pack_* internal tasks) gathers the facts and these functions decide and shape.

* `mode()` / `route_allowed(caller)` -- the ACTION_PACKS switch, read per call.
* `validate_create_body(body)` -- the POST /action-packs whitelist.
* `store_payload_error(payload)` / `payload_shape_error(payload)` -- the internal store
  task's size and shape gates.
* `shape_pack(...)` -- the GET one view after the S13 re-check (removed sources lose
  everything but their ref; claims/actions citing only removed sources are dropped).
* `render_markdown(view)` -- GET /action-packs/{id}/export?format=md.

Nothing here logs: the callers log ids and counts only.
"""
import datetime as dt
import json
import os
import re
import uuid

# A source ref as the worker writes it (R3, D2, W5); anything else in a path is a 404.
REF_RE = re.compile(r"^[A-Za-z]{1,4}[0-9]{1,5}$")
# The web snapshot viewer returns at most this much stored text (spec §14 S5 keeps 200 kB).
SNAPSHOT_MAX_BYTES = 200_000


def snapshot_key_prefix(company_id, pack_id) -> str:
    """Where Task 9 stores a pack's web snapshots on the lake bucket (spec §3.2):
    action_packs/<company_id>/<pack_id>/web/<ref>.txt. The viewer reads only keys under
    this prefix, whatever the source row says."""
    return f"action_packs/{company_id}/{pack_id}/web/"


def snapshot_key(company_id, pack_id, ref) -> str:
    """The ONE key a web source's snapshot may have (Task 9 writes it, the store task
    records only it, the viewer reads only it). ValueError for a ref that is not a ref."""
    if not isinstance(ref, str) or not REF_RE.match(ref):
        raise ValueError("bad ref")
    return f"{snapshot_key_prefix(company_id, pack_id)}{ref}.txt"


def snapshot_key_error(payload, company_id, pack_id):
    """None, or 'snapshot_key' when any source's snapshot_s3_key is not EXACTLY
    snapshot_key(company_id, pack_id, <its own ref>) or sits on a non-web source. Run by
    the store task before anything is written: a key elsewhere would be recorded in the
    lake ownership index under this pack's company."""
    for s in (payload or {}).get("sources") or []:
        key = s.get("snapshot_s3_key") if isinstance(s, dict) else None
        if key is None:
            continue
        try:
            want = snapshot_key(company_id, pack_id, s.get("ref"))
        except ValueError:
            return "snapshot_key"
        if s.get("provenance") != "web" or key != want:
            return "snapshot_key"
    return None


def snapshot_keys(payload) -> list:
    """Every snapshot_s3_key in a store payload (after snapshot_key_error passed)."""
    return [s["snapshot_s3_key"] for s in (payload or {}).get("sources") or []
            if isinstance(s, dict) and s.get("snapshot_s3_key")]

# off (also unset / anything unknown) -> every pack route 404s like an unknown route and the
# internal tasks answer {"error": "disabled"}; internal -> platform_admin only; on -> everyone.
MODES = ("off", "internal", "on")

INSTRUCTION_MAX_CHARS = 2000
CREATE_KEYS = ("instruction", "site_id")
ARCHIVE_KEYS = ("archived",)
# The worker's plan-stage strings (heartbeat task). Generous caps: they bound one row.
READING_MAX_CHARS = 4000
TITLE_MAX_CHARS = 200

# The store task arrives as a synchronous (RequestResponse) Lambda invoke, whose request
# payload is capped at 6 MB (6,291,456 bytes) by Lambda itself -- an oversized invoke never
# reaches org-api at all. 5.5 MB leaves ~0.5 MB for the event envelope ({"task", "pack_id"})
# and for encoding variance between the worker's serializer and this measure, so a payload
# that passes here is never one Lambda would have refused, and a too-large pack gets a
# deterministic error code rather than a transport exception. Measured as the UTF-8 length
# of json.dumps with ensure_ascii=True (the default, and the largest encoding of non-ASCII
# text), so the worker can run the very same check before it invokes.
STORE_PAYLOAD_MAX_BYTES = 5_500_000

# Action types the UI may offer Apply for (S15); draft types have Copy/Export only (§5.1).
APPLY_TYPES = ("close_item", "update_item", "create_item")
APPLICABLE_STATES = ("proposed", "failed")
STALE_SWEEP_MINUTES = 20                     # S9

# What a non-removed source returns. Excerpt/title/url only while the source is live (S13).
_SOURCE_OUT = ("ref", "kind", "provenance", "topic_id", "chunk_id", "document_id",
               "revision", "page_from", "page_to", "stable_id", "title", "date", "site_id",
               "who", "excerpt", "url", "fetched_at", "snapshot_s3_key")


def mode() -> str:
    """The ACTION_PACKS switch, read at call time so an env edit is a rollback."""
    value = (os.environ.get("ACTION_PACKS") or "").strip().lower()
    return value if value in MODES else "off"


def enabled() -> bool:
    """Internal tasks run only while the switch is internal or on."""
    return mode() in ("internal", "on")


def route_allowed(caller) -> bool:
    """Whether this caller may reach the pack routes at all (else 404, as if unknown)."""
    m = mode()
    if m == "on":
        return True
    if m == "internal":
        return (caller or {}).get("global_role") == "platform_admin"
    return False


def validate_create_body(body):
    """(instruction, site_id_or_None, None) or (None, None, (message, status, extra)).
    Whitelisted: unknown keys are refused rather than ignored (a typo must not silently
    become a default)."""
    if body is None:
        return None, None, ("malformed JSON body", 400, None)
    extra = sorted(set(body) - set(CREATE_KEYS))
    if extra:
        return None, None, ("unknown fields", 400, {"fields": extra})
    instruction = body.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        return None, None, ("instruction is required", 400, None)
    instruction = instruction.strip()
    if len(instruction) > INSTRUCTION_MAX_CHARS:
        return None, None, (f"instruction must be at most {INSTRUCTION_MAX_CHARS} characters",
                            400, None)
    site_id = body.get("site_id")
    if site_id is not None and not isinstance(site_id, str):
        return None, None, ("site_id must be a string", 400, None)
    return instruction, (site_id or None), None


def validate_archive_body(body):
    """(archived_bool, None) or (None, (message, status, extra))."""
    if body is None:
        return None, ("malformed JSON body", 400, None)
    extra = sorted(set(body) - set(ARCHIVE_KEYS))
    if extra:
        return None, ("unknown fields", 400, {"fields": extra})
    if not isinstance(body.get("archived"), bool):
        return None, ("archived must be true or false", 400, None)
    return body["archived"], None


def payload_size(payload) -> int:
    """Bytes of the payload as the invoke carries it (see STORE_PAYLOAD_MAX_BYTES)."""
    return len(json.dumps(payload, default=str).encode("utf-8"))


def store_payload_error(payload):
    """None when the payload may be stored, else an error dict for the store task."""
    if not isinstance(payload, dict):
        return {"error": "invalid", "detail": "payload_not_object"}
    if payload_size(payload) > STORE_PAYLOAD_MAX_BYTES:
        return {"error": "payload_too_large"}
    return None


def _is_uuid(v):
    try:
        uuid.UUID(v)
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _is_iso(v, kind):
    try:
        (dt.date.fromisoformat if kind == "date" else dt.datetime.fromisoformat)(v)
        return True
    except (ValueError, TypeError):
        return False


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool) and -2**31 <= v < 2**31


def _str_list(v):
    return isinstance(v, list) and all(isinstance(x, str) and x for x in v)


# Per-field type rules for the store payload; a value may always be None (= omitted).
_PACK_TYPES = {"title": "str", "reading": "str", "conclusion": "list", "run_trace": "dict",
               "models": "dict", "as_of": "datetime", "sources": "list", "actions": "list"}
_SOURCE_TYPES = {"ref": "str", "kind": "str", "provenance": "str", "topic_id": "uuid",
                 "chunk_id": "uuid", "document_id": "uuid", "revision": "int",
                 "page_from": "int", "page_to": "int", "stable_id": "uuid", "title": "str",
                 "date": "date", "site_id": "uuid", "who": "str", "excerpt": "str",
                 "url": "str", "fetched_at": "datetime", "snapshot_s3_key": "str"}
_ACTION_TYPES = {"seq": "int", "type": "str", "target_stable_id": "uuid",
                 "target_snapshot": "dict", "proposed": "dict", "rationale": "str",
                 "refs": "strlist", "verifier": "str"}
_CHECK = {"str": lambda v: isinstance(v, str), "list": lambda v: isinstance(v, list),
          "dict": lambda v: isinstance(v, dict), "int": _is_int, "uuid": _is_uuid,
          "date": lambda v: _is_iso(v, "date"), "datetime": lambda v: _is_iso(v, "datetime"),
          "strlist": _str_list}


def _types_ok(obj, rules):
    return all(obj.get(k) is None or _CHECK[t](obj[k]) for k, t in rules.items() if k in obj)


def payload_shape_error(payload):
    """None, or a detail code for a store payload whose SHAPE is wrong (a non-list sources,
    a non-dict action, a string seq, a malformed uuid/date, ...). Run before the repository
    so a malformed payload is answered {"error": "invalid", "detail": code} and never reaches
    the database as a driver error, nor raises a TypeError (a Lambda FunctionError for the
    worker). Unknown keys and dangling refs stay the repository's checks."""
    if not isinstance(payload, dict):
        return "payload_not_object"
    if not _types_ok(payload, _PACK_TYPES):
        return "pack_field_type"
    for claim in payload.get("conclusion") or []:
        if not isinstance(claim, dict) or not isinstance(claim.get("text"), str) or (
                claim.get("refs") is not None and not _str_list(claim["refs"])) or (
                claim.get("confidence") not in (None, "supported", "uncertain")):
            return "conclusion_shape"
    for s in payload.get("sources") or []:
        if not isinstance(s, dict):
            return "source_shape"
        if not all(isinstance(s.get(k), str) and s.get(k) for k in ("ref", "kind", "provenance")):
            return "source_required"
        if not _types_ok(s, _SOURCE_TYPES):
            return "source_field_type"
    for a in payload.get("actions") or []:
        if not isinstance(a, dict):
            return "action_shape"
        if not (_is_int(a.get("seq")) and isinstance(a.get("type"), str)
                and _str_list(a.get("refs")) and a.get("refs")
                and isinstance(a.get("verifier"), str)):
            return "action_required"
        if not _types_ok(a, _ACTION_TYPES):
            return "action_field_type"
    return None


def invalid_detail(message: str) -> str:
    """A code for the repository's ValueError (its message names keys/refs; codes only leave)."""
    if message.startswith("unknown"):
        return "unknown_keys"
    if "cites refs" in message:
        return "dangling_ref"
    return "invalid_payload"


def request_object(pack, caller_sub) -> dict:
    """The S3 kickoff object (S1): the pack id, the creator's sub and the site id. NEVER the
    instruction -- the worker reads it from the database through the claim task."""
    return {"pack_id": str(pack["id"]), "caller_sub": caller_sub,
            "site_id": str(pack["site_id"])}


# ---------------------------------------------------------------------- GET one view

def _source_out(src, removed: bool) -> dict:
    if removed:
        return {"ref": src["ref"], "kind": src["kind"], "provenance": src["provenance"],
                "removed": True}
    out = {k: src.get(k) for k in _SOURCE_OUT}
    out["removed"] = False
    return out


def cites_only_removed(refs, removed_refs) -> bool:
    """S13: a claim/action is hidden when it cites at least one ref and every one is removed.
    A claim with no refs cites nothing removed and stays."""
    refs = list(refs or [])
    return bool(refs) and all(r in removed_refs for r in refs)


def visible_actions(actions, removed_refs):
    return [a for a in actions or [] if not cites_only_removed(a.get("refs"), removed_refs)]


def applicable_candidate(action, pack) -> bool:
    """The parts of S15 that need no database: an apply type, never `unsupported`, an
    action still open to apply, on a finished, unarchived pack. The ACL half is org-api's."""
    return (action.get("type") in APPLY_TYPES
            and action.get("verifier") != "unsupported"
            and action.get("state") in APPLICABLE_STATES
            and pack.get("state") == "ready"
            and pack.get("archived_at") is None)


# ---------------------------------------------------------------------- apply (Task 7)
# POST /action-packs/{id}/actions/{aid}/apply -- spec §5, §14 S10/S11. The database half
# (locks, live row, ACL, writes) is org-api's apply_pack_action; these decide and shape.

APPLY_BODY_KEYS = ("fields",)
# The fields an applier may edit before applying, per type (S10: `text` is never patchable
# on an existing item; a create carries its own text).
APPLY_EDIT_FIELDS = {
    "close_item": ("status",),
    "update_item": ("deadline", "responsible", "priority", "status"),
    "create_item": ("text", "responsible", "deadline", "priority"),
}
CLOSE_STATUSES = ("done", "closed")
CREATE_TEXT_MAX_CHARS = 2000
# S10, from -> allowed targets. Same -> same is not a transition (it is the noop case).
STATUS_TRANSITIONS = {
    "open": ("in_progress", "blocked", "done", "closed"),
    "in_progress": ("blocked", "done", "closed", "open"),
    "blocked": ("in_progress", "done", "closed", "open"),
    "done": ("open",),
    "closed": ("open",),
}
# The live item's fields an apply response shows next to the run's snapshot (§5.2).
ITEM_VIEW_FIELDS = ("status", "deadline", "responsible", "priority", "text", "updated_at")


def validate_apply_body(body, action_type):
    """(edits_dict, None) or (None, (message, status, extra)). The body is empty or
    {"fields": {...}}; every edited field must be in APPLY_EDIT_FIELDS[action_type]
    (refused, never ignored). A close's edited status must still close. Values are
    otherwise validated by the PATCH rules at write time."""
    if body is None:
        return None, ("malformed JSON body", 400, None)
    extra = sorted(set(body) - set(APPLY_BODY_KEYS))
    if extra:
        return None, ("unknown fields", 400, {"fields": extra})
    edits = body.get("fields", {})
    if not isinstance(edits, dict):
        return None, ("fields must be an object", 400, None)
    bad = sorted(set(edits) - set(APPLY_EDIT_FIELDS.get(action_type, ())))
    if bad:
        return None, ("field not editable", 400, {"fields": bad})
    if action_type == "close_item" and "status" in edits \
            and edits["status"] not in CLOSE_STATUSES:
        return None, (f"status must be one of {list(CLOSE_STATUSES)}", 400, None)
    return dict(edits), None


def effective_fields(action, edits):
    """The fields this apply writes: the action's proposal with the applier's edits on top.
    (fields, None) or (None, (message, status, extra)).

    A proposal naming a field apply may not write is refused whole (409
    unsupported_field) rather than silently trimmed: the UI showed it. create_item's
    `site_id` is the pack's own site (checked by the caller) and is dropped here."""
    atype = action.get("type")
    allowed = APPLY_EDIT_FIELDS.get(atype, ())
    proposed = dict(action.get("proposed") or {})
    if atype == "create_item":
        proposed.pop("site_id", None)
    bad = sorted(set(proposed) - set(allowed))
    if bad:
        return None, ("the action proposes a field apply cannot write", 409,
                      {"code": "unsupported_field", "fields": bad})
    fields = {**proposed, **(edits or {})}
    if atype == "close_item":
        fields.setdefault("status", "done")
        if fields["status"] not in CLOSE_STATUSES:
            return None, ("a close must set done or closed", 409,
                          {"code": "unsupported_field", "fields": ["status"]})
    if atype == "create_item":
        text = fields.get("text")
        if not isinstance(text, str) or not text.strip():
            return None, ("text is required", 400, None)
        if len(text.strip()) > CREATE_TEXT_MAX_CHARS:
            return None, (f"text must be at most {CREATE_TEXT_MAX_CHARS} characters", 400,
                          None)
        fields["text"] = text.strip()
    if not fields:
        return None, ("the action has nothing to apply", 409, {"code": "empty_proposal"})
    return fields, None


def transition_allowed(current, target) -> bool:
    """S10's table. Same -> same is not a transition."""
    return target in STATUS_TRANSITIONS.get(current, ())


def _norm(value):
    if value is None:
        return None
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return str(value)


def same_value(a, b) -> bool:
    """Field equality across a date and its ISO string (the row vs the JSON snapshot)."""
    return _norm(a) == _norm(b)


def differs_from_snapshot(fields, snapshot, live) -> bool:
    """S10/§5.2: has any field this apply touches moved since the run read it? A snapshot
    without that field (or none at all) cannot prove it has not -- fail closed (True)."""
    snap = snapshot if isinstance(snapshot, dict) else {}
    return any(f not in snap or not same_value(snap[f], live.get(f)) for f in fields)


def item_view(row):
    """The live item's comparable fields for a stale response (None when there is none)."""
    if row is None:
        return None
    return {f: _norm(row.get(f)) if f in ("deadline", "updated_at") else row.get(f)
            for f in ITEM_VIEW_FIELDS}


def shape_pack(pack, *, removed_refs, site_name, targets, applicable, viewer_id) -> dict:
    """The GET /action-packs/{id} body.

    removed_refs -- refs whose source is deleted / redacted / not visible to this reader
    targets      -- {action id: {"task_no": int|None, "visible": bool}} for close/update
    applicable   -- {action id: bool}
    Actions and claims that cite only removed sources are left out entirely."""
    removed_refs = set(removed_refs or ())
    conclusion = [c for c in (pack.get("conclusion") or [])
                  if isinstance(c, dict) and not cites_only_removed(c.get("refs"), removed_refs)]
    actions = []
    for a in visible_actions(pack.get("actions"), removed_refs):
        aid = str(a["id"])
        target = targets.get(aid) or {}
        out = {
            "id": aid, "seq": a.get("seq"), "type": a.get("type"),
            "verifier": a.get("verifier"), "state": a.get("state"),
            "refs": list(a.get("refs") or []), "rationale": a.get("rationale"),
            "proposed": a.get("proposed"),
            "target_stable_id": (str(a["target_stable_id"])
                                 if a.get("target_stable_id") is not None else None),
            # Like the snapshot below: the number names a live item, so only while the
            # reader can see it.
            "target_task_no": target.get("task_no") if target.get("visible") else None,
            "applicable": bool(applicable.get(aid)),
            "applied_at": a.get("applied_at"), "applied_result": a.get("applied_result"),
            "created_item_stable_id": (str(a["created_item_stable_id"])
                                       if a.get("created_item_stable_id") is not None
                                       else None),
        }
        # The run's copy of the target (incl. its text) only while the reader can still
        # see the live item -- the same S13 rule as a source.
        out["target_snapshot"] = (a.get("target_snapshot") if target.get("visible")
                                  else None)
        actions.append(out)
    return {
        "id": str(pack["id"]), "site_id": str(pack["site_id"]), "site_name": site_name,
        "title": pack.get("title"), "instruction": pack.get("instruction"),
        "reading": pack.get("reading"), "state": pack.get("state"),
        "failure": pack.get("failure"), "conclusion": conclusion,
        "as_of": pack.get("as_of"), "created_at": pack.get("created_at"),
        "updated_at": pack.get("updated_at"),
        "archived": pack.get("archived_at") is not None,
        "archived_at": pack.get("archived_at"),
        "is_creator": str(pack.get("created_by")) == str(viewer_id),
        "actions": actions,
        "sources": [_source_out(s, s["ref"] in removed_refs) for s in pack.get("sources") or []],
    }


def shape_list_row(row, site_name) -> dict:
    return {"id": str(row["id"]), "title": row.get("title"),
            "instruction": row.get("instruction"), "state": row.get("state"),
            "failure": row.get("failure"), "site_id": str(row["site_id"]),
            "site_name": site_name, "created_at": row.get("created_at"),
            "archived": row.get("archived_at") is not None,
            "action_counts": row.get("action_counts")}


# ---------------------------------------------------------------------- export

_ACTION_LABEL = {"close_item": "Close", "update_item": "Update", "create_item": "Create",
                 "draft_rfi": "Draft RFI", "draft_email": "Draft email", "note": "Note"}


def _refs(refs):
    return " ".join(f"[{r}]" for r in refs or [])


def _md_line(text):
    return " ".join(str(text or "").split())


def render_markdown(view) -> str:
    """The pack as Markdown, from the SAME shaped view GET returns (S13 already applied:
    removed sources carry no excerpt/title here either)."""
    lines = [f"# {_md_line(view.get('title') or 'Action pack')}", ""]
    meta = [f"State: {view.get('state')}"]
    if view.get("site_name"):
        meta.append(f"Site: {_md_line(view['site_name'])}")
    if view.get("as_of"):
        meta.append(f"Evidence as of: {view['as_of']}")
    lines += [" · ".join(meta), ""]
    if view.get("reading"):
        lines += ["## Reading", "", _md_line(view["reading"]), ""]
    if view.get("conclusion"):
        lines += ["## Conclusion", ""]
        for c in view["conclusion"]:
            tag = " _(uncertain)_" if c.get("confidence") == "uncertain" else ""
            lines.append(f"- {_md_line(c.get('text'))} {_refs(c.get('refs'))}{tag}".rstrip())
        lines.append("")
    if view.get("actions"):
        lines += ["## Actions", ""]
        for a in view["actions"]:
            label = _ACTION_LABEL.get(a.get("type"), a.get("type"))
            target = f" #{a['target_task_no']}" if a.get("target_task_no") else ""
            weak = " _(uncertain)_" if a.get("verifier") == "weak" else ""
            lines.append(f"- **{label}{target}** ({a.get('state')}){weak}: "
                         f"{_md_line(a.get('rationale'))} {_refs(a.get('refs'))}".rstrip())
            proposed = a.get("proposed")
            if isinstance(proposed, dict):
                for k in sorted(proposed):
                    lines.append(f"  - {k}: {_md_line(proposed[k])}")
        lines.append("")
    if view.get("sources"):
        lines += ["## Sources", ""]
        for s in view["sources"]:
            if s.get("removed"):
                lines.append(f"- [{s['ref']}] source removed")
                continue
            bits = [b for b in (_md_line(s.get("title")), s.get("date"), _md_line(s.get("who")))
                    if b]
            if s.get("url"):
                bits.append(s["url"])
            lines.append(f"- [{s['ref']}] " + " · ".join(str(b) for b in bits))
            if s.get("excerpt"):
                lines.append(f"  > {_md_line(s['excerpt'])}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
