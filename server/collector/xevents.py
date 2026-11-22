"""Reads new events from the ShopLite_Diagnostics Extended Events files and
turns each into a SqlXEvents_CL row (observability/schema.yml).

The session (server/jobs/sql/010_server_settings_and_xe.sql) writes rolling
files: ShopLite_Diagnostics_0_<number>.xel, at most 10 of 50 MB. SQL Server's
sys.fn_xe_file_target_read_file reads them; given the last file and offset
read, it returns only the events after it. So each minute the collector reads
just what's new, and saves the new position (in the collector's state file).

Three situations:
  - Normal: resume from the saved position.
  - First start (no position): read everything in the files, but keep only
    the last 24 hours (schema: conventions.incremental).
  - The saved file has rolled away (SQL Server deleted the oldest file): the
    position is invalid, so read everything again and keep only events newer
    than the last one sent. Nothing is lost unless the collector was stopped
    long enough for 500 MB of events to roll past, and nothing is sent twice.

Each event is XML like:
  <event name="rpc_completed" timestamp="...">
    <data name="duration"><value>2500000</value></data>
    <action name="client_app_name"><value>ShopLite-API/1.2.0</value></action>
  </event>
"data" fields belong to the event itself; "action" fields are the extra
context the session asked for (application, database, login, host, ...).
"""

import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import pyodbc

import sql_queries as q

MAX_EVENT_XML = 30000      # schema: EventXml, up to 30000 characters
MAX_SQL_TEXT = 4000
MAX_MESSAGE = 2000


def read_new_events(cursor, file_pattern: str, position: dict | None, since: datetime | None,
                    computer: str) -> tuple[list[dict], dict | None]:
    """Returns (rows, new position). position is {"file", "offset", "timestamp"} or None.
    since: on first start, keep only events after this time."""
    rows = []
    if position:
        try:
            events = q.run_query(cursor, q.XE_EVENTS, file_pattern, position["file"], position["offset"])
        except pyodbc.Error:
            # The file it points to no longer exists (rolled over): read all
            # files again, keeping only events newer than the last one sent.
            since = datetime.fromisoformat(position["timestamp"])
            events = q.run_query(cursor, q.XE_EVENTS, file_pattern, None, None)
    else:
        events = q.run_query(cursor, q.XE_EVENTS, file_pattern, None, None)

    new_position = None
    # Several events share one file_offset (SQL Server writes them in blocks),
    # so the unique EventId adds the event's position inside its block. The
    # same block always returns its events in the same order, so the ID is
    # stable if the events are read again.
    block, position_in_block = None, 0
    for r in events:
        happened = r.timestamp_utc.replace(tzinfo=timezone.utc)
        if (r.file_name, r.file_offset) == block:
            position_in_block += 1
        else:
            block, position_in_block = (r.file_name, r.file_offset), 0
        new_position = {"file": r.file_name, "offset": r.file_offset, "timestamp": happened.isoformat()}
        if since is not None and happened <= since:
            continue
        event_id = f"{_file_name_only(r.file_name)}:{r.file_offset}:{position_in_block}"
        rows.append(parse_event(r.object_name, r.event_data, happened, computer, event_id))
    return rows, new_position


def parse_event(name: str, event_xml: str, happened: datetime, computer: str, event_id: str | None = None) -> dict:
    """One event's XML -> one SqlXEvents_CL row. Fields an event doesn't have stay null."""
    row = {
        "TimeGenerated": happened, "Computer": computer, "EventId": event_id, "EventName": name,
        "SessionId": None, "DatabaseName": None, "ClientApp": None, "LoginName": None,
        "ClientHost": None, "QueryHash": None, "DurationMs": None, "CpuMs": None,
        "LogicalReads": None, "ErrorNumber": None, "Severity": None, "Message": None,
        "SqlText": None,
        "EventXml": event_xml[:MAX_EVENT_XML],
        "EventXmlTruncated": len(event_xml) > MAX_EVENT_XML,
    }
    try:
        root = ET.fromstring(event_xml)
    except ET.ParseError:
        return row                                 # keep the raw XML; the fields stay null

    data = {d.get("name"): _value(d) for d in root.findall("data")}
    actions = {a.get("name"): _value(a) for a in root.findall("action")}

    row["SessionId"] = _int(actions.get("session_id"))
    row["DatabaseName"] = actions.get("database_name") or data.get("database_name")
    row["ClientApp"] = actions.get("client_app_name")
    row["LoginName"] = actions.get("server_principal_name")
    row["ClientHost"] = actions.get("client_hostname")
    row["QueryHash"] = _hex_hash(actions.get("query_hash"))

    # Durations and CPU are microseconds in Extended Events; milliseconds here.
    if data.get("duration") is not None:
        row["DurationMs"] = round(int(data["duration"]) / 1000.0, 1)
    if data.get("cpu_time") is not None:
        row["CpuMs"] = round(int(data["cpu_time"]) / 1000.0, 1)
    row["LogicalReads"] = _int(data.get("logical_reads"))

    if name == "error_reported":
        row["ErrorNumber"] = _int(data.get("error_number"))
        row["Severity"] = _int(data.get("severity"))
        row["Message"] = _cut(data.get("message"), MAX_MESSAGE)

    # Where each event keeps its SQL text: the statement for an RPC, the whole
    # batch for an ad-hoc batch, the sql_text action for an error. Deadlock
    # and blocked-process reports keep theirs inside EventXml.
    text = data.get("statement") or data.get("batch_text") or actions.get("sql_text")
    row["SqlText"] = _cut(text, MAX_SQL_TEXT)
    return row


def _file_name_only(path: str) -> str:
    """F:\\XE\\ShopLite_Diagnostics_0_133....xel -> ShopLite_Diagnostics_0_133....xel
    (the folder isn't part of the identity; both separators handled)."""
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def _value(element) -> str | None:
    """The text inside <value>. For the deadlock and blocked-process reports the
    value is an XML document itself; it's kept in EventXml, so None here."""
    value = element.find("value")
    if value is None:
        return None
    return value.text if len(value) == 0 else None


def _int(text):
    try:
        return None if text in (None, "") else int(text)
    except ValueError:
        return None


def _cut(text, limit):
    return None if text is None else text[:limit]


def _hex_hash(text):
    """query_hash arrives as an unsigned 64-bit number; Query Store shows it as
    0x + 16 hex digits. Same format here, so SqlXEvents_CL.QueryHash joins to
    SqlTopQueries_CL.QueryHash. 0 means "no hash" (e.g. the statement wasn't a query)."""
    number = _int(text)
    return None if not number else f"0x{number:016X}"
