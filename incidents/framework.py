"""The incident injector's framework: everything the incidents share.

An incident causes one realistic problem on purpose, for a limited time, then
puts everything back. The framework makes every incident follow the same
safety rules (docs/phase-5-monitoring.md, A5):

  - start, revert, and a MAXIMUM DURATION after which it reverts itself;
  - a DRY RUN that shows what would happen and changes nothing;
  - production guards: weekdays only, starting no earlier than 13:30 UTC (the
    alert emails are suppressed before that) and finishing by 22:30 UTC (the
    VM deallocates at 23:00); shoppers must be active (traffic), so there's
    something to affect; one incident at a time;
  - GROUND TRUTH, recorded where Atlas can never read it: what was injected,
    when, by which sessions, the true root cause, and the correct fix. In
    production it goes to its own storage account (never the backup account:
    Atlas may later get read access there) through a write-only SAS
    (create and write, no read, no list: even the injector can't read it back);
    locally to incidents/.ground-truth/ (git-ignored). No ground truth, no
    incident: if recording the start fails, nothing is started.
  - NO GIVEAWAY: each incident connects with a realistic program name and login
    that fit its story; nothing is named "injector".

How a run goes (Runner.start):
  guards -> dry run? -> record start -> save state -> incident.start()
  -> incident.hold() until the deadline or Ctrl+C -> incident.revert()
  -> clear state -> record end
A crash or a VM shutdown in the middle leaves the state file (and possibly a
persistent change: a dropped index, a renamed column). 'status' shows both,
'revert' cleans up both; incidents' detect() and revert() work from the live
system, not only from the state file.
"""

import json
import os
import signal
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

INCIDENTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = INCIDENTS_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))
from envconfig import load_layers, setting, vendor_url  # noqa: E402
ODBC_DRIVER = "ODBC Driver 18 for SQL Server"

EARLIEST_START_UTC = (13, 30)       # alert emails are suppressed before this
LATEST_END_UTC = (22, 30)           # the VM deallocates at 23:00


class Refused(Exception):
    """A guard said no. Nothing was changed."""


# -----------------------------------------------------------------------------
# Settings
# -----------------------------------------------------------------------------
@dataclass
class Settings:
    environment: str                 # "local" (Docker on the Mac) or "prod"
    host: str                        # "vm" or "mac": where this copy runs (prod only)
    sql_host: str
    sql_port: int
    sql_admin_user: str | None       # None = Windows authentication (the VM's administrator)
    sql_admin_password: str | None
    sql_trust_certificate: bool
    api_env_file: Path               # the API's .env: shoplite_app's password, feature flags
    api_restart_command: str | None  # how to restart the API after changing its .env
    api_base_url: str                # the API itself, for incidents that add load through it
    vendor_admin_url: str | None
    ground_truth_sas_url: str | None
    ground_truth_dir: Path
    state_dir: Path

    @staticmethod
    def load(env_file: Path | None = None) -> "Settings":
        """Layered (tools/envconfig.py): real environment > incidents/.env (or
        --env-file) > the root .env. Locally the root .env is enough; the VM and
        the Mac's production copy use their own file with LOAD_ROOT_ENV=false."""
        load_layers(INCIDENTS_DIR, own_env=env_file)

        def get(name, default=None):
            value = os.environ.get(name, "").strip()
            return value or default

        environment = get("INJECTOR_ENVIRONMENT", "local")
        if environment not in ("local", "prod"):
            raise Refused("INJECTOR_ENVIRONMENT must be local or prod")
        host = get("INJECTOR_HOST", "vm" if environment == "prod" else "mac")
        # The admin login. Windows authentication (the VM's administrator, a plausible
        # human DBA) ONLY in the VM's production copy, and only while SQL_ADMIN_USER is
        # empty there. Everywhere else an empty value means "sa", as in the root
        # .env.example: a Mac has no Windows login to offer SQL Server.
        windows_auth_allowed = environment == "prod" and host == "vm"
        trust = (get("SQL_TRUST_SERVER_CERTIFICATE", "true")).lower()
        if trust not in ("true", "false"):
            raise Refused(f"SQL_TRUST_SERVER_CERTIFICATE must be true or false, found '{trust}'")
        settings = Settings(
            environment=environment, host=host,
            sql_host=get("SQL_HOST", "localhost"), sql_port=int(get("SQL_PORT", "1433")),
            sql_admin_user=get("SQL_ADMIN_USER", None if windows_auth_allowed else "sa"),
            sql_admin_password=setting("SQL_ADMIN_PASSWORD", "SA_PASSWORD"),
            # Layered like everything else (real env > own file > root .env); true when
            # unset, like the ETL and the collector (the local container's certificate
            # is self-signed). The VM template sets it explicitly.
            sql_trust_certificate=trust == "true",
            api_env_file=Path(get("API_ENV_FILE", str(REPO_ROOT / "shoplite" / "api" / ".env"))),
            api_restart_command=get("API_RESTART_COMMAND"),
            api_base_url=(setting("API_BASE_URL", "API_URL") or "http://localhost:8000").rstrip("/"),
            # VENDOR_ADMIN_URL as given (Neon), or built from the root .env's POSTGRES_* (encoded).
            vendor_admin_url=vendor_url("admin", explicit="VENDOR_ADMIN_URL"),
            ground_truth_sas_url=get("GROUND_TRUTH_SAS_URL"),
            ground_truth_dir=Path(get("GROUND_TRUTH_DIR", str(INCIDENTS_DIR / ".ground-truth"))),
            state_dir=Path(get("STATE_DIR", str(INCIDENTS_DIR / ".state"))),
        )
        if environment == "prod" and not settings.ground_truth_sas_url:
            raise Refused("GROUND_TRUTH_SAS_URL is required in production (no ground truth, no incident)")
        return settings


