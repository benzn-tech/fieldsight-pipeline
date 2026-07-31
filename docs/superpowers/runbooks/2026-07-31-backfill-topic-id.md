# Backfill report_chunks.topic_id (authority-flip, 2026-07-17 → today)

## 1. Enumerate the (report_date, folder) pairs still NULL (prod DB, Data API)
CL=arn:aws:rds:ap-southeast-2:509194952652:cluster:fieldsight-db-test-dbcluster-hywiixu8ihi9
SEC=arn:aws:secretsmanager:ap-southeast-2:509194952652:secret:rds!cluster-1757a281-ee31-460d-b56e-950817921010-Ansbey
aws rds-data execute-statement --resource-arn "$CL" --secret-arn "$SEC" --database fieldsight \
  --sql "SELECT DISTINCT report_date, split_part(source_s3_key,'/',3) folder FROM report_chunks WHERE topic_id IS NULL AND report_date >= '2026-07-17' ORDER BY 1" --region ap-southeast-2

## 2. For each (date, folder): re-trigger ingest by re-touching the embeddings sidecar
# ingest fires on embeddings/{date}/{folder}/vectors.json ObjectCreated → re-runs ingest_report (delete+reinsert chunks with the Task 7 topic-linking fix), reusing the existing embeddings — no DashScope re-embed.
aws s3 cp s3://fieldsight-data-509194952652/embeddings/<date>/<folder>/vectors.json \
  s3://fieldsight-data-509194952652/embeddings/<date>/<folder>/vectors.json \
  --metadata-directive REPLACE --region ap-southeast-2

## 3. Verify topic_id recovered
aws rds-data execute-statement --resource-arn "$CL" --secret-arn "$SEC" --database fieldsight \
  --sql "SELECT count(*) total, count(topic_id) with_topic FROM report_chunks WHERE created_at::date >= '2026-07-17'" --region ap-southeast-2
# with_topic should now be > 0 (unmatched report topics legitimately stay NULL; Task 1 keeps them searchable).
