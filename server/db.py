"""SQLite storage helpers and schema for the batch warehouse tool."""

from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    latest_seal_id TEXT,
    published_release_id INTEGER,
    is_sample INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS batch_files (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    filename TEXT NOT NULL,
    version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    content TEXT NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0,
    is_current INTEGER NOT NULL DEFAULT 1,
    uploaded_at TEXT NOT NULL,
    replaced_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_batch_files_current
    ON batch_files(batch_id, kind) WHERE is_current = 1;

CREATE TABLE IF NOT EXISTS candidate_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_file_id TEXT NOT NULL REFERENCES batch_files(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    data_json TEXT,
    raw_json TEXT,
    field_errors_json TEXT,
    parse_ok INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_candidate_rows_file
    ON candidate_rows(batch_file_id, line_no);

CREATE TABLE IF NOT EXISTS file_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_file_id TEXT NOT NULL REFERENCES batch_files(id) ON DELETE CASCADE,
    line_no INTEGER,
    field TEXT,
    rule TEXT NOT NULL,
    severity TEXT NOT NULL,
    value TEXT,
    message TEXT NOT NULL,
    related_json TEXT
);

CREATE TABLE IF NOT EXISTS seals (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    valid INTEGER NOT NULL,
    error_count INTEGER NOT NULL,
    warning_count INTEGER NOT NULL,
    issue_count INTEGER NOT NULL,
    content_summary TEXT NOT NULL,
    base_release_id INTEGER NOT NULL,
    UNIQUE(batch_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS seal_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seal_id TEXT NOT NULL REFERENCES seals(id) ON DELETE CASCADE,
    severity TEXT NOT NULL,
    rule TEXT NOT NULL,
    kind TEXT,
    filename TEXT,
    line_no INTEGER,
    field TEXT,
    value TEXT,
    message TEXT NOT NULL,
    related_json TEXT
);

CREATE TABLE IF NOT EXISTS releases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT,
    batch_label TEXT,
    seal_id TEXT,
    label TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'staged',
    created_at TEXT NOT NULL,
    activated_at TEXT,
    prev_release_id INTEGER,
    customer_count INTEGER NOT NULL DEFAULT 0,
    contract_count INTEGER NOT NULL DEFAULT 0,
    invoice_count INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_release_seal
    ON releases(batch_id, seal_id) WHERE seal_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS published_customers (
    release_id INTEGER NOT NULL,
    customer_id TEXT NOT NULL,
    name TEXT NOT NULL,
    region TEXT NOT NULL,
    contact_email TEXT NOT NULL,
    source_batch TEXT,
    source_file TEXT,
    source_line INTEGER
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pub_customers_pk
    ON published_customers(release_id, customer_id);

CREATE TABLE IF NOT EXISTS published_contracts (
    release_id INTEGER NOT NULL,
    contract_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    title TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    signed_on TEXT NOT NULL,
    source_batch TEXT,
    source_file TEXT,
    source_line INTEGER
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pub_contracts_pk
    ON published_contracts(release_id, contract_id);

CREATE TABLE IF NOT EXISTS published_invoices (
    release_id INTEGER NOT NULL,
    invoice_id TEXT NOT NULL,
    contract_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    issued_on TEXT NOT NULL,
    source_batch TEXT,
    source_file TEXT,
    source_line INTEGER
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pub_invoices_pk
    ON published_invoices(release_id, invoice_id);

CREATE TABLE IF NOT EXISTS publish_ops (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    fault TEXT,
    phase TEXT,
    release_id INTEGER,
    note TEXT
);

CREATE TABLE IF NOT EXISTS batch_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    event TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime(ISO_FORMAT)


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(db_path: str | Path) -> None:
    """Create the schema and the empty baseline release (release 0)."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'latest_release_id'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO releases (id, batch_id, batch_label, label, state, created_at,"
                    " activated_at, prev_release_id, customer_count, contract_count, invoice_count)"
                    " VALUES (0, NULL, NULL, '基线空版本', 'active', ?, ?, NULL, 0, 0, 0)",
                    (utc_now(), utc_now()),
                )
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('latest_release_id', '0')"
                )
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value)"
                    " VALUES ('schema_version', '1')"
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def latest_release_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'latest_release_id'"
    ).fetchone()
    return int(row["value"]) if row else 0
