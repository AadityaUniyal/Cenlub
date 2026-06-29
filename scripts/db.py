"""
scripts/db.py
=============
Database connection helper — loaded from DATABASE_URL env var.
Never hardcodes credentials.

Usage:
    from scripts.db import get_conn, execute_schema

All callers get a psycopg2 connection with autocommit=False.
Use as context manager:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(...)
        conn.commit()
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Generator

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------

def _dsn() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise EnvironmentError(
            "DATABASE_URL environment variable is not set. "
            "Add it to your .env file or export it before running."
        )
    return url


@contextmanager
def get_conn() -> Generator:
    """Yield a psycopg2 connection; commit on clean exit, rollback on error."""
    import psycopg2  # type: ignore
    conn = psycopg2.connect(_dsn())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def execute_schema(schema_path: str = "scripts/schema.sql") -> None:
    """
    Run the DDL script once to create all tables.
    Safe to call multiple times — uses CREATE TABLE IF NOT EXISTS.
    """
    if not os.path.exists(schema_path):
        raise FileNotFoundError(f"Schema file not found: {schema_path}")

    with open(schema_path, encoding="utf-8") as fh:
        sql = fh.read()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
    log.info("Schema applied from %s", schema_path)
