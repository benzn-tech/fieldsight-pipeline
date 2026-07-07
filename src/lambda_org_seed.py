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
    n_sites_backfilled = n_login_folder_set = n_field_only_enrolled = 0
    cognito_names_seeded = set()
    with get_connection() as conn:
        company = (companies.get_company_by_name(conn, company_name)
                   or companies.create_company(conn, company_name))

        slug_to_site = {}
        for slug, s in mapping.get("sites", {}).items():
            site = sites.get_company_site_by_name(conn, company["id"], s["name"])
            if site is None:
                site = sites.create_site(conn, company["id"], s["name"],
                                         location=s.get("location"),
                                         client=s.get("client"),
                                         slug=slug)
                n_sites += 1
            else:
                sites.set_slug(conn, site["id"], slug)
            n_sites_backfilled += 1
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
            # folder_name backfill (F2, Fable review): prefer the MAPPING's
            # canonical name over the Cognito display name. S3 report
            # folders are always mapping-name-derived (report_generator:
            # info["name"].replace(' ','_')) -- a Cognito display name that
            # differs in case/spelling from the mapping name (e.g. Cognito
            # "jarley trainor" vs mapping "Jarley Trainor") would otherwise
            # store a folder_name that never matches the real S3 folder,
            # silently breaking get_by_folder_name-based historical
            # user_id backfill for that person. Falls back to the Cognito
            # name itself when there's no mapping match (e.g. admin-only
            # login with no device).
            info = by_name.get(name.lower())
            folder = (info["name"] if info else name).replace(" ", "_")
            if folder:  # F1: crash guard -- an empty Cognito name would
                # violate idx_users_company_folder (company, folder_name) on
                # the 2nd empty-name user and roll back the whole seed.
                users.set_folder_name(conn, sub, folder)
                n_login_folder_set += 1
            cognito_names_seeded.add(name.lower())
            if info:
                for slug in info.get("sites", []):
                    site = slug_to_site.get(slug)
                    if site:
                        memberships.ensure_membership(
                            conn, user["id"], site["id"],
                            info.get("role", "worker"))
                        n_memberships += 1

        # Second pass: enroll mapping people who never signed in via Cognito
        # (device-only field workers) as kind='field_only' directory rows.
        # KNOWN LIMITATION (F3, Fable review, Phase 1 scope): if a
        # field_only person later gets a Cognito login, the NEXT seed run's
        # users.set_folder_name(sub, folder) collides with the surviving
        # field_only row on the (company, folder_name) unique index --
        # the seed crashes until the two rows are manually merged.
        # Promotion/merge handling is a future task, not Phase 1 scope.
        for info in mapping.get("mapping", {}).values():
            name = info.get("name")
            if not name or name.lower() in cognito_names_seeded:
                continue
            first, last = split_name(name)
            field_user = users.upsert_field_only_user(
                conn, company["id"], folder_name=name.replace(" ", "_"),
                first_name=first, last_name=last,
                global_role=info.get("role", "worker"))
            n_field_only_enrolled += 1
            for slug in info.get("sites", []):
                site = slug_to_site.get(slug)
                if site:
                    memberships.ensure_membership(
                        conn, field_user["id"], site["id"],
                        info.get("role", "worker"))
                    n_memberships += 1

    logger.info("seed done: company=%s users=%d sites=%d memberships=%d "
                "sites_backfilled=%d login_folder_set=%d field_only_enrolled=%d",
                company_name, n_users, n_sites, n_memberships,
                n_sites_backfilled, n_login_folder_set, n_field_only_enrolled)
    # company["id"] is a uuid.UUID (psycopg dict_row) — Lambda marshals the
    # return value with plain json (no default=str, unlike the API's ok()),
    # so coerce to str or the invoke fails with Runtime.MarshalError.
    return {"company": {"id": str(company["id"]), "name": company["name"]},
            "users": n_users, "sites": n_sites, "memberships": n_memberships,
            "sites_backfilled": n_sites_backfilled,
            "login_folder_set": n_login_folder_set,
            "field_only_enrolled": n_field_only_enrolled}
