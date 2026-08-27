"""Local SQLite store for Reporting API extracts.

Why a store at all: the Reporting API's rate limits are tight (AllData is 2
queries per 10 minutes), so re-querying per dashboard view is not an option.
Extract once, then read locally as often as you like.

Schema is generated from reporting_config.REPORTING_ENTITIES, so adding an
entity there is the only change needed. Upserts are keyed on each entity's
primary key, which makes re-extracting idempotent and lets bookmark deltas
merge instead of duplicating.

Uses stdlib sqlite3 - no new dependency, and nothing extra in the .exe.
"""

import os
import re
import sqlite3

from .reporting_config import REPORTING_ENTITIES, STORE_FILENAME
from .settings import _config_dir


def default_store_path():
    """Where the store lives by default (beside the settings file)."""
    return os.path.join(_config_dir(), STORE_FILENAME)


_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def normalise_key(key):
    """camelCase/PascalCase -> snake_case.

    VisitExtended and RfmVisit use camelCase while every other schema uses
    snake_case, so all incoming keys funnel through here.
    """
    if not key:
        return key
    if "_" in key:
        return key.lower()
    return _CAMEL_BOUNDARY.sub("_", key).lower()


def normalise_row(row):
    """Normalise every key in one row dict."""
    return {normalise_key(k): v for k, v in row.items()}


def _coerce(value):
    """Make a value safe to bind to a SQLite column."""
    if isinstance(value, bool):
        return 1 if value else 0
    if value is None or isinstance(value, (int, float, str, bytes)):
        return value
    # Nested structures (json_view payloads, stray objects) get stored as text.
    return str(value)


def _tables():
    """Yield (table_name, primary_key, columns) for every root and sub-entity."""
    for spec in REPORTING_ENTITIES.values():
        yield spec["table"], spec["primary_key"], spec["columns"]
        for sub in (spec.get("sub_entities") or {}).values():
            yield sub["table"], sub["primary_key"], sub["columns"]


