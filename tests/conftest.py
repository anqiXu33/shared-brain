import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

# server.py creates its clients at import time. Dummy values let the tests import it;
# nothing in the unit tests talks to Supabase or OpenAI.
os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test.test.test")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

SQL_FILES = [
    "schema.sql",
    "migrations/001_history.sql",
    "migrations/002_kinds_validity_hybrid.sql",
    "migrations/003_eval.sql",
    "migrations/004_deterministic_ties.sql",
    "migrations/005_projects.sql",
]


@pytest.fixture(scope="session")
def db():
    """A fresh database with schema.sql and every migration applied, in order.

    Needs DATABASE_URL pointing at a Postgres with pgvector and pg_trgm available
    and permission to create databases. Skipped when DATABASE_URL is not set.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        if os.environ.get("REQUIRE_DATABASE"):  # set in CI, so database tests never skip silently
            pytest.fail("REQUIRE_DATABASE is set but DATABASE_URL is not")
        pytest.skip("DATABASE_URL not set")
    import psycopg

    name = f"sb_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(f'create database "{name}"')
    test_url = psycopg.conninfo.make_conninfo(url, dbname=name)
    try:
        with psycopg.connect(test_url, autocommit=True) as conn:
            for f in SQL_FILES:
                conn.execute((ROOT / f).read_text())
            yield conn
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(f'drop database if exists "{name}" with (force)')
