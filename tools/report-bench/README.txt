Report / extraction prompt x model bench (offline tooling; bench-only, nothing in src/ changes).
Spec: docs/superpowers/specs/2026-10-28-report-extraction-prompt-model-bench-design.md (rev 2.1).
Plan: docs/superpowers/plans/2026-10-28-report-extraction-bench-p0.md.
The owner runs every live / keyed step (AWS read, OpenRouter, Anthropic); P0 tests use fakes only.
Modules are named rb_* so they never collide with tools/web-answer-bench modules in one interpreter.
