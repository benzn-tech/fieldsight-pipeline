"""Measurement harness for item_support (Track C spec §7/§9; pre-registration
docs/superpowers/specs/2026-11-08-item-support-eval-preregistration.md).

Reads TEST finals read-only. Everything that holds a folder name, item text or turn text lands
under the gitignored data/, runs/ or labels/ directories next to this file; only
results/summary.json (counts only) is meant to be committed.
"""
