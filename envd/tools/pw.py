"""Generated credentials.

All engine-generated passwords use URL-safe base64 charset ([A-Za-z0-9_-]):
safe as PostgreSQL/odoo literals by construction (no quotes, no backslashes),
safe in shell argv without quoting surprises, safe in .env files.
"""

from __future__ import annotations

import re
import secrets

SQL_SAFE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def generate_password(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


def is_sql_safe(password: str) -> bool:
    return bool(SQL_SAFE.match(password))
