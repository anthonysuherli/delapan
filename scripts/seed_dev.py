"""Ensure the MCP user exists in GoTrue and belongs to the target org.

    .venv/bin/python scripts/seed_dev.py [--org <uuid>]

Idempotent. Uses the service-role client (admin). Reads the MCP user creds from
settings (DLP_MCP_USER_EMAIL / DLP_MCP_USER_PASSWORD). The org comes from --org
or DELAPAN_CLOUD_ORG_ID — never a hardcoded tenant id. Prints the resolved
user_id.
"""

from __future__ import annotations

import argparse
import os
import sys

from delapan.core.clients.supabase import service_client
from delapan.core.config import get_settings


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", default=os.environ.get("DELAPAN_CLOUD_ORG_ID", ""))
    args = ap.parse_args()
    if not args.org:
        sys.exit("pass --org <uuid> or set DELAPAN_CLOUD_ORG_ID")
    s = get_settings()
    sb = service_client()

    # 1. find-or-create the auth user (admin API)
    email, password = s.mcp_user_email, s.mcp_user_password
    listed = sb.auth.admin.list_users()
    users = listed if isinstance(listed, list) else getattr(listed, "users", [])
    user = next((u for u in users if getattr(u, "email", None) == email), None)
    if user is None:
        res = sb.auth.admin.create_user(
            {"email": email, "password": password, "email_confirm": True})
        user = res.user
        print(f"created auth user {email}")
    else:
        print(f"auth user {email} already exists")
    user_id = user.id

    # 2. ensure org membership
    member = (sb.table("org_members").select("user_id")
              .eq("org_id", args.org).eq("user_id", user_id).limit(1).execute().data)
    if not member:
        sb.table("org_members").insert(
            {"org_id": args.org, "user_id": user_id, "role": "member"}).execute()
        print(f"added {email} to org {args.org}")
    else:
        print(f"{email} already a member of {args.org}")
    print(f"user_id={user_id}")


if __name__ == "__main__":
    sys.exit(main())
