"""Check the vendor database against the data contract
(knowledge/contracts/vendor-supplier-feed.yml) BEFORE anything is loaded.

The ETL (run_etl.py) calls validate() inside its read-only vendor
transaction. Every difference becomes one problem in plain words, naming the
table, column, what the contract expects, and what was found, for example:

    supply.prices.unit_cost: type expected 'numeric', found 'double precision'
    supply.prices: newest updated_at is 41.2 hours old (contract maximum 36)

Any problem stops the load: loading data the ETL doesn't understand would be
worse than loading nothing. The problems end up in ops.etl_runs.error_message,
which is the evidence Atlas reads when the vendor changes something.

Checks, in order:
  1. Schema: every contract column exists with the right type, nullability,
     length, precision, and scale (from information_schema.columns). If
     anything is wrong here, the data checks are skipped: they would only
     fail on the same missing or changed columns.
  2. Volume: minimum row counts per table.
  3. Keys: no duplicate primary or unique keys.
  4. Values: SKU pattern, minimums (cost >= 0), allowed values (USD), and
     references (every catalog item's supplier exists).
  5. Complete join: every catalog item has a price row and a stock row.
  6. Freshness: the newest price change is recent enough.

Table and column names come from the contract file (in the repo, reviewed
like code), and they still go through psycopg's sql.Identifier, which quotes
them properly. Values always go as query parameters.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml
from psycopg import sql

REQUIRED_SECTIONS = ("contract", "version", "source", "tables", "volume", "freshness", "target")


@dataclass
class ValidationResult:
    problems: list[str] = field(default_factory=list)
    row_counts: dict[str, int] = field(default_factory=dict)
    newest_change_at: datetime | None = None    # freshness evidence, kept even on success

    @property
    def ok(self) -> bool:
        return not self.problems


def load_contract(path: Path) -> dict:
    """Read the contract YAML. safe_load: a YAML file can't run code."""
    with open(path, encoding="utf-8") as handle:
        contract = yaml.safe_load(handle)
    if not isinstance(contract, dict):
        raise ValueError(f"{path}: not a contract file")
    missing = [name for name in REQUIRED_SECTIONS if name not in contract]
    if missing:
        raise ValueError(f"{path}: the contract has no {', '.join(missing)} section")
    contract["version"] = str(contract["version"])
    return contract


def validate(conn, contract: dict) -> ValidationResult:
    """Run every check on an open vendor connection. Read-only."""
    result = ValidationResult()
    with conn.cursor() as cur:
        check_schema(cur, contract, result)
        if result.problems:
            return result
        check_volume(cur, contract, result)
        check_keys(cur, contract, result)
        check_values(cur, contract, result)
        check_complete_join(cur, contract, result)
        check_freshness(cur, contract, result)
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def table_ref(contract: dict, table: str) -> sql.Identifier:
    """"supply"."prices": schema-qualified and quoted."""
    return sql.Identifier(contract["source"]["schema"], table)


def name(contract: dict, table: str, column: str | None = None) -> str:
    """supply.prices.unit_cost, for messages."""
    parts = [contract["source"]["schema"], table] + ([column] if column else [])
    return ".".join(parts)


def scalar(cur, query, params=()):
    cur.execute(query, params)
    return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# 1. Schema
# ---------------------------------------------------------------------------
COLUMNS_QUERY = """
    SELECT table_name, column_name, data_type, is_nullable,
           character_maximum_length, numeric_precision, numeric_scale
    FROM information_schema.columns
    WHERE table_schema = %s
"""


def check_schema(cur, contract: dict, result: ValidationResult) -> None:
    # information_schema only lists tables this login can read, so a table
    # the vendor stopped granting looks the same as a missing table.
    cur.execute(COLUMNS_QUERY, (contract["source"]["schema"],))
    found: dict[str, dict[str, dict]] = {}
    for table, column, data_type, is_nullable, max_length, precision, scale in cur.fetchall():
        found.setdefault(table, {})[column] = {
            "type": data_type,
            "nullable": is_nullable == "YES",
            "max_length": max_length,
            "precision": precision,
            "scale": scale,
        }

    for table, spec in contract["tables"].items():
        if table not in found:
            result.problems.append(f"{name(contract, table)}: table missing, or not readable by this login")
            continue
        for column, rules in spec["columns"].items():
            where = name(contract, table, column)
            actual = found[table].get(column)
            if actual is None:
                result.problems.append(f"{where}: column missing (renamed or removed?)")
                continue
            if actual["type"] != rules["type"]:
                result.problems.append(
                    f"{where}: type expected '{rules['type']}', found '{actual['type']}'")
            if actual["nullable"] != rules["nullable"]:
                expected = "nullable" if rules["nullable"] else "NOT NULL"
                found_as = "nullable" if actual["nullable"] else "NOT NULL"
                result.problems.append(f"{where}: expected {expected}, found {found_as}")
            for key in ("max_length", "precision", "scale"):
                if key in rules and actual[key] != rules[key]:
                    result.problems.append(f"{where}: {key} expected {rules[key]}, found {actual[key]}")

        if contract.get("extra_columns") != "allowed":
            extras = sorted(set(found[table]) - set(spec["columns"]))
            if extras:
                result.problems.append(f"{name(contract, table)}: unexpected columns {', '.join(extras)}")


