"""The collector's local buffer: records wait here, on disk, until their
destination has accepted them.

Every cycle's records are written here FIRST, then sent. So when Log
Analytics or ADX is unreachable (network blip, Azure incident, expired
secret), nothing is lost: the files stay, and the next cycles retry them,
oldest first. The collector's watermarks only move forward once records are
in a file here (main.py), which is what makes delivery "at least once".

Layout (on the data disk, F:\\collector\\buffer on the VM):
    <root>/<destination>/<UTC time>_<table>_<record count>.jsonl
One file per table per cycle per destination. JSON Lines: one record per
line, so a file is easy to read by eye and to send as-is. The record count
in the name lets the heartbeat report how much is waiting without opening
every file.

Limits, so a long outage can't fill the disk or send data nobody can use:
    max_age   files older than this are deleted unsent. Log Analytics doesn't
              accept records much older than two days, so its buffer keeps 47
              hours; ADX has no such rule and keeps 7 days.
    max_bytes when a destination's folder is larger than this, the oldest
              files are deleted first.
Every deletion of unsent data is logged with how many records it lost.
"""

import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("collector.buffer")

FILE_NAME = re.compile(r"^(?P<stamp>\d{8}T\d{6}\d{6})_(?P<table>[A-Za-z0-9_]+)_(?P<count>\d+)\.jsonl$")


def to_json(record: dict) -> str:
    """One record as a JSON line. Datetimes become ISO 8601 UTC text with a Z,
    which Log Analytics and ADX both read as datetime."""
    def convert(value):
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        if isinstance(value, date):
            return value.isoformat()
        raise TypeError(f"Can't write {type(value).__name__} as JSON")
    return json.dumps(record, default=convert, ensure_ascii=False)


class Buffer:
    def __init__(self, root: Path, destination: str, max_age: timedelta, max_bytes: int):
        self.folder = root / destination
        self.folder.mkdir(parents=True, exist_ok=True)
        self.destination = destination
        self.max_age = max_age
        self.max_bytes = max_bytes

    def store(self, table: str, records: list[dict]) -> None:
        """Write records to a new file. Written to a temporary name and renamed,
        so a crash mid-write never leaves a half file that looks complete."""
        if not records:
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        final = self.folder / f"{stamp}_{table}_{len(records)}.jsonl"
        temp = final.with_suffix(".tmp")
        with open(temp, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(to_json(record) + "\n")
        os.replace(temp, final)

    def pending(self) -> list[tuple[Path, str, int]]:
        """Waiting files, oldest first: (path, table, record count)."""
        files = []
        for path in self.folder.iterdir():
            match = FILE_NAME.match(path.name)
            if match:
                files.append((path, match["table"], int(match["count"])))
        return sorted(files, key=lambda f: f[0].name)   # the name starts with the time

    @staticmethod
    def read(path: Path) -> list[dict]:
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    @staticmethod
    def remove(path: Path) -> None:
        path.unlink(missing_ok=True)

    def record_count(self) -> int:
        return sum(count for _, _, count in self.pending())

    def enforce_limits(self) -> None:
        """Delete what's too old, then the oldest until the folder fits max_bytes."""
        cutoff = datetime.now(timezone.utc) - self.max_age
        files = self.pending()
        kept = []
        for path, table, count in files:
            written = datetime.strptime(path.name[:21], "%Y%m%dT%H%M%S%f").replace(tzinfo=timezone.utc)
            if written < cutoff:
                log.warning("%s: dropped %d unsent %s records older than %s", self.destination, count, table, self.max_age)
                self.remove(path)
            else:
                kept.append((path, table, count))
        total = sum(path.stat().st_size for path, _, _ in kept)
        for path, table, count in kept:
            if total <= self.max_bytes:
                break
            total -= path.stat().st_size
            log.warning("%s: buffer over %d MB, dropped %d unsent %s records", self.destination,
                        self.max_bytes // 1_048_576, count, table)
            self.remove(path)
        # Leftovers from a crash mid-write (never complete, never sent).
        for temp in self.folder.glob("*.tmp"):
            temp.unlink(missing_ok=True)