# -----------------------------------------------------------------------------
# Ground truth
# -----------------------------------------------------------------------------
class GroundTruth:
    """Writes one JSON document per event (start, end). Never reads anything
    back: in production the SAS can't read, by design."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def write(self, record: dict, kind: str) -> str:
        day = record["started_at"][:10]
        name = f"{day}/{record['incident_id']}-{kind}.json"
        body = json.dumps(record, indent=2, default=str).encode("utf-8")
        if self.settings.environment == "prod":
            base, _, sas = self.settings.ground_truth_sas_url.partition("?")
            request = urllib.request.Request(f"{base.rstrip('/')}/{name}?{sas}", data=body, method="PUT",
                                             headers={"x-ms-blob-type": "BlockBlob",
                                                      "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    if response.status not in (200, 201):
                        raise OSError(f"HTTP {response.status}")
            except (urllib.error.URLError, OSError) as error:
                raise OSError(f"Couldn't write the ground truth ({name}): {error}") from None
            return f"ground-truth storage: {name}"
        path = self.settings.ground_truth_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return str(path)


# -----------------------------------------------------------------------------
# State: the active incident, so 'status' and 'revert' work after a crash
# -----------------------------------------------------------------------------
class State:
    def __init__(self, settings: Settings):
        self.path = settings.state_dir / "active.json"

    def load(self) -> dict | None:
        return json.loads(self.path.read_text()) if self.path.exists() else None

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, default=str))

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


# -----------------------------------------------------------------------------
# What an incident works with
# -----------------------------------------------------------------------------
@dataclass
class Context:
    settings: Settings
    dry_run: bool
    minutes: int
    deadline: datetime
    actions: list[str] = field(default_factory=list)       # what was done, for the ground truth
    sessions: list[dict] = field(default_factory=list)      # the injected sessions
    details: dict = field(default_factory=dict)             # anything revert() will need, saved in the state
    stop_requested: bool = False

    # ---- doing things (or, in a dry run, only saying them) ----
    def do(self, description: str, action=None):
        """Every change goes through here: logged, and skipped in a dry run."""
        self.actions.append(description)
        print(("  [dry run] would: " if self.dry_run else "  ") + description, flush=True)
        if action is not None and not self.dry_run:
            return action()
        return None

    def note(self, message: str) -> None:
        print("  " + message, flush=True)

    # ---- time ----
    def remaining(self) -> float:
        return (self.deadline - datetime.now(timezone.utc)).total_seconds()

    def should_stop(self) -> bool:
        return self.stop_requested or self.remaining() <= 0

    def wait(self, seconds: float) -> None:
        """Sleep in small steps, so Ctrl+C or the deadline ends it quickly."""
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.should_stop():
            time.sleep(min(1.0, end - time.monotonic()))

    # ---- SQL Server ----
    def sql(self, program_name: str, login: str = "admin", database: str = "SalesDB",
            autocommit: bool = True):
        """A connection with the incident's own story: program name and login.
        login "admin": the SQL admin (Windows authentication on the VM, a
        plausible human DBA); "app": shoplite_app, the API's login."""
        import pyodbc
        parts = [f"DRIVER={{{ODBC_DRIVER}}}", f"SERVER={self.settings.sql_host},{self.settings.sql_port}",
                 f"DATABASE={database}", "Encrypt=yes",
                 f"TrustServerCertificate={'yes' if self.settings.sql_trust_certificate else 'no'}",
                 f"APP={program_name}"]
        if login == "app":
            parts += ["UID=shoplite_app", "PWD={" + self.api_setting("DB_PASSWORD").replace("}", "}}") + "}"]
        elif self.settings.sql_admin_user:
            if not self.settings.sql_admin_password:
                raise Refused(f"no password for the SQL admin login {self.settings.sql_admin_user}: "
                              "set SQL_ADMIN_PASSWORD (or SA_PASSWORD in the root .env)")
            parts += ["UID={" + self.settings.sql_admin_user + "}",
                      "PWD={" + self.settings.sql_admin_password.replace("}", "}}") + "}"]
        else:
            parts.append("Trusted_Connection=yes")
        connection = pyodbc.connect(";".join(parts) + ";", timeout=10, autocommit=autocommit)
        session_id = connection.cursor().execute("SELECT @@SPID").fetchone()[0]
        self.sessions.append({"session_id": session_id, "program_name": program_name,
                              "login": "shoplite_app" if login == "app" else "SQL admin",
                              "connected_at": datetime.now(timezone.utc).isoformat()})
        return connection

    def scalar(self, connection, sql: str, *params):
        row = connection.cursor().execute(sql, *params).fetchone()
        return None if row is None else row[0]

    # ---- the API's settings (its .env on the VM; locally usually the root .env) ----
    API_FALLBACKS = {"DB_PASSWORD": ("DB_PASSWORD", "SHOPLITE_APP_PASSWORD"), "ADMIN_TOKEN": ("ADMIN_TOKEN",)}

    def api_setting(self, name: str) -> str:
        """A value the API itself uses: from the API's own .env if it has it,
        else from the layered environment (locally the root .env, under its
        canonical name: DB_PASSWORD -> SHOPLITE_APP_PASSWORD)."""
        from dotenv import dotenv_values
        names = self.API_FALLBACKS.get(name, (name,))
        if self.settings.api_env_file.exists():
            values = dotenv_values(self.settings.api_env_file)
            for candidate in names:
                if (values.get(candidate) or "").strip():
                    return values[candidate].strip()
        value = setting(*names)
        if value is None:
            raise Refused(f"{name} isn't set in {self.settings.api_env_file} or the root .env")
        return value