# ---------------------------------------------------------------------------
# 2. Volume
# ---------------------------------------------------------------------------
def check_volume(cur, contract: dict, result: ValidationResult) -> None:
    minimums = contract["volume"].get("min_rows", {})
    for table in contract["tables"]:
        count = scalar(cur, sql.SQL("SELECT count(*) FROM {}").format(table_ref(contract, table)))
        result.row_counts[table] = count
        minimum = minimums.get(table)
        if minimum is not None and count < minimum:
            result.problems.append(
                f"{name(contract, table)}: {count} rows, fewer than the contract minimum of {minimum}")


# ---------------------------------------------------------------------------
# 3. Keys: the vendor's constraints should guarantee these, but the contract
#    is about the DATA ShopLite receives, so it checks the data itself.
# ---------------------------------------------------------------------------
def check_keys(cur, contract: dict, result: ValidationResult) -> None:
    for table, spec in contract["tables"].items():
        for key in [spec["primary_key"]] + spec.get("unique", []):
            query = sql.SQL(
                "SELECT count(*) FROM (SELECT 1 FROM {table} GROUP BY {columns} HAVING count(*) > 1) AS d"
            ).format(table=table_ref(contract, table),
                     columns=sql.SQL(", ").join(map(sql.Identifier, key)))
            duplicates = scalar(cur, query)
            if duplicates:
                result.problems.append(
                    f"{name(contract, table)}: {duplicates} duplicate values of ({', '.join(key)})")


# ---------------------------------------------------------------------------
# 4. Values
# ---------------------------------------------------------------------------
def check_values(cur, contract: dict, result: ValidationResult) -> None:
    for table, spec in contract["tables"].items():
        t = table_ref(contract, table)
        for column, rules in spec["columns"].items():
            c = sql.Identifier(column)
            where = name(contract, table, column)

            if "pattern" in rules:
                # !~ is "doesn't match the regular expression"; min() shows one example.
                cur.execute(sql.SQL("SELECT count(*), min({c}::text) FROM {t} WHERE {c}::text !~ %s")
                            .format(c=c, t=t), (rules["pattern"],))
                bad, example = cur.fetchone()
                if bad:
                    result.problems.append(
                        f"{where}: {bad} values don't match {rules['pattern']} (e.g. '{example}')")

            if "min" in rules:
                bad = scalar(cur, sql.SQL("SELECT count(*) FROM {t} WHERE {c} < %s").format(c=c, t=t),
                             (rules["min"],))
                if bad:
                    result.problems.append(f"{where}: {bad} values below {rules['min']}")

            if "allowed" in rules:
                cur.execute(sql.SQL("SELECT count(*), min({c}::text) FROM {t} WHERE {c}::text <> ALL(%s)")
                            .format(c=c, t=t), ([str(v) for v in rules["allowed"]],))
                bad, example = cur.fetchone()
                if bad:
                    result.problems.append(
                        f"{where}: {bad} values not in {rules['allowed']} (e.g. '{example}')")

            if "references" in rules:
                ref_table, ref_column = rules["references"].split(".")
                query = sql.SQL(
                    "SELECT count(*) FROM {t} AS x "
                    "WHERE NOT EXISTS (SELECT 1 FROM {rt} AS r WHERE r.{rc} = x.{c})"
                ).format(t=t, c=c, rt=table_ref(contract, ref_table), rc=sql.Identifier(ref_column))
                orphans = scalar(cur, query)
                if orphans:
                    result.problems.append(
                        f"{where}: {orphans} rows point to a missing {name(contract, ref_table, ref_column)}")


# ---------------------------------------------------------------------------
# 5. Complete join: each catalog item has its price and stock rows. (Exactly
#    one each is already guaranteed by the key check on prices and stock.)
# ---------------------------------------------------------------------------
def check_complete_join(cur, contract: dict, result: ValidationResult) -> None:
    tables = contract["volume"].get("complete_join", [])
    if not tables:
        return
    base, *others = tables
    key = sql.Identifier(contract["tables"][base]["primary_key"][0])
    for other in others:
        query = sql.SQL(
            "SELECT count(*) FROM {b} AS b WHERE NOT EXISTS (SELECT 1 FROM {o} AS o WHERE o.{k} = b.{k})"
        ).format(b=table_ref(contract, base), o=table_ref(contract, other), k=key)
        missing = scalar(cur, query)
        if missing:
            result.problems.append(
                f"{name(contract, base)}: {missing} rows have no matching row in {name(contract, other)}")


# ---------------------------------------------------------------------------
# 6. Freshness
# ---------------------------------------------------------------------------
def check_freshness(cur, contract: dict, result: ValidationResult) -> None:
    rule = contract["freshness"]
    # Both times come from the vendor's clock, so a skewed clock on the ETL
    # host can't make a fresh feed look stale (or the other way round).
    cur.execute(sql.SQL("SELECT max({c}), now() FROM {t}")
                .format(c=sql.Identifier(rule["column"]), t=table_ref(contract, rule["table"])))
    newest, now = cur.fetchone()
    where = name(contract, rule["table"])
    if newest is None:
        result.problems.append(f"{where}: no {rule['column']} values, so freshness can't be shown")
        return
    result.newest_change_at = newest
    age_hours = (now - newest).total_seconds() / 3600
    if age_hours > rule["max_age_hours"]:
        result.problems.append(
            f"{where}: newest {rule['column']} is {newest.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC, "
            f"{age_hours:.1f} hours old (contract maximum {rule['max_age_hours']})")
