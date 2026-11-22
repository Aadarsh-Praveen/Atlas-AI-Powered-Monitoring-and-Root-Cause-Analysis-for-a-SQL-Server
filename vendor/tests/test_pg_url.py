"""Tests for building Postgres URLs from separate settings: postgres_url in
tools/envconfig.py, and the vendor scripts that use it (seed.py, activity.py).

No database needed. Run from the repo root, in the root virtual environment:

    python -m pytest vendor/tests -v

Each test parses the URL with psycopg's conninfo_to_dict, which uses libpq,
the same parser psycopg.connect uses. So "the parsed password equals the
original" means the server would receive exactly that password.

The tests never read a real .env: the scripts' layered loader (load_layers)
is replaced with a no-op and the settings are set by the test itself.
"""

import sys
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict

VENDOR_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VENDOR_DIR))          # so the tests import vendor/*.py like the scripts do
sys.path.insert(0, str(VENDOR_DIR.parent / "tools"))

import activity  # noqa: E402
import seed  # noqa: E402
from envconfig import postgres_url  # noqa: E402

# Every character with a job in a URL, plus a space.
TRICKY_PASSWORD = "p@ss#word%25/:?&= x"


def test_password_with_at_and_hash_survives_the_round_trip():
    url = postgres_url("vendor", TRICKY_PASSWORD, "localhost", 5433, "vendordb")
    parsed = conninfo_to_dict(url)
    assert parsed["password"] == TRICKY_PASSWORD
    assert (parsed["user"], parsed["host"], parsed["port"], parsed["dbname"]) == \
        ("vendor", "localhost", "5433", "vendordb")


def test_user_and_database_are_encoded_too():
    parsed = conninfo_to_dict(postgres_url("odd@user", "x", "localhost", 5433, "db#1"))
    assert (parsed["user"], parsed["dbname"]) == ("odd@user", "db#1")


def test_options_are_added_as_a_query_string():
    parsed = conninfo_to_dict(postgres_url("u", "p@1", "example.neon.tech", 5432, "vendordb", sslmode="require"))
    assert (parsed["sslmode"], parsed["password"]) == ("require", "p@1")


def test_the_old_unencoded_url_was_broken():
    # What seed.py used to build: the "@" in the password ends the
    # user:password part early, so libpq never sees the real password.
    raw = "postgresql://vendor:abc@123@localhost:5433/vendordb"
    try:
        parsed = conninfo_to_dict(raw)
    except psycopg.ProgrammingError:
        return                                   # rejected outright: also broken
    assert parsed.get("password") != "abc@123"


def no_env_files(monkeypatch):
    """The scripts read no .env files in these tests, and no stray settings leak in."""
    monkeypatch.setattr(seed, "load_layers", lambda *args, **kwargs: [])
    monkeypatch.setattr(activity, "load_layers", lambda *args, **kwargs: [])
    for name in ("VENDOR_ADMIN_URL", "VENDOR_ACTIVITY_URL", "VENDOR_HOST", "VENDOR_PORT", "VENDOR_DB", "VENDOR_SSLMODE"):
        monkeypatch.delenv(name, raising=False)


def test_seed_admin_url_encodes_the_root_env_password(monkeypatch):
    no_env_files(monkeypatch)
    monkeypatch.setenv("POSTGRES_USER", "vendor")
    monkeypatch.setenv("POSTGRES_PASSWORD", TRICKY_PASSWORD)
    monkeypatch.setenv("POSTGRES_DB", "vendordb")
    parsed = conninfo_to_dict(seed.admin_url())
    assert (parsed["password"], parsed["host"], parsed["port"]) == (TRICKY_PASSWORD, "localhost", "5433")


def test_activity_url_encodes_the_role_password(monkeypatch):
    no_env_files(monkeypatch)
    monkeypatch.setenv("VENDOR_ACTIVITY_PASSWORD", TRICKY_PASSWORD)
    monkeypatch.setenv("POSTGRES_DB", "vendordb")
    parsed = conninfo_to_dict(activity.activity_url())
    assert (parsed["user"], parsed["password"], parsed["dbname"]) == \
        ("vendor_activity", TRICKY_PASSWORD, "vendordb")


def test_a_full_url_is_used_as_given(monkeypatch):
    # Neon's console gives an already-encoded URL; encoding it again would break it.
    given = "postgresql://neondb_owner:abc%40123@example.neon.tech/vendordb?sslmode=require"
    no_env_files(monkeypatch)
    monkeypatch.setenv("VENDOR_ADMIN_URL", given)
    assert seed.admin_url() == given
