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

import hashlib
import os
import re
import sqlite3
import threading
from contextlib import contextmanager

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


def synthetic_key(row, fields):
    """Deterministic id for a row the API gives no key of its own.

    Comments and emails on an activity have no documented unique field, so
    keying them on activity_id alone would make every comment overwrite the
    last. Hashing the identifying fields keeps one row per distinct comment
    while staying idempotent: the same content always yields the same key, so
    re-extracting does not duplicate.
    """
    # A field that is absent and a field that is present-but-null are the same
    # thing here. Without this, str(None) -> "None" hashes differently from the
    # missing case and the "re-extracting does not duplicate" guarantee breaks,
    # which matters because the API's envelope is inconsistent about nulls.
    parts = ["" if row.get(f) is None else str(row.get(f)) for f in fields]
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()


def _child_rows(normalised, spec, api_name, sub):
    """Pull one sub-entity's nested rows out of already-normalised parents."""
    key = normalise_key(api_name)
    key_fields = sub.get("synthetic_key")
    rows = []
    for parent in normalised:
        nested = parent.get(key) or parent.get(api_name)
        if not isinstance(nested, list):
            continue
        parent_value = parent.get(spec["primary_key"])
        for child in nested:
            child_norm = normalise_row(child)
            child_norm.setdefault(sub["parent_key"], parent_value)
            if key_fields and not child_norm.get(sub["primary_key"]):
                child_norm[sub["primary_key"]] = synthetic_key(
                    child_norm, key_fields)
            rows.append(child_norm)
    return rows


def _tables():
    """Yield (table_name, primary_key, columns) for every root and sub-entity."""
    for spec in REPORTING_ENTITIES.values():
        yield spec["table"], spec["primary_key"], spec["columns"]
        for sub in (spec.get("sub_entities") or {}).values():
            yield sub["table"], sub["primary_key"], sub["columns"]


