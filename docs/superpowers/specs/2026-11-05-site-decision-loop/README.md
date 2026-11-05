# Site decision loop: diagram and Chinese companion

These are the visual companions to `../2026-11-05-site-decision-loop-design.md` (design rev 2).

| File | What it is |
|---|---|
| `diagram.html` | The owner-reviewed diagram page (rev 7). Open it in a browser. Three views: `#overview`, `#product` (the full product flow), `#dev` (the dev pipeline flow). Text is Chinese. |
| `diagram.pdf` | The same page printed: A4, 4 pages (overview, product flow, dev flow). |
| `diagram-print.html` | Source of `diagram.pdf`. It is `diagram.html` plus print CSS that lays all three views out in order. |
| `flowgen.py` | Generates the two SVG flow diagrams. Edit the node coordinates and labels here, regenerate, and paste the SVGs back into `diagram.html`. |
| `design-zh.html` / `design-zh.pdf` | Chinese version of the design document, for the owner. Decisions are marked owner-decided, my call, or open. |

Live copy (private to the owner): https://claude.ai/artifact/TKCKhHFSduQuU3HWdz4Hif

**How to read the flow diagrams:**
- Node border colour shows status:
  - solid green = exists;
  - amber dashed = partial (TEST or shadow mode only);
  - red dashed = missing.
  These statuses are rough and were not checked node by node against the code.
- Line colours:
  - dark = main flow;
  - blue = retry or send back;
  - brown = path through a person;
  - red = block or rollback;
  - dashed grey = writes a record.

**Regenerating the PDFs** (Windows, headless Edge):
`msedge --headless=new --no-pdf-header-footer --print-to-pdf=diagram.pdf diagram-print.html`

Keep the diagram in step with the design doc: if one changes, change the other in the same PR.
