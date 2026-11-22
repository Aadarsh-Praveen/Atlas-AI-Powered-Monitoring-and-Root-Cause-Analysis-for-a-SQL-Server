"""Tests for tools/envconfig.py: the layered settings every component uses.

No database, no real .env: every test writes its own small files in a
temporary folder and passes its own environment dictionary, so nothing on
the machine is read or changed.

    python -m pytest tools/tests -v       (any virtual environment with pytest and python-dotenv)
"""

import os
import sys
from pathlib import Path

import pytest
from psycopg.conninfo import conninfo_to_dict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import envconfig  # noqa: E402
from envconfig import load_layers, postgres_url, setting, vendor_url  # noqa: E402


@pytest.fixture
def files(tmp_path):
    """A root .env and a component folder with its own .env (written per test)."""
    root = tmp_path / "root.env"
    component = tmp_path / "component"
    component.mkdir()

    def write(root_text=None, own_text=None):
        if root_text is not None:
            root.write_text(root_text)
        if own_text is not None:
            (component / ".env").write_text(own_text)
        return root, component
    return write


# -----------------------------------------------------------------------------
# The order
# -----------------------------------------------------------------------------
def test_component_file_overrides_root(files):
    root, component = files("SHARED=from-root\nONLY_ROOT=r\n", "SHARED=from-component\n")
    env = {}
    used = load_layers(component, root, env)
    assert env["SHARED"] == "from-component"
    assert env["ONLY_ROOT"] == "r"
    assert used == [str(component / ".env"), str(root)]


def test_real_environment_wins_over_both(files):
    root, component = files("SHARED=from-root\n", "SHARED=from-component\n")
    env = {"SHARED": "from-environment"}
    load_layers(component, root, env)
    assert env["SHARED"] == "from-environment"


def test_root_only_when_the_component_has_no_file(files):
    root, component = files("SHOPLITE_ETL_PASSWORD=x\n")
    env = {}
    assert load_layers(component, root, env) == [str(root)]
    assert env["SHOPLITE_ETL_PASSWORD"] == "x"


def test_an_empty_value_doesnt_block_a_lower_layer(files):
    # e.g. the component file ships "ADX_INGEST_URI=" empty: the root value still applies.
    root, component = files("VALUE=from-root\n", "VALUE=\n")
    env = {}
    load_layers(component, root, env)
    assert env["VALUE"] == "from-root"


# -----------------------------------------------------------------------------
# Production: the root file is never needed
# -----------------------------------------------------------------------------
def test_unreadable_root_env_is_skipped_without_error(files, monkeypatch):
    root, component = files("SA_PASSWORD=admin-secret\n", "SALESDB_PASSWORD=etl\n")
    real_read = Path.read_text

    def locked(path, *args, **kwargs):
        if Path(path) == root:
            raise PermissionError(13, "Access is denied", str(path))   # what a service account gets on the VM
        return real_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", locked)
    env = {}
    used = load_layers(component, root, env)
    assert used == [str(component / ".env")]
    assert env == {"SALESDB_PASSWORD": "etl"}           # nothing from the admin's file


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="file permissions work differently here")
def test_unreadable_root_env_by_file_permissions(files):
    root, component = files("SA_PASSWORD=admin-secret\n", "SQL_PASSWORD=monitor\n")
    root.chmod(0)                                        # nobody may read it
    try:
        env = {}
        load_layers(component, root, env)
        assert "SA_PASSWORD" not in env and env["SQL_PASSWORD"] == "monitor"
    finally:
        root.chmod(0o600)


def test_load_root_env_false_never_reads_the_root(files):
    root, component = files("SHOPLITE_APP_PASSWORD=from-root\n", "LOAD_ROOT_ENV=false\nDB_PASSWORD=own\n")
    env = {}
    used = load_layers(component, root, env)
    assert used == [str(component / ".env")]
    assert "SHOPLITE_APP_PASSWORD" not in env


# -----------------------------------------------------------------------------
# Fallback names
# -----------------------------------------------------------------------------
def test_setting_prefers_the_components_own_name():
    env = {"DB_PASSWORD": "own", "SHOPLITE_APP_PASSWORD": "root"}
    assert setting("DB_PASSWORD", "SHOPLITE_APP_PASSWORD", environ=env) == "own"


def test_setting_falls_back_to_the_canonical_name():
    env = {"DB_PASSWORD": "  ", "SHOPLITE_APP_PASSWORD": "root"}
    assert setting("DB_PASSWORD", "SHOPLITE_APP_PASSWORD", environ=env) == "root"
    assert setting("NOPE", default="d", environ=env) == "d"


# -----------------------------------------------------------------------------
# URLs built from parts
# -----------------------------------------------------------------------------
TRICKY = "p@ss#word%25/:?&= x"


def test_postgres_url_encodes_every_special_character():
    parsed = conninfo_to_dict(postgres_url("odd@user", TRICKY, "localhost", 5433, "db#1", sslmode="require"))
    assert (parsed["user"], parsed["password"], parsed["dbname"], parsed["sslmode"]) == \
        ("odd@user", TRICKY, "db#1", "require")


def test_vendor_url_built_for_each_role():
    env = {"SHOPLITE_READER_PASSWORD": TRICKY, "VENDOR_ACTIVITY_PASSWORD": "a#1",
           "POSTGRES_USER": "vendor", "POSTGRES_PASSWORD": "v@2", "POSTGRES_DB": "vendordb"}
    reader = conninfo_to_dict(vendor_url("reader", environ=env))
    assert (reader["user"], reader["password"], reader["host"], reader["port"]) == \
        ("shoplite_reader", TRICKY, "localhost", "5433")
    assert conninfo_to_dict(vendor_url("activity", environ=env))["password"] == "a#1"
    admin = conninfo_to_dict(vendor_url("admin", environ=env))
    assert (admin["user"], admin["password"]) == ("vendor", "v@2")


def test_an_explicit_url_is_used_as_given():
    given = "postgresql://shoplite_reader:abc%40123@ep-x.neon.tech/vendordb?sslmode=require"
    env = {"VENDOR_READER_URL": given, "SHOPLITE_READER_PASSWORD": "ignored", "POSTGRES_DB": "x"}
    assert vendor_url("reader", explicit="VENDOR_READER_URL", environ=env) == given


def test_vendor_url_is_none_when_a_part_is_missing():
    assert vendor_url("reader", environ={"POSTGRES_DB": "vendordb"}) is None


def test_the_repo_root_is_found():
    assert (envconfig.REPO_ROOT / "CLAUDE.md").exists()
