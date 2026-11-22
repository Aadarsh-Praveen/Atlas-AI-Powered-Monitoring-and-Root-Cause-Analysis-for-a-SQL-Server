"""Layered settings for every component: one place that decides where values come from.

Order (highest first; a variable set higher up is never replaced by a lower one):
  1. real environment variables   GitHub Actions, one-off overrides
  2. the component's own .env     e.g. server/etl/.env: the VM, or a local override
  3. the root .env                your Mac: every local value lives here

So locally you keep everything in the root .env, with one canonical name per
login (SHOPLITE_APP_PASSWORD, SHOPLITE_ETL_PASSWORD, ...; see .env.example),
and components fall back to those names when their own isn't set.

Production (the VM): each service has its own .env, locked by ACL to its
service account, and that file is its ONLY source:
  - the root C:\\atlas\\.env holds the SQL admin password and is readable by
    Administrators only; a root .env that can't be read is skipped (one debug
    log line), never an error;
  - each service's VM .env says LOAD_ROOT_ENV=false, so the root file is never
    used even when someone runs the service by hand as Administrator (where
    it WOULD be readable): a missing value then fails the same way it would
    for the service account, instead of silently coming from the admin's file.

Usage, in a component:
    import sys; sys.path.insert(0, str(REPO_ROOT / "tools"))
    from envconfig import load_layers, setting, vendor_url
    load_layers(COMPONENT_DIR)                       # before reading any setting
    password = setting("SALESDB_PASSWORD", "SHOPLITE_ETL_PASSWORD")
    url = vendor_url("reader", explicit="VENDOR_READER_URL")

Uses python-dotenv only to parse files; nothing here touches a network or a
database.
"""

import io
import logging
import os
from pathlib import Path
from urllib.parse import quote, urlencode

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT_ENV = REPO_ROOT / ".env"

log = logging.getLogger("envconfig")

# The vendor's Postgres logins and which root variables hold their parts.
VENDOR_ROLES = {
    "reader":   {"user": "shoplite_reader", "password": "SHOPLITE_READER_PASSWORD"},
    "activity": {"user": "vendor_activity", "password": "VENDOR_ACTIVITY_PASSWORD"},
    "admin":    {"user_var": "POSTGRES_USER", "password": "POSTGRES_PASSWORD"},
}


def _read(path: Path) -> str | None:
    """A .env file's text, or None if it doesn't exist or can't be read."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:                          # PermissionError on the VM's locked root .env
        log.debug("skipping %s: %s", path, error)
        return None


def _apply(text: str, environ) -> None:
    """Set every variable from a .env text that isn't set yet (never override).
    Empty values ("ADX_INGEST_URI=") aren't set at all: an empty value means
    "not set here", so a lower layer or a fallback name can still supply it."""
    from dotenv import dotenv_values
    for name, value in dotenv_values(stream=io.StringIO(text)).items():
        if value and value.strip() and not environ.get(name, "").strip():
            environ[name] = value


def load_layers(component_dir: Path | None = None, root_env: Path = ROOT_ENV, environ=None,
                own_env: Path | None = None) -> list[str]:
    """Load the component's .env, then the root .env, neither overriding what's
    already set. Returns the files actually used (for a startup log line).
    own_env: a specific file instead of <component_dir>/.env (e.g. --env-file)."""
    environ = os.environ if environ is None else environ
    used = []
    own = Path(own_env) if own_env else (Path(component_dir) / ".env" if component_dir is not None else None)
    if own is not None:
        text = _read(own)
        if text is not None:
            _apply(text, environ)
            used.append(str(own))
    if environ.get("LOAD_ROOT_ENV", "").strip().lower() == "false":
        return used                                    # production services: their own file only
    text = _read(root_env)
    if text is not None:
        _apply(text, environ)
        used.append(str(root_env))
    return used


def setting(*names: str, default: str | None = None, environ=None) -> str | None:
    """The first of these variables that is set (not empty): a component's own
    name first, then the canonical root name. E.g. setting("DB_PASSWORD", "SHOPLITE_APP_PASSWORD")."""
    environ = os.environ if environ is None else environ
    for name in names:
        value = environ.get(name, "").strip()
        if value:
            return value
    return default


def postgres_url(user: str, password: str, host: str, port, dbname: str, **options) -> str:
    """postgresql://user:password@host:port/dbname[?options], user, password, and
    database percent-encoded: "@" -> %40, "#" -> %23, "%" -> %25, "/" -> %2F, ...
    (safe="" so even "/" is encoded). Postgres decodes them back."""
    def encode(value) -> str:
        return quote(str(value), safe="")
    url = f"postgresql://{encode(user)}:{encode(password)}@{host}:{port}/{encode(dbname)}"
    clean = {k: v for k, v in options.items() if v}
    return url + ("?" + urlencode(clean) if clean else "")


def vendor_url(role: str, explicit: str | None = None, environ=None) -> str | None:
    """The vendor's Postgres URL for a login ("reader", "activity", "admin").

    If the explicit variable (e.g. VENDOR_READER_URL) is set, it's used exactly
    as given (Neon's URLs are already encoded). Otherwise it's built from parts:
    the role's user and password (root variables), VENDOR_HOST (default
    localhost), VENDOR_PORT (default 5433, the local vendor-pg), POSTGRES_DB,
    and VENDOR_SSLMODE if set. None if a part is missing."""
    environ = os.environ if environ is None else environ
    if explicit:
        given = setting(explicit, environ=environ)
        if given:
            return given
    spec = VENDOR_ROLES[role]
    user = spec.get("user") or setting(spec["user_var"], environ=environ)
    password = setting(spec["password"], environ=environ)
    database = setting("VENDOR_DB", "POSTGRES_DB", environ=environ)
    if not (user and password and database):
        return None
    return postgres_url(user, password,
                        setting("VENDOR_HOST", default="localhost", environ=environ),
                        setting("VENDOR_PORT", default="5433", environ=environ),
                        database, sslmode=setting("VENDOR_SSLMODE", environ=environ))
