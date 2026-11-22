"""Tests for the injector's settings and 'status' (incidents/framework.py).

No database, no real .env: Settings.load's layered loader is pointed at
temporary files written by each test. Run from the repo root:

    incidents/.venv/bin/python -m pytest incidents/tests -v
"""

import sys
from pathlib import Path

import pytest

INCIDENTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INCIDENTS_DIR))

import framework  # noqa: E402
from framework import Incident, Refused, Runner, Settings  # noqa: E402

NAMES = ("INJECTOR_ENVIRONMENT", "INJECTOR_HOST", "SQL_ADMIN_USER", "SQL_ADMIN_PASSWORD", "SA_PASSWORD",
         "SQL_TRUST_SERVER_CERTIFICATE", "LOAD_ROOT_ENV", "GROUND_TRUTH_SAS_URL", "STATE_DIR",
         "VENDOR_ADMIN_URL", "POSTGRES_USER", "POSTGRES_PASSWORD")


@pytest.fixture
def layers(tmp_path, monkeypatch):
    """Returns load(root_text, own_text=None) -> Settings, reading only temp files."""
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    real_load_layers = framework.load_layers

    def load(root_text: str, own_text: str | None = None) -> Settings:
        root = tmp_path / "root.env"
        root.write_text(root_text)
        own = tmp_path / "own.env"
        if own_text is not None:
            own.write_text(own_text)
        monkeypatch.setattr(framework, "load_layers",
                            lambda component_dir=None, own_env=None: real_load_layers(
                                component_dir=None, own_env=own, root_env=root))
        return Settings.load()

    return load


def test_local_trusts_the_certificate_from_the_root_env(layers):
    assert layers("SQL_TRUST_SERVER_CERTIFICATE=true\n").sql_trust_certificate is True


def test_local_trust_defaults_to_true(layers):
    assert layers("").sql_trust_certificate is True


def test_root_false_is_honoured(layers):
    assert layers("SQL_TRUST_SERVER_CERTIFICATE=false\n").sql_trust_certificate is False


def test_a_typo_in_trust_is_refused(layers):
    with pytest.raises(Refused):
        layers("SQL_TRUST_SERVER_CERTIFICATE=yes\n")


def test_local_admin_defaults_to_sa(layers):
    settings = layers("SA_PASSWORD=x\n")
    assert (settings.sql_admin_user, settings.sql_admin_password) == ("sa", "x")


def test_local_empty_admin_user_still_means_sa(layers):
    assert layers("SQL_ADMIN_USER=\nSA_PASSWORD=x\n").sql_admin_user == "sa"


def test_vm_production_copy_uses_windows_authentication(layers):
    settings = layers("SQL_ADMIN_USER=sa\nSA_PASSWORD=root-secret\n",   # must NOT be read
                      "LOAD_ROOT_ENV=false\nINJECTOR_ENVIRONMENT=prod\nINJECTOR_HOST=vm\n"
                      "GROUND_TRUTH_SAS_URL=https://example.invalid/c?sig=x\nSQL_ADMIN_USER=\n")
    assert (settings.sql_admin_user, settings.sql_admin_password) == (None, None)


def test_mac_production_copy_never_uses_windows_authentication(layers):
    settings = layers("", "LOAD_ROOT_ENV=false\nINJECTOR_ENVIRONMENT=prod\nINJECTOR_HOST=mac\n"
                          "GROUND_TRUTH_SAS_URL=https://example.invalid/c?sig=x\n")
    assert settings.sql_admin_user == "sa"


# ---- status ----
class Fake(Incident):
    def __init__(self, name, result):
        self.name, self.runs_on, self.result = name, ("vm", "mac"), result

    def detect(self, ctx):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_status_is_unknown_when_an_incident_cannot_be_checked(layers, capsys):
    runner = Runner(layers(""), {"a": Fake("a", None), "b": Fake("b", OSError("SSL Provider: certificate verify failed"))})
    assert runner.status() == 3
    out = capsys.readouterr().out
    assert "Result UNKNOWN" in out and "b" in out
    assert "No persistent incident changes found" not in out


def test_status_clean_when_everything_was_checked(layers, capsys):
    runner = Runner(layers(""), {"a": Fake("a", None)})
    assert runner.status() == 0
    assert "No persistent incident changes found" in capsys.readouterr().out


def test_status_shows_the_login_and_trust_it_used(layers, capsys):
    Runner(layers("SA_PASSWORD=x\n"), {}).status()
    assert "as sa, trust certificate: yes" in capsys.readouterr().out