# -----------------------------------------------------------------------------
# The base class every incident file defines
# -----------------------------------------------------------------------------
class Incident:
    name = ""                        # command-line name, e.g. "blocking-chain"
    title = ""
    runs_on = ("vm",)                # where in production: "vm" (SQL Server, the API) and/or "mac" (Neon)
    default_minutes = 20
    max_minutes = 45
    foreground = True                # False: start, record, exit; revert later (multi-day incidents)
    needs_traffic = True             # shoppers must be active for it to matter

    # The answer key (ground truth), filled by each incident:
    root_cause = ""
    correct_fix = ""
    expected_signals: list[str] = []

    def preflight(self, ctx: Context) -> None:
        """Check it can run (e.g. the index exists). Raise Refused if not."""

    def start(self, ctx: Context) -> None:
        """Cause the problem. Every CHANGE goes through ctx.do(), so a dry run
        changes nothing; reading (checks, queries) is fine either way."""
        raise NotImplementedError

    def hold(self, ctx: Context) -> None:
        """Keep the incident going until the deadline or Ctrl+C. Default: wait."""
        ctx.wait(ctx.remaining())

    def revert(self, ctx: Context) -> None:
        """Put everything back. Must be safe to run twice, and must work from the
        live system (after a crash, ctx.details may be empty)."""
        raise NotImplementedError

    def detect(self, ctx: Context) -> str | None:
        """For 'status': describe a persistent change still in place, or None.
        Session-only incidents (their effect ends with their connection) have none."""
        return None


