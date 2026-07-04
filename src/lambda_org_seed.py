"""
Lambda: fieldsight-org-seed v1.0 — one-shot idempotent org backfill (Phase 3)

Manual invoke only. Creates the company row, mirrors the Cognito user pool
(real login users) into Aurora users, creates sites from S3
config/user_mapping.json, and enrolls mapped users as memberships.
Re-running creates nothing new — but it RE-APPLIES mapping/admin-derived roles, overwriting any role changed later via the org API.

Event: {"company_name"?: str, "admin_emails"?: [str]}
Needs: cognito-idp interface endpoint + S3 gateway endpoint (in-VPC, no NAT).
"""
import json
import logging
import os

import boto3

from db.connection import get_connection
from repositories import companies, memberships, sites, users

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
CONFIG_KEY = os.environ.get("CONFIG_KEY", "config/user_mapping.json")
COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
DEFAULT_COMPANY = "FieldSight"
DEFAULT_ADMIN_EMAILS = ["benl.tech@outlook.com"]


def load_mapping() -> dict:
    obj = boto3.client("s3").get_object(Bucket=S3_BUCKET, Key=CONFIG_KEY)
    return json.loads(obj["Body"].read().decode("utf-8"))


def list_cognito_users() -> list:
    client = boto3.client("cognito-idp")
    out, token = [], None
    while True:
        kwargs = {"UserPoolId": COGNITO_USER_POOL_ID}
        if token:
            kwargs["PaginationToken"] = token
        resp = client.list_users(**kwargs)
        out.extend(resp.get("Users", []))
        token = resp.get("PaginationToken")
        if not token:
            return out


def attrs_of(user) -> dict:
    return {a["Name"]: a["Value"] for a in user.get("Attributes", [])}


def mapping_by_name(mapping: dict) -> dict:
    """device→info mapping re-keyed by lowercased person name."""
    return {info["name"].lower(): info
            for info in mapping.get("mapping", {}).values() if info.get("name")}


def resolve_role(email, name, admin_emails, by_name) -> str:
    if email.lower() in admin_emails:
        return "admin"
    info = by_name.get((name or "").lower())
    return info.get("role", "worker") if info else "worker"


def split_name(name):
    parts = (name or "").strip().split(None, 1)
    if not parts:
        return (None, None)
    return (parts[0], parts[1] if len(parts) > 1 else None)


def lambda_handler(event, context):
    event = event or {}
    company_name = event.get("company_name", DEFAULT_COMPANY)
    admin_emails = {e.lower() for e in event.get("admin_emails", DEFAULT_ADMIN_EMAILS)}

    mapping = load_mapping()
    by_name = mapping_by_name(mapping)
    cognito_users = list_cognito_users()

    n_users = n_sites = n_memberships = 0
    with get_connection() as conn:
        company = (companies.get_company_by_name(conn, company_name)
                   or companies.create_company(conn, company_name))

        slug_to_site = {}
        for slug, s in mapping.get("sites", {}).items():
            site = sites.get_company_site_by_name(conn, company["id"], s["name"])
            if site is None:
                site = sites.create_site(conn, company["id"], s["name"],
                                         location=s.get("location"),
                                         client=s.get("client"))
                n_sites += 1
            slug_to_site[slug] = site

        for cu in cognito_users:
            a = attrs_of(cu)
            sub, email, name = a.get("sub"), a.get("email", ""), a.get("name", "")
            if not sub or not email:
                continue
            first, last = split_name(name)
            role = resolve_role(email, name, admin_emails, by_name)
            user = users.upsert_user(conn, sub, email, company_id=company["id"],
                                     first_name=first, last_name=last,
                                     global_role=role)
            n_users += 1
            info = by_name.get(name.lower())
            if info:
                for slug in info.get("sites", []):
                    site = slug_to_site.get(slug)
                    if site:
                        memberships.ensure_membership(
                            conn, user["id"], site["id"],
                            info.get("role", "worker"))
                        n_memberships += 1

    logger.info("seed done: company=%s users=%d sites=%d memberships=%d",
                company_name, n_users, n_sites, n_memberships)
    # company["id"] is a uuid.UUID (psycopg dict_row) — Lambda marshals the
    # return value with plain json (no default=str, unlike the API's ok()),
    # so coerce to str or the invoke fails with Runtime.MarshalError.
    return {"company": {"id": str(company["id"]), "name": company["name"]},
            "users": n_users, "sites": n_sites, "memberships": n_memberships}
