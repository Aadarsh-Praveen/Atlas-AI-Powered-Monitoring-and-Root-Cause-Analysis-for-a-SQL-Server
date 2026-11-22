"""Where the collector's records go: Log Analytics, ADX, or the screen.

Each sender has send(table, records), which either succeeds or raises. The
service (main.py) calls it for each buffered file, oldest first, and deletes
the file only on success. A failure stops that destination's flush for this
cycle and starts a back-off (1 minute, doubling, at most 15), so an outage
isn't hammered with retries; the other destination carries on independently.

Log Analytics: the Logs Ingestion API.
    Records go to a data collection ENDPOINT (DCE), addressed to a data
    collection RULE (DCR, by its immutable ID) and a stream
    ("Custom-<table>"). The DCR maps the stream onto the custom table. Sign-in:
    the VM's managed identity (Azure gives the VM an identity; the DCR grants
    it "Monitoring Metrics Publisher"), so no secret exists on the VM for this.
    Which DCR a table belongs to comes from observability/schema.yml.

ADX: queued ingestion with the app registration.
    The free cluster doesn't support managed identities (checked when B1 was
    written), so the collector signs in as an Entra app registration whose
    secret lives only in the collector's locked .env. Queued ingestion hands
    the data to ADX, which loads it within minutes (it batches small
    ingestions). "Accepted" here means queued; a load that fails later shows
    up in ADX's ".show ingestion failures", and the heartbeat's AdxOk stays
    true for it. The mapping "collector_json" (observability/adx/) maps each
    JSON property to its column.

Print: for local checks (--print). Shows each table's row count and first
row; sends nothing.
"""

import io
import json
import logging
import time

from buffer import to_json

log = logging.getLogger("collector.senders")


class BackOff:
    """After a failure, wait before trying the destination again: 60 s, 120 s,
    240 s, ... at most 900 s. A success resets it."""

    def __init__(self, first: float = 60, longest: float = 900, clock=time.monotonic):
        self.first, self.longest, self.clock = first, longest, clock
        self.delay = 0.0
        self.next_try = 0.0

    def ready(self) -> bool:
        return self.clock() >= self.next_try

    def failed(self) -> None:
        self.delay = self.first if self.delay == 0 else min(self.delay * 2, self.longest)
        self.next_try = self.clock() + self.delay

    def succeeded(self) -> None:
        self.delay = 0.0
        self.next_try = 0.0


class LogAnalyticsSender:
    name = "loganalytics"

    def __init__(self, endpoint: str, rule_ids: dict[str, str], table_rules: dict[str, str],
                 managed_identity_client_id: str | None = None):
        """endpoint: the DCE's logs ingestion URL. rule_ids: DCR name -> immutable ID
        (dcr-...). table_rules: table -> DCR name, from schema.yml."""
        from azure.identity import ManagedIdentityCredential
        from azure.monitor.ingestion import LogsIngestionClient
        # System-assigned identity by default; a client ID selects a user-assigned one.
        credential = (ManagedIdentityCredential(client_id=managed_identity_client_id)
                      if managed_identity_client_id else ManagedIdentityCredential())
        self.client = LogsIngestionClient(endpoint=endpoint, credential=credential, logging_enable=False)
        self.rule_ids = rule_ids
        self.table_rules = table_rules

    def send(self, table: str, records: list[dict]) -> None:
        rule = self.table_rules.get(table)
        if rule is None or rule not in self.rule_ids:
            raise ValueError(f"No data collection rule configured for {table}")
        # The client splits large uploads into the API's 1 MB chunks itself, and
        # raises if any chunk fails (the whole file is then retried: duplicates
        # are possible, which is why event tables have a unique_id).
        documents = [json.loads(to_json(r)) for r in records]   # datetimes as ISO text
        self.client.upload(rule_id=self.rule_ids[rule], stream_name=f"Custom-{table}", logs=documents)


class AdxSender:
    name = "adx"

    def __init__(self, ingest_uri: str, database: str, tenant_id: str, app_id: str, app_secret: str):
        """ingest_uri: the cluster's INGESTION URI (https://ingest-<cluster>...)."""
        from azure.kusto.data import KustoConnectionStringBuilder
        from azure.kusto.data.data_format import DataFormat, IngestionMappingKind
        from azure.kusto.ingest import IngestionProperties, QueuedIngestClient
        connection = KustoConnectionStringBuilder.with_aad_application_key_authentication(
            ingest_uri, app_id, app_secret, tenant_id)
        self.client = QueuedIngestClient(connection)
        self.database = database
        self._properties = lambda table: IngestionProperties(
            database=database, table=table, data_format=DataFormat.MULTIJSON,
            ingestion_mapping_reference="collector_json", ingestion_mapping_kind=IngestionMappingKind.JSON)

    def send(self, table: str, records: list[dict]) -> None:
        from azure.kusto.ingest import StreamDescriptor
        payload = "\n".join(to_json(r) for r in records).encode("utf-8")
        self.client.ingest_from_stream(StreamDescriptor(io.BytesIO(payload)), ingestion_properties=self._properties(table))


class PrintSender:
    name = "print"

    def send(self, table: str, records: list[dict]) -> None:
        print(f"\n== {table}: {len(records)} row(s)")
        print("   first: " + to_json(records[0])[:600])