# -----------------------------------------------------------------------------
# Guards
# -----------------------------------------------------------------------------
def check_time_window(minutes: int, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    if now.weekday() >= 5:
        raise Refused("Production incidents run on weekdays only (the VM is off at weekends).")
    earliest = now.replace(hour=EARLIEST_START_UTC[0], minute=EARLIEST_START_UTC[1], second=0, microsecond=0)
    latest = now.replace(hour=LATEST_END_UTC[0], minute=LATEST_END_UTC[1], second=0, microsecond=0)
    if now < earliest:
        raise Refused(f"Too early: start at {earliest:%H:%M} UTC or later (alert emails are suppressed before).")
    if now + timedelta(minutes=minutes) > latest:
        raise Refused(f"It would still run at {latest:%H:%M} UTC; the VM deallocates at 23:00. "
                      f"Use --minutes {max(0, int((latest - now).total_seconds() // 60))} or start earlier tomorrow.")


def check_traffic(ctx: Context, strict: bool) -> None:
    """Shoppers are active if the API's SQL sessions finished a request in the
    last 5 minutes (the GitHub variable itself isn't readable from here).
    strict (production): no traffic refuses the incident. Locally it only warns,
    since Locust usually isn't running on the Mac."""
    connection = ctx.sql("Microsoft SQL Server Management Studio")
    try:
        active = ctx.scalar(connection, """
            SELECT COUNT(*) FROM sys.dm_exec_sessions
            WHERE program_name LIKE N'ShopLite-API/%'
              AND last_request_end_time > DATEADD(MINUTE, -5, GETDATE())""")
    finally:
        connection.close()
        ctx.sessions.pop()                  # this check isn't part of the incident
    if not active:
        message = "No shopper traffic in the last 5 minutes (TRAFFIC_ENABLED=true? inside business hours?)."
        if strict:
            raise Refused(message)
        ctx.note("WARNING: " + message + " Continuing (local).")


# -----------------------------------------------------------------------------
# The runner
# -----------------------------------------------------------------------------
class Runner:
    def __init__(self, settings: Settings, incidents: dict[str, Incident]):
        self.settings = settings
        self.incidents = incidents
        self.state = State(settings)
        self.truth = GroundTruth(settings)

    def _context(self, minutes: int, dry_run: bool) -> Context:
        return Context(self.settings, dry_run, minutes,
                       datetime.now(timezone.utc) + timedelta(minutes=minutes))

    def start(self, name: str, minutes: int | None, dry_run: bool, skip_traffic_check: bool) -> int:
        incident = self.incidents[name]
        minutes = minutes or incident.default_minutes
        if not 1 <= minutes <= incident.max_minutes:
            raise Refused(f"--minutes must be between 1 and {incident.max_minutes} for {name}")
        prod = self.settings.environment == "prod"
        if prod and self.settings.host not in incident.runs_on:
            raise Refused(f"{name} runs on: {', '.join(incident.runs_on)} (this copy is INJECTOR_HOST={self.settings.host})")
        active = self.state.load()
        if active:
            raise Refused(f"An incident is already active: {active['name']} (started {active['started_at']}). "
                          "Run 'status', then 'revert' if it's left over.")
        if prod and incident.foreground:
            check_time_window(minutes)

        ctx = self._context(minutes, dry_run)
        print(f"{incident.title}: {minutes} min, until {ctx.deadline:%H:%M} UTC ({self.settings.environment})")
        # The check needs SQL Server: on the VM in production, or locally.
        can_check = self.settings.environment == "local" or self.settings.host == "vm"
        if incident.needs_traffic and can_check and not skip_traffic_check:
            check_traffic(ctx, strict=prod)
        incident.preflight(ctx)

        record = {
            "incident_id": str(uuid.uuid4()), "name": incident.name, "title": incident.title,
            "environment": self.settings.environment, "dry_run": dry_run,
            "started_at": datetime.now(timezone.utc).isoformat(), "planned_minutes": minutes,
            "deadline": ctx.deadline.isoformat(), "root_cause": incident.root_cause,
            "correct_fix": incident.correct_fix, "expected_signals": incident.expected_signals,
        }
        if dry_run:
            print("  [dry run] ground truth that would be recorded:")
            print(json.dumps(record, indent=2))
            incident.start(ctx)
            print("  [dry run] nothing was changed.")
            return 0

        where = self.truth.write(record, "start")        # no ground truth, no incident
        print(f"  ground truth recorded: {where}")
        self.state.save({**record, "details": {}})

        outcome = "timeout"
        previous = signal.signal(signal.SIGINT, lambda *_: setattr(ctx, "stop_requested", True))
        try:
            incident.start(ctx)
            self.state.save({**record, "details": ctx.details, "sessions": ctx.sessions})
            if not incident.foreground:
                print(f"  Started. It stays active until you run: python incidents/injector.py revert {name}")
                return 0
            print(f"  Active. Reverts at {ctx.deadline:%H:%M} UTC, or press Ctrl+C to revert now.")
            incident.hold(ctx)
            if ctx.stop_requested:
                outcome = "stopped (Ctrl+C)"
        except Exception as error:
            outcome = f"failed: {type(error).__name__}: {error}"
            print(f"  ERROR: {error}. Reverting.")
        finally:
            signal.signal(signal.SIGINT, previous)
            if incident.foreground or outcome.startswith("failed"):
                self._finish(incident, ctx, record, outcome)
        return 0 if not outcome.startswith("failed") else 1

    def _finish(self, incident: Incident, ctx: Context, record: dict, outcome: str) -> None:
        print("  Reverting ...")
        incident.revert(ctx)
        self.state.clear()
        end = {**record, "ended_at": datetime.now(timezone.utc).isoformat(), "outcome": outcome,
               "injected_sessions": ctx.sessions, "actions": ctx.actions}
        try:
            print(f"  ground truth recorded: {self.truth.write(end, 'end')}")
        except OSError as error:
            fallback = INCIDENTS_DIR / ".ground-truth-unsent" / f"{record['incident_id']}-end.json"
            fallback.parent.mkdir(parents=True, exist_ok=True)
            fallback.write_text(json.dumps(end, indent=2, default=str))
            print(f"  WARNING: {error}. Saved locally to {fallback}; upload it by hand.")
        print(f"  Reverted ({outcome}).")

    def status(self) -> int:
        active = self.state.load()
        print(f"Active incident: {active['name']} since {active['started_at']} (until {active['deadline']})"
              if active else "Active incident: none recorded")
        s = self.settings
        print(f"SQL Server: {s.sql_host},{s.sql_port} as "
              f"{s.sql_admin_user or 'Windows authentication'}, "
              f"trust certificate: {'yes' if s.sql_trust_certificate else 'no'}")
        ctx = self._context(1, dry_run=True)
        found, unchecked = False, []
        for incident in self.incidents.values():
            if s.environment == "prod" and s.host not in incident.runs_on:
                continue
            try:
                change = incident.detect(ctx)
            except Exception as error:            # can't check (e.g. no connection): say so
                change = None
                unchecked.append(incident.name)
                print(f"  {incident.name}: couldn't check ({type(error).__name__}: {error})")
            if change:
                found = True
                print(f"  STILL IN PLACE: {incident.name}: {change}  -> revert with: "
                      f"python incidents/injector.py revert {incident.name}")
        scope = "" if s.environment == "local" or s.host == "vm" else " (Only Mac-side incidents were checked here.)"
        if unchecked:
            # "Nothing found" would be a lie: an unchecked incident may still be in place.
            print(f"Result UNKNOWN: couldn't check {len(unchecked)} incident(s): {', '.join(unchecked)}. "
                  "Fix the cause above and run status again." + scope)
            return 3
        if not found:
            print("No persistent incident changes found." + scope)
        return 0

    def revert(self, name: str | None) -> int:
        active = self.state.load()
        name = name or (active["name"] if active else None)
        if not name:
            print("Nothing to revert: no active incident recorded. ('status' checks for leftovers.)")
            return 0
        incident = self.incidents[name]
        ctx = self._context(1, dry_run=False)
        if active and active["name"] == name:
            ctx.details = active.get("details", {})
            self._finish(incident, ctx, {k: v for k, v in active.items() if k not in ("details", "sessions")},
                         "reverted by hand")
        else:
            print(f"Reverting {name} (no matching state: cleaning up from the live system)")
            incident.revert(ctx)
            print("  Done.")
        return 0