class ReportStore:
    """Read/write access to the local extract database."""

    def __init__(self, path=None):
        self.path = path or default_store_path()
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.ensure_schema()

    def close(self):
        self.conn.close()

    # -- schema ----------------------------------------------------------

    def ensure_schema(self):
        """Create any missing tables. Safe to call every startup."""
        cur = self.conn.cursor()
        for table, pk, columns in _tables():
            cols = [f'"{name}" {sql_type}' for name, sql_type in columns.items()]
            if pk not in columns:
                cols.append(f'"{pk}" TEXT')
            # is_deleted supports reconcile(): bookmarks never report deletions,
            # so rows are soft-deleted rather than silently kept forever.
            cols.append('"is_deleted" INTEGER DEFAULT 0')
            cols.append(f'PRIMARY KEY ("{pk}")')
            cur.execute(
                f'CREATE TABLE IF NOT EXISTS "{table}" ({", ".join(cols)})')

        cur.execute("""
            CREATE TABLE IF NOT EXISTS bookmarks (
                endpoint TEXT NOT NULL,
                reporting_period TEXT NOT NULL,
                bookmark TEXT,
                updated_at TEXT,
                PRIMARY KEY (endpoint, reporting_period)
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT,
                entity TEXT,
                reporting_period TEXT,
                mode TEXT,
                rows INTEGER,
                date_range TEXT,
                status TEXT,
                notes TEXT
            )""")
        self.conn.commit()

    # -- writes ----------------------------------------------------------

    def upsert(self, table, primary_key, columns, rows):
        """Insert or update rows by primary key. Returns the number written.

        Unknown keys in a row are ignored (the API can return more than the
        registry declares); missing ones are left NULL. Rows with no primary
        key value are skipped rather than silently colliding on NULL.
        """
        known = list(columns.keys())
        if primary_key not in known:
            known.append(primary_key)
        placeholders = ", ".join("?" for _ in known)
        quoted = ", ".join(f'"{c}"' for c in known)
        updates = ", ".join(f'"{c}"=excluded."{c}"'
                            for c in known if c != primary_key)
        sql = (f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders}) '
               f'ON CONFLICT("{primary_key}") DO UPDATE SET {updates}'
               if updates else
               f'INSERT OR REPLACE INTO "{table}" ({quoted}) '
               f'VALUES ({placeholders})')

        cur = self.conn.cursor()
        written = 0
        for row in rows:
            norm = normalise_row(row)
            if norm.get(primary_key) in (None, ""):
                continue
            values = [_coerce(norm.get(c)) for c in known]
            cur.execute(sql, values)
            written += 1
        self.conn.commit()
        return written

    def upsert_entity(self, entity, rows):
        """Upsert a root entity's rows plus any nested sub-entity rows.

        Returns {table: rows_written}. Sub-entity rows arrive nested inside each
        root row (that is what `entities` expansion returns), and are linked
        back via the sub-entity's parent_key.
        """
        spec = REPORTING_ENTITIES[entity]
        written = {}
        normalised = [normalise_row(r) for r in rows]

        written[spec["table"]] = self.upsert(
            spec["table"], spec["primary_key"], spec["columns"], normalised)

        for api_name, sub in (spec.get("sub_entities") or {}).items():
            key = normalise_key(api_name)
            child_rows = []
            for parent in normalised:
                nested = parent.get(key) or parent.get(api_name)
                if not isinstance(nested, list):
                    continue
                parent_value = parent.get(spec["primary_key"])
                for child in nested:
                    child_norm = normalise_row(child)
                    child_norm.setdefault(sub["parent_key"], parent_value)
                    child_rows.append(child_norm)
            if child_rows:
                written[sub["table"]] = self.upsert(
                    sub["table"], sub["primary_key"], sub["columns"], child_rows)
        return written

    def reconcile(self, table, primary_key, live_keys):
        """Soft-delete rows whose key is absent from a full key sweep.

        Bookmarks only ever report added data, so deletions have to be found by
        comparing against a full listing.
        """
        cur = self.conn.cursor()
        cur.execute(f'SELECT "{primary_key}" FROM "{table}"')
        existing = {r[0] for r in cur.fetchall()}
        missing = existing - set(live_keys)
        for key in missing:
            cur.execute(
                f'UPDATE "{table}" SET is_deleted=1 WHERE "{primary_key}"=?',
                (key,))
        self.conn.commit()
        return len(missing)

    # -- bookmarks -------------------------------------------------------

    def get_bookmark(self, endpoint, reporting_period):
        """A bookmark is only valid for the period it was issued against."""
        cur = self.conn.cursor()
        cur.execute("SELECT bookmark FROM bookmarks WHERE endpoint=? "
                    "AND reporting_period=?",
                    (endpoint, reporting_period or ""))
        row = cur.fetchone()
        return row[0] if row else None

    def set_bookmark(self, endpoint, reporting_period, bookmark, when=""):
        """Record a bookmark. Call only AFTER the rows are committed."""
        if not bookmark:
            return
        self.conn.execute(
            "INSERT INTO bookmarks (endpoint, reporting_period, bookmark, "
            "updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(endpoint, reporting_period) DO UPDATE SET "
            "bookmark=excluded.bookmark, updated_at=excluded.updated_at",
            (endpoint, reporting_period or "", str(bookmark), when))
        self.conn.commit()

    # -- run history -----------------------------------------------------

    def record_run(self, entity, reporting_period, mode, rows, date_range,
                   status, notes="", started_at=""):
        self.conn.execute(
            "INSERT INTO runs (started_at, entity, reporting_period, mode, "
            "rows, date_range, status, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (started_at, entity, reporting_period or "", mode, rows,
             date_range, status, notes))
        self.conn.commit()

    def last_runs(self):
        """Most recent run per entity: {entity: sqlite3.Row}."""
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM runs ORDER BY run_id DESC")
        latest = {}
        for row in cur.fetchall():
            latest.setdefault(row["entity"], row)
        return latest

    # -- reads -----------------------------------------------------------

    def counts(self):
        """{table: live_row_count} for every table in the registry."""
        cur = self.conn.cursor()
        result = {}
        for table, _pk, _cols in _tables():
            cur.execute(f'SELECT COUNT(*) FROM "{table}" WHERE is_deleted=0')
            result[table] = cur.fetchone()[0]
        return result

    def query(self, sql, params=()):
        """Run a read-only query and return a list of sqlite3.Row."""
        cur = self.conn.cursor()
        cur.execute(sql, params)
        return cur.fetchall()

    def scalar(self, sql, params=(), default=0):
        """First column of the first row, or default when NULL/absent."""
        rows = self.query(sql, params)
        if not rows or rows[0][0] is None:
            return default
        return rows[0][0]
