"""The PROJECT_DOCUMENTS switch (spec 2026-10-16-project-documents-design, Rollout).

Read per call so a test, or an emergency env edit, takes effect without a reload. Set on
OrgApi, RagSearch and AskAgent by the ProjectDocuments template parameter."""
import os


def on():
    return os.environ.get("PROJECT_DOCUMENTS", "false").lower() == "true"
