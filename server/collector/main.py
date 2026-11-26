"""The ShopLite monitoring collector: the service loop.

Runs on prod-sql-01 as a Windows service (WinSW, like the API; batch 3), and
locally from the Mac for testing. Every minute:

  1. collect     the collections that are due (collect.py)
  2. store       write the records to each destination's buffer on disk
  3. commit      only now move the watermarks forward: the records are safe
  4. flush       send each destination's buffered files, oldest first;
                 a failure leaves the files and backs off that destination
  5. heartbeat   one CollectorHeartbeat_CL row: how the cycle went, each
                 destination's status, how much is buffered. Stored and sent
                 like any other record. Its absence for 5 minutes is an alert.

Usage:
    python main.py                 run as the service does, forever
    python main.py --once          one cycle, then exit
    python main.py --print --once  one cycle, print every table's rows, send
                                   nothing, save no watermarks (Part C1)

Exit codes: 0 stopped normally, 2 configuration problem.

Settings come in layers (tools/envconfig.py): real environment variables,
then server/collector/.env if present, then the repo's root .env. Locally the
root .env is enough (the SQL password falls back to SHOPLITE_MONITOR_PASSWORD;
the vendor URL is built from SHOPLITE_READER_PASSWORD and friends); on the VM
the collector's own .env is its only source (LOAD_ROOT_ENV=false). Monitoring must not depend on what it monitors, so
this runs outside SQL Agent, and keeps running when SQL Server, Log
Analytics, or ADX is down: those show up in the heartbeat instead.
"""

import argparse
import logging
import os
import signal
import socket
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyodbc
import yaml

from buffer import Buffer
from collect import Collector, CycleResult, Settings as CollectSettings, State
from senders import AdxSender, BackOff, LogAnalyticsSender, PrintSender

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tools"))
from envconfig import load_layers, vendor_url  # noqa: E402

VERSION = "1.0.0"
COLLECTOR_DIR = Path(__file__).resolve().parent
REPO_ROOT = COLLECTOR_DIR.parent.parent
SCHEMA_FILE = REPO_ROOT / "observability" / "schema.yml"
ODBC_DRIVER = "ODBC Driver 18 for SQL Server"
CYCLE_SECONDS = 60

log = logging.getLogger("collector")


class ConfigError(Exception):
    pass


