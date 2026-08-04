"""Minimal Notion access for the device ledger.

Only two operations are needed — read every row, patch some properties on one
row — so this is a hand-rolled client over urllib3 rather than a dependency.

Notion's property envelopes are nested and forgiving in the worst way: a value
in the wrong shape is often accepted and ignored, leaving a table that looks
merely stale rather than one that errors. Hence `build_props` raises on an
unknown key instead of dropping it, and every shape is unit-tested.

The hand-edited columns — Dispatched, Returned, Notes — deliberately have NO
writable mapping. There is no way to address them, so no future edit can
overwrite a human's entry by accident.

API version 2025-09-03 (data_sources endpoints). If Notion rejects it, check
their changelog and update this constant and the docstring together.
"""

import datetime as dt
import json
import logging

import urllib3

logger = logging.getLogger()

NOTION_VERSION = "2025-09-03"
_API = "https://api.notion.com/v1"
_http = urllib3.PoolManager()

# our name -> (Notion property name, kind). Writable columns only.
_PROPS = {
    "client": ("Client", "rich_text"),
    "due_back": ("Due back", "date"),
    "activated": ("Activated", "date"),
    "status": ("Status", "select"),
    "last_seen": ("Last seen", "date"),
    "app_version": ("App version", "rich_text"),
    "actual_site": ("Actual site", "rich_text"),
    "last_synced": ("Last synced", "date"),
}


def _headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _text_of(prop):
    if not prop:
        return None
    parts = prop.get("rich_text") or prop.get("title") or []
    joined = "".join(p.get("plain_text", "") for p in parts)
    return joined or None


def _date_of(prop):
    if not prop:
        return None
    d = prop.get("date")
    if not d or not d.get("start"):
        return None
    # Notion returns either 2026-08-01 or a full ISO datetime; a date is what we mean.
    return dt.date.fromisoformat(d["start"][:10])


def parse_row(raw):
    props = raw.get("properties", {})
    return {
        "page_id": raw.get("id"),
        "device": _text_of(props.get("Device")) or "",
        "dispatched": _date_of(props.get("Dispatched")),
        "due_back": _date_of(props.get("Due back")),
        "returned": bool((props.get("Returned") or {}).get("checkbox", False)),
        "client": _text_of(props.get("Client")),
        "activated": _date_of(props.get("Activated")),
        "notes": _text_of(props.get("Notes")),
    }


def build_props(values):
    """Translate plain values into Notion envelopes. Raises on an unknown key."""
    out = {}
    for key, value in values.items():
        if key not in _PROPS:
            raise KeyError(f"unknown device-ledger property {key!r}")
        name, kind = _PROPS[key]
        if kind == "date":
            out[name] = {"date": {"start": value.isoformat()} if value else None}
        elif kind == "select":
            out[name] = {"select": {"name": value} if value else None}
        else:
            out[name] = {"rich_text": [{"text": {"content": str(value)}}] if value else []}
    return out


def list_rows(token, data_source_id):
    rows, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        resp = _http.request(
            "POST", f"{_API}/data_sources/{data_source_id}/query",
            headers=_headers(token), body=json.dumps(body).encode(),
        )
        if resp.status != 200:
            raise RuntimeError(f"notion query failed {resp.status}: {resp.data[:300]!r}")
        payload = json.loads(resp.data)
        rows.extend(parse_row(r) for r in payload.get("results", []))
        if not payload.get("has_more"):
            return rows
        cursor = payload.get("next_cursor")


def update_row(token, page_id, props):
    body = json.dumps({"properties": build_props(props)}).encode()
    resp = _http.request("PATCH", f"{_API}/pages/{page_id}", headers=_headers(token), body=body)
    if resp.status != 200:
        raise RuntimeError(f"notion update failed {resp.status}: {resp.data[:300]!r}")
