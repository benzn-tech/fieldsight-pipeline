"""Measurement harness for the item-continuity spec's pre-registered evaluation (§7).

Builds session sets from TEST (and, read-only, PROD) and runs the baseline vs
with-block arms under the deployed extract-session model config. Never writes to S3
or any database; outputs land under a gitignored `continuity_eval_runs/<run_id>/`
directory, and only the committed counts are ever meant to enter the repo.
"""