# -----------------------------------------------------------------------------
# Settings
# -----------------------------------------------------------------------------
def setting(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if default is not None:
        return default
    raise ConfigError(f"{name} is not set (root .env.example lists the local values; server/collector/.env.example the VM ones)")


def optional(name: str) -> str | None:
    return os.environ.get(name, "").strip() or None


def flag(name: str, default: str) -> bool:
    value = setting(name, default).lower()
    if value not in ("true", "false"):
        raise ConfigError(f"{name} must be true or false, found '{value}'")
    return value == "true"


@dataclass
class Config:
    sql_host: str
    sql_port: int
    sql_user: str
    sql_password: str
    sql_trust_certificate: bool
    sql_connect_timeout: int
    data_dir: Path                      # state file and buffer (F:\collector on the VM)
    collect: CollectSettings
    la_endpoint: str | None
    la_rule_ids: dict[str, str]
    managed_identity_client_id: str | None
    adx_ingest_uri: str | None
    adx_database: str
    adx_tenant_id: str | None
    adx_app_id: str | None
    adx_app_secret: str | None
    buffer_max_mb: int


def load_config() -> Config:
    la_endpoint = optional("LA_DCE_ENDPOINT")
    adx_uri = optional("ADX_INGEST_URI")
    config = Config(
        sql_host=setting("SQL_HOST", "localhost"),
        sql_port=int(setting("SQL_PORT", "1433")),
        sql_user=setting("SQL_USER", "shoplite_monitor"),
        sql_password=optional("SQL_PASSWORD") or setting("SHOPLITE_MONITOR_PASSWORD"),
        sql_trust_certificate=flag("SQL_TRUST_SERVER_CERTIFICATE", "true"),
        sql_connect_timeout=int(setting("SQL_CONNECT_TIMEOUT_SECONDS", "10")),
        data_dir=Path(setting("COLLECTOR_DATA_DIR", str(COLLECTOR_DIR / ".data"))),
        collect=CollectSettings(
            # Default: the local container's path; the VM's .env sets F:\XE\...
            xe_file_pattern=setting("XE_FILE_PATTERN", "/var/opt/mssql/xe/ShopLite_Diagnostics*.xel"),
            # VENDOR_READER_URL as given, or built from the root .env's parts; None = no vendor check.
            vendor_url=vendor_url("reader", explicit="VENDOR_READER_URL"),
            query_timeout=int(setting("SQL_QUERY_TIMEOUT_SECONDS", "15"))),
        la_endpoint=la_endpoint,
        la_rule_ids={name: value for name, value in (
            ("dcr-shoplite-sql", optional("LA_DCR_SQL_IMMUTABLE_ID")),
            ("dcr-shoplite-ops", optional("LA_DCR_OPS_IMMUTABLE_ID"))) if value},
        managed_identity_client_id=optional("MANAGED_IDENTITY_CLIENT_ID"),
        adx_ingest_uri=adx_uri,
        adx_database=setting("ADX_DATABASE", "shoplite"),
        adx_tenant_id=optional("ADX_TENANT_ID"),
        adx_app_id=optional("ADX_APP_ID"),
        adx_app_secret=optional("ADX_APP_SECRET"),
        buffer_max_mb=int(setting("BUFFER_MAX_MB", "500")),
    )
    if la_endpoint and len(config.la_rule_ids) != 2:
        raise ConfigError("LA_DCE_ENDPOINT is set: also set LA_DCR_SQL_IMMUTABLE_ID and LA_DCR_OPS_IMMUTABLE_ID")
    if adx_uri and not (config.adx_tenant_id and config.adx_app_id and config.adx_app_secret):
        raise ConfigError("ADX_INGEST_URI is set: also set ADX_TENANT_ID, ADX_APP_ID, and ADX_APP_SECRET")
    return config


def table_rules() -> dict[str, str]:
    """table -> DCR name, from observability/schema.yml (data_collection_rules)."""
    schema = yaml.safe_load(SCHEMA_FILE.read_text(encoding="utf-8"))
    return {table: rule for rule, tables in schema["data_collection_rules"].items() for table in tables}


def sql_connector(config: Config):
    def braced(value: str) -> str:
        return "{" + value.replace("}", "}}") + "}"

    conn_str = (
        f"DRIVER={{{ODBC_DRIVER}}};SERVER={config.sql_host},{config.sql_port};DATABASE=master;"
        f"UID={braced(config.sql_user)};PWD={braced(config.sql_password)};Encrypt=yes;"
        f"TrustServerCertificate={'yes' if config.sql_trust_certificate else 'no'};"
        "APP=ShopLite-Collector;"        # program_name: Atlas can tell the collector's own sessions apart
    )
    return lambda: pyodbc.connect(conn_str, timeout=config.sql_connect_timeout, autocommit=True)


# -----------------------------------------------------------------------------
# Destinations
# -----------------------------------------------------------------------------
class Destination:
    """A sender plus its buffer and back-off."""

    def __init__(self, sender, buffer: Buffer):
        self.sender, self.buffer, self.backoff = sender, buffer, BackOff()
        self.ok: bool | None = None     # last attempt's result; None = nothing tried yet

    def flush(self) -> None:
        self.buffer.enforce_limits()
        if not self.backoff.ready():
            return
        for path, table, _ in self.buffer.pending():
            try:
                records = Buffer.read(path)
            except ValueError as error:           # damaged file: set aside, don't block the queue
                log.error("%s: unreadable buffer file %s moved aside: %s", self.sender.name, path.name, error)
                path.rename(path.with_suffix(".bad"))
                continue
            try:
                self.sender.send(table, records)
            except Exception as error:            # network, auth, throttling, bad config
                self.ok = False
                self.backoff.failed()
                log.warning("%s: send failed (%s), retrying in %d s: %s", self.sender.name, table,
                            self.backoff.delay, str(error)[:300])
                return
            Buffer.remove(path)
            self.ok = True
        self.backoff.succeeded()


def build_destinations(config: Config, print_only: bool) -> list[Destination]:
    buffers = config.data_dir / "buffer"
    max_bytes = config.buffer_max_mb * 1_048_576
    if print_only:
        return [Destination(PrintSender(), Buffer(buffers, "print", timedelta(hours=1), max_bytes))]
    destinations = []
    if config.la_endpoint:
        destinations.append(Destination(
            LogAnalyticsSender(config.la_endpoint, config.la_rule_ids, table_rules(), config.managed_identity_client_id),
            Buffer(buffers, "loganalytics", timedelta(hours=47), max_bytes)))
    if config.adx_ingest_uri:
        destinations.append(Destination(
            AdxSender(config.adx_ingest_uri, config.adx_database, config.adx_tenant_id,
                      config.adx_app_id, config.adx_app_secret),
            Buffer(buffers, "adx", timedelta(days=7), max_bytes)))
    if not destinations:
        raise ConfigError("No destination configured: set LA_DCE_ENDPOINT and/or ADX_INGEST_URI, or use --print")
    return destinations


# -----------------------------------------------------------------------------
# The cycle
# -----------------------------------------------------------------------------
def heartbeat(collector: Collector, result: CycleResult, seconds: float, destinations: list[Destination]) -> dict:
    status = {d.sender.name: d.ok for d in destinations}
    return {
        "TimeGenerated": datetime.now(timezone.utc),
        "Computer": collector.computer or socket.gethostname(),    # SQL Server unreachable: the host's own name
        "CollectorVersion": VERSION,
        "CycleSeconds": round(seconds, 2),
        "QueriesRun": result.queries_run,
        "QueriesFailed": result.queries_failed,
        "FailedCollections": ", ".join(result.failures) or None,
        "LastError": result.last_error,
        "BufferedRecords": sum(d.buffer.record_count() for d in destinations),
        "SqlOk": result.sql_ok,
        "LogAnalyticsOk": status.get("loganalytics"),
        "AdxOk": status.get("adx"),
    }


def run_cycle(collector: Collector, state: State, destinations: list[Destination], save_state: bool) -> None:
    started = time.monotonic()
    result = collector.run_cycle()
    for destination in destinations:
        for table, records in result.records.items():
            destination.buffer.store(table, records)
    if save_state:
        state.commit()                  # records are on disk: safe to move the watermarks
    else:
        state.discard()                 # --print: re-read the same window next time
    for destination in destinations:
        destination.flush()
    beat = heartbeat(collector, result, time.monotonic() - started, destinations)
    for destination in destinations:
        destination.buffer.store("CollectorHeartbeat_CL", [beat])
        destination.flush()
    rows = sum(len(r) for r in result.records.values())
    # Each failure was already logged with its full message (collect.py); this
    # summary line names them all.
    log.info("cycle: %d rows, %d/%d collections failed%s", rows, result.queries_failed, result.queries_run,
             f": {', '.join(result.failures)}" if result.failures else "")


def main() -> int:
    parser = argparse.ArgumentParser(description="ShopLite monitoring collector")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--print", action="store_true", help="print the rows instead of sending; save no watermarks")
    args = parser.parse_args()

    # To stdout: WinSW writes it to shoplite-collector.out.log (stderr would go to .err.log).
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)sZ %(levelname)s %(name)s: %(message)s")
    logging.Formatter.converter = time.gmtime     # log times in UTC, like everything else
    # The Azure SDKs (azure.core's HTTP logging policy, azure.identity, azure.monitor.ingestion,
    # azure.kusto) log every request and response at INFO, which buries the collector's own lines.
    # "azure" is the parent of all of them: warnings and errors still come through.
    logging.getLogger("azure").setLevel(logging.WARNING)
    load_layers(COLLECTOR_DIR)                    # own .env, then the root .env; never overrides what's set

    try:
        config = load_config()
        config.data_dir.mkdir(parents=True, exist_ok=True)
        destinations = build_destinations(config, args.print)
    except (ConfigError, ValueError, OSError) as error:
        log.error("configuration: %s", error)
        return 2

    state = State(config.data_dir / "state.json")
    collector = Collector(sql_connector(config), config.collect, state)
    log.info("ShopLite collector %s started; destinations: %s", VERSION,
             ", ".join(d.sender.name for d in destinations))

    # Stop cleanly when the service manager asks (Ctrl+C / Ctrl+Break from
    # WinSW, SIGTERM elsewhere): finish the current cycle, then exit.
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stop)

    while True:
        cycle_start = time.monotonic()
        try:
            run_cycle(collector, state, destinations, save_state=not args.print)
        except Exception:                         # never let one bad cycle end monitoring
            log.exception("cycle failed")
        if args.once or stopping:
            break
        # Sleep to the next minute, in small steps so a stop request is quick.
        while not stopping and time.monotonic() - cycle_start < CYCLE_SECONDS:
            time.sleep(1)
        if stopping:
            break
    log.info("collector stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
