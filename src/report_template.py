"""Templates are data, and this module is the only thing that turns them into a prompt.

A template names sections and says what each is for. It does NOT say how to write
them: the phrasing constraints are what made the old schema-driven report read like
a form (spec 2026-09-15 §6.1). House style -- how long, one sentence per item, what
never to repeat -- rides with the template because it is a house decision, not a
property of this code.
"""
import io
import json
import os
import re

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report_templates")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class TemplateNotFound(Exception):
    """No built-in template with that id and version. Never fall back to another
    one: a report names the template it was written to, and a silent substitution
    would make that name a lie."""


def load_template(template_id, version):
    if not _ID_RE.match(template_id or "") or not isinstance(version, int):
        raise TemplateNotFound("%s v%s" % (template_id, version))
    path = os.path.join(TEMPLATE_DIR, "%s.v%d.json" % (template_id, version))
    if not os.path.isfile(path):
        raise TemplateNotFound("%s v%s" % (template_id, version))
    with io.open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _action_lines(action_items):
    out = []
    for a in action_items or []:
        owner = (a.get("owner") or "").strip() or "no owner recorded"
        due = (a.get("deadline") or "").strip() or "no date"
        out.append("- %s | owner: %s | when: %s" % ((a.get("action") or "").strip(), owner, due))
    return out


def render_prompt(template, scope, action_items, transcript):
    """One prompt: what this recording is, the section plan, the house style, the
    action items as DATA, and the transcript."""
    sections = []
    for s in list(template.get("sections") or []) + [template["catch_all"]]:
        sections.append("### %s\n%s" % (s["title"], s["purpose"]))

    leave_out = ""
    if template.get("excluded_subjects"):
        covers = "; ".join(e["covers"] for e in template["excluded_subjects"])
        leave_out = (
            "\n## Leave out\n"
            "Do not report on: %s.\n"
            "If a stretch was mostly about that, leave it out entirely, including the "
            "final section. Do not summarise it under another heading.\n" % covers)

    style = ""
    if template.get("style"):
        style = "\n## House style\n" + "\n".join("- " + s for s in template["style"]) + "\n"

    lines = _action_lines(action_items)
    if lines:
        actions = (
            "\n## The actions already on record\n"
            "These were captured from this recording. Write them into the Actions "
            "section using the owner and date given here, one line each, as\n"
            "**Owner** - what they will do - *when*.\n"
            "Where the owner reads 'no owner recorded' or the date reads 'no date', "
            "write it that way. **Do not invent an owner or a date**, and do not add "
            "actions that are not in this list.\n\n" + "\n".join(lines) + "\n")
    else:
        actions = (
            "\n## The actions already on record\n"
            "None were captured from this recording. Write \"Nothing here.\" under the "
            "Actions heading. **Do not invent an owner or a date** and do not invent "
            "actions from the transcript.\n")

    return (
        "## The recording\n"
        "Site folder: {folder}\n"
        "Date:        {date}\n"
        "Window:      {frm} - {to}   ({n} recordings)\n"
        "\n## Sections\n"
        "Write one section for each heading below, in this order, using these headings\n"
        "exactly as written. The note under each heading says what that section is for.\n"
        "It is a description of purpose, not a format and not a list of fields.\n\n"
        "{sections}\n"
        "{leave_out}{style}{actions}"
        "\n## How to write it\n"
        "- Plain sentences. Write the way you would tell a colleague who has just got\n"
        "  back what happened. Short paragraphs; a list only where the thing is a list.\n"
        "- Use people's names where the recording makes clear who said or did something.\n"
        "  Labels like spk_0 are the transcription provider's own, assigned per API call\n"
        "  and not carried between calls. They are not identities. Never print them.\n"
        "- If a section has nothing behind it, write \"Nothing here.\" Do not pad it.\n"
        "- Say only what the recording supports. Where a figure or date was spoken as\n"
        "  provisional, say so alongside it.\n"
        "- Report the work. Do not quote swearing or personal remarks about people.\n"
        "\n## The recording\n{transcript}\n"
    ).format(folder=scope["folder"], date=scope["date"], frm=scope["from"], to=scope["to"],
             n=scope["recordings"], sections="\n\n".join(sections), leave_out=leave_out,
             style=style, actions=actions, transcript=transcript)
