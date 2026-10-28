# Plan: remove the RealPTT ingest path

Spec: `docs/superpowers/specs/2026-10-29-remove-realptt-ingest-design.md`

1. Template: remove the 14 resources, 10 parameters and 6 outputs listed in the spec.
   Re-home the "CloudWatch Log Groups" and "CloudWatch Alarms" section headers, and fix
   the stack description, the `EnableSchedules` description and the site-coords grant
   comment.
2. Workflows: in both deploy workflows, drop the RealPTT and Fargate env entries, the
   VPC/SUBNETS lines and the four override lines, all in the same commit as step 1.
3. Delete the three source modules and the three tests dedicated to them. Trim the shared
   tests (spec, "Tests").
4. Legacy `scripts/deploy-lambda-code.sh`: drop the two map entries.
5. Fix stale comments in `nz_time.py`, org-api and `samconfig.toml`.
6. Verify:
   - run the full `tests/unit` suite and compare it with the develop baseline (failures
     that already exist locally are listed in the PR);
   - run `sam validate --lint` (SAM CLI 1.169.0).
7. Open a PR to develop. Once it is merged, the TEST deploy is the first real
   CloudFormation delete; check that it completes before any release to main.