class ReportStore:
    """Read/write access to the local extract database.

    One instance is safe to use from several threads, which the GUI needs: the
    store is created on the connect worker, extracts run on other workers, and
    the store/dashboard labels render on the Tk main thread. A sqlite3
    connection is bound to its creating thread by default, so sharing one
    raised "SQLite objects created in a thread can only be used in that same
    thread".

    How that is handled, and the limits of it:

    * Writes go through one connection, serialised on a re-entrant lock. It has
      to be re-entrant because upsert() is called while the entity-level lock
      is already held.
    * Reads open their own short-lived connection instead of taking that lock,
      so a label refresh on the main thread cannot freeze the UI behind a bulk
      upsert on a worker. (An in-memory store cannot be reopened, so there
      reads fall back to the shared connection under the lock.)
    * WAL mode lets those readers run while a write is in progress, and
      busy_timeout stops a concurrent writer failing instantly.

    The lock guards THIS INSTANCE, not the file: two ReportStore objects, or
    two copies of the app, are separate connections and rely on SQLite's own
    locking - which is what WAL and busy_timeout above are for.
    """

    # Wait this long for another writer before raising "database is locked".
    BUSY_TIMEOUT_MS = 10_000

    def __init__(self, path=None):
        self.path = path or default_store_path()
        self._memory = self.path == ":memory:"
        if not self._memory:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self.conn = self._connect()
        self.ensure_schema()

    def _connect(self):
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={self.BUSY_TIMEOUT_MS}")
        if not self._memory:
            # WAL: readers do not block on the writer, which is what keeps the
            # main thread responsive during an extract.
            conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextmanager
    def _reading(self):
        """Yield a connection for a read.

        File-backed: a private short-lived connection, so reads never wait on
        the write lock. In-memory: the shared connection, under the lock, since
        ":memory:" cannot be reopened.
        """
        if self._memory:
            with self._lock:
                yield self.conn
            return
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def close(self):
        with self._lock:
            self.conn.close()

    # -- schema ----------------------------------------------------------

    def ensure_schema(self):
        """Create any missing tables. Safe to call every startup."""
        with self._lock:
            self._create_tables()

    def _create_tables(self):
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

        Commits on success, rolls back on failure. Use _write_rows directly to
        batch several tables into one transaction, or to see the skipped count.
        """
        with self._lock:
            try:
                written, _skipped = self._write_rows(
                    table, primary_key, columns, rows)
            except Exception:
                self.conn.rollback()
                raise
            self.conn.commit()
            return written

    def _write_rows(self, table, primary_key, columns, rows):
        """Upsert rows without committing. Returns (written, skipped).

        Unknown keys in a row are ignored (the API can return more than the
        registry declares); missing ones are left NULL.

        Rows with no primary key value are skipped rather than colliding on
        NULL - and COUNTED, because that is the signature of the registry
        naming a key field this instance does not actually use. Several
        sub-entity keys are unverified guesses from a spec with known defects,
        so a silent zero here would look identical to "there was no data".
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
        skipped = 0
        for row in rows:
            norm = normalise_row(row)
            if norm.get(primary_key) in (None, ""):
                skipped += 1
                continue
            cur.execute(sql, [_coerce(norm.get(c)) for c in known])
            written += 1
        return written, skipped

    def upsert_entity(self, entity, rows):
        """Upsert a root entity's rows plus any nested sub-entity rows.

        Returns (written, skipped), both {table: count}. `skipped` only carries
        tables that actually lost rows, and means the registry's primary key for
        that table is not a field this instance returns - see _write_rows.

        Sub-entity rows arrive nested inside each root row (that is what
        `entities` expansion returns) and are linked back via parent_key.

        The root and all of its children go in ONE transaction: a failure part
        way through a sub-entity would otherwise leave, say, activities stored
        with no order lines.
        """
        spec = REPORTING_ENTITIES[entity]
        normalised = [normalise_row(r) for r in rows]
        written = {}
        skipped = {}

        def record(table, result):
            count, missed = result
            written[table] = count
            if missed:
                skipped[table] = missed

        with self._lock:
            try:
                record(spec["table"], self._write_rows(
                    spec["table"], spec["primary_key"], spec["columns"],
                    normalised))
                for api_name, sub in (spec.get("sub_entities") or {}).items():
                    child_rows = _child_rows(normalised, spec, api_name, sub)
                    if child_rows:
                        record(sub["table"], self._write_rows(
                            sub["table"], sub["primary_key"], sub["columns"],
                            child_rows))
            except Exception:
                self.conn.rollback()
                raise
            self.conn.commit()
        return written, skipped

    def reconcile(self, table, primary_key, live_keys):
        """Soft-delete rows whose key is absent from a full key sweep.

        Bookmarks only ever report added data, so deletions have to be found by
        comparing against a full listing.
        """
        with self._lock:
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
        with self._reading() as conn:
            row = conn.execute(
                "SELECT bookmark FROM bookmarks WHERE endpoint=? "
                "AND reporting_period=?",
                (endpoint, reporting_period or "")).fetchone()
            return row[0] if row else None

    def set_bookmark(self, endpoint, reporting_period, bookmark, when=""):
        """Record a bookmark. Call only AFTER the rows are committed."""
        if not bookmark:
            return
        with self._lock:
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
        with self._lock:
            self.conn.execute(
                "INSERT INTO runs (started_at, entity, reporting_period, mode, "
                "rows, date_range, status, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (started_at, entity, reporting_period or "", mode, rows,
                 date_range, status, notes))
            self.conn.commit()

    def last_runs(self):
        """Most recent run per entity: {entity: sqlite3.Row}."""
        with self._reading() as conn:
            latest = {}
            for row in conn.execute("SELECT * FROM runs ORDER BY run_id DESC"):
                latest.setdefault(row["entity"], row)
            return latest

    # -- reads -----------------------------------------------------------

    def counts(self):
        """{table: live_row_count} for every table in the registry."""
        with self._reading() as conn:
            return {
                table: conn.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE is_deleted=0'
                ).fetchone()[0]
                for table, _pk, _cols in _tables()
            }

    def query(self, sql, params=()):
        """Run a READ query and return a list of sqlite3.Row.

        Reads only - this may run on a private connection, so a statement that
        writes would not be committed. Use the upsert/bookmark/run methods to
        change anything.
        """
        with self._reading() as conn:
            return conn.execute(sql, params).fetchall()

    def scalar(self, sql, params=(), default=0):
        """First column of the first row, or default when NULL/absent."""
        rows = self.query(sql, params)
        if not rows or rows[0][0] is None:
            return default
        return rows[0][0]
