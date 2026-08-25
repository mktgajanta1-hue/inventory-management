"""
MediaTrack — database layer (PostgreSQL).

Schema philosophy (ad-tech standard):
  clients    → who advertises
  screens    → the physical DOOH inventory (loop of N slots, each slot_seconds long)
  campaigns  → a client's creative/commercial container
  slots      → the actual airtime reservation: campaign X occupies position N
               on screen Y from start_date to end_date. Availability is always
               derived from slots, never stored.

Connection details come from DATABASE_URL, e.g.
    postgresql://user:pass@host:5432/mediatrack

This module deliberately keeps the old sqlite3-style call surface
(`?` placeholders, `cursor.lastrowid`, rows indexable by name *and* position)
so the rest of the application did not need to be rewritten. The translation
to PostgreSQL happens here and nowhere else.
"""

import logging
import os
import re
import time
from urllib.parse import urlparse

import psycopg
from psycopg_pool import ConnectionPool

log = logging.getLogger("mediatrack.db")


# ---------------------------------------------------------------- config

def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Example:\n"
            "  export DATABASE_URL=postgresql://mediatrack:mediatrack@localhost:5432/mediatrack"
        )
    # Render/Heroku hand out postgres:// — psycopg wants postgresql://
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    # Managed Postgres requires TLS; add it unless the caller said otherwise.
    if "sslmode=" not in url:
        host = (urlparse(url).hostname or "").lower()
        if host not in ("localhost", "127.0.0.1", "db", "postgres", ""):
            url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


DB_URL = os.environ.get("DATABASE_URL", "")   # kept for logging/health only


# ---------------------------------------------------------------- rows

class Row(dict):
    """Behaves like sqlite3.Row: r["col"] and r[0] both work, dict(r) works."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return dict.__getitem__(self, key)

    def keys(self):
        return list(dict.keys(self))


def _row_factory(cursor):
    cols = [c.name for c in (cursor.description or [])]

    def make(values):
        return Row(zip(cols, values))

    return make


# ---------------------------------------------------------------- SQL shim

_PLACEHOLDER = re.compile(r"\?")


def _translate(sql: str) -> str:
    """`?` → `%s`. (No SQL in this project contains a literal '?' or '%'.)"""
    return _PLACEHOLDER.sub("%s", sql)


def _needs_returning(sql: str) -> bool:
    s = sql.strip().lstrip("(").upper()
    return s.startswith("INSERT") and " RETURNING " not in s


class Cursor:
    """sqlite3.Cursor-compatible wrapper (execute/executemany/fetch*/lastrowid)."""

    def __init__(self, cur):
        self._cur = cur
        self.lastrowid = None

    def execute(self, sql, params=()):
        sql = _translate(sql)
        appended = False
        if _needs_returning(sql):
            sql = sql.rstrip().rstrip(";") + " RETURNING id"
            appended = True
        self._cur.execute(sql, tuple(params) if params else None)
        if appended:
            row = self._cur.fetchone()
            self.lastrowid = row["id"] if row else None
        return self

    def executemany(self, sql, seq):
        self._cur.executemany(_translate(sql), [tuple(p) for p in seq])
        return self

    def executescript(self, script):
        self._cur.execute(script)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur.fetchall())

    @property
    def rowcount(self):
        return self._cur.rowcount

    def close(self):
        self._cur.close()


class Connection:
    """
    sqlite3.Connection-compatible wrapper around a pooled psycopg connection.

    Used exactly like before:

        with get_conn() as conn:
            conn.execute("INSERT ... VALUES (?)", (x,))
            conn.commit()

    On exit the transaction is committed (or rolled back if an exception is in
    flight — same as sqlite3) and the connection goes back to the pool.
    """

    def __init__(self, pool, conn):
        self._pool = pool
        self._conn = conn
        self._released = False

    # -- sqlite-style convenience: conn.execute(...) returns a cursor
    def execute(self, sql, params=()):
        return Cursor(self._conn.cursor()).execute(sql, params)

    def executemany(self, sql, seq):
        return Cursor(self._conn.cursor()).executemany(sql, seq)

    def executescript(self, script):
        return Cursor(self._conn.cursor()).executescript(script)

    def cursor(self):
        return Cursor(self._conn.cursor())

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def _release(self):
        if not self._released:
            self._released = True
            self._pool.putconn(self._conn)

    def close(self):
        self._release()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._release()
        return False


# ---------------------------------------------------------------- pool

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=database_url(),
            min_size=int(os.environ.get("DB_POOL_MIN", "1")),
            max_size=int(os.environ.get("DB_POOL_MAX", "10")),
            kwargs={"row_factory": _row_factory},
            # Fail fast rather than hanging a request (and the health check)
            # for minutes when the database is unreachable.
            timeout=float(os.environ.get("DB_POOL_TIMEOUT", "10")),
            # Managed Postgres restarts and idle-timeouts kill pooled sockets;
            # this discards dead ones instead of serving an error to the user.
            check=ConnectionPool.check_connection,
            max_idle=float(os.environ.get("DB_POOL_MAX_IDLE", "300")),
            open=True,
        )
    return _pool


def get_conn() -> Connection:
    pool = get_pool()
    return Connection(pool, pool.getconn())


def wait_for_db(timeout: int = 60) -> None:
    """Postgres in docker-compose / Render may still be booting."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with psycopg.connect(database_url(), connect_timeout=5) as c:
                c.execute("SELECT 1")
            return
        except Exception as e:            # noqa: BLE001
            last = e
            log.info("Waiting for PostgreSQL...")
            time.sleep(2)
    raise RuntimeError(f"PostgreSQL not reachable: {last}")


# ---------------------------------------------------------------- schema

# Dates stay TEXT (ISO yyyy-mm-dd) exactly as in the SQLite version: every
# availability/overlap comparison in app.py is a string comparison, and ISO
# dates sort correctly as text. Keeping the type identical keeps the behaviour
# identical.
SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id              INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    company         TEXT NOT NULL UNIQUE,
    contact_person  TEXT,
    phone           TEXT,
    industry        TEXT,
    created_by      TEXT,
    created_at      TEXT
);

CREATE TABLE IF NOT EXISTS screens (
    id           INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    name         TEXT NOT NULL,
    location     TEXT NOT NULL,
    city         TEXT NOT NULL DEFAULT 'Bhubaneswar',
    width_ft     DOUBLE PRECISION NOT NULL,
    height_ft    DOUBLE PRECISION NOT NULL,
    res_w        INTEGER,
    res_h        INTEGER,
    loop_slots   INTEGER NOT NULL DEFAULT 8,
    slot_seconds INTEGER NOT NULL DEFAULT 15,
    spots_per_day INTEGER NOT NULL DEFAULT 360,
    rate_month   INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'active',
    photo        TEXT
);

CREATE TABLE IF NOT EXISTS campaigns (
    id         INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    client_id  INTEGER NOT NULL REFERENCES clients(id),
    name       TEXT NOT NULL,
    creative   TEXT
);

CREATE TABLE IF NOT EXISTS slots (
    id          INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    screen_id   INTEGER NOT NULL REFERENCES screens(id),
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
    position_no INTEGER NOT NULL,
    start_date  TEXT NOT NULL,
    end_date    TEXT NOT NULL,
    rate_month  INTEGER NOT NULL DEFAULT 0,
    booked_by   TEXT
);

CREATE INDEX IF NOT EXISTS idx_slots_screen_dates
    ON slots (screen_id, start_date, end_date);

CREATE TABLE IF NOT EXISTS users (
    id        INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    username  TEXT NOT NULL UNIQUE,
    name      TEXT NOT NULL,
    role      TEXT NOT NULL DEFAULT 'sales',
    salt      TEXT NOT NULL,
    pw_hash   TEXT NOT NULL,
    created_at TEXT
);

-- idempotent equivalents of the old PRAGMA-based migrations
ALTER TABLE screens ADD COLUMN IF NOT EXISTS photo TEXT;
ALTER TABLE slots   ADD COLUMN IF NOT EXISTS booked_by TEXT;
ALTER TABLE clients ADD COLUMN IF NOT EXISTS created_by TEXT;
ALTER TABLE clients ADD COLUMN IF NOT EXISTS created_at TEXT;
ALTER TABLE users   ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE users   ADD COLUMN IF NOT EXISTS can_upload BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS activity_log (
    id        SERIAL PRIMARY KEY,
    at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_name TEXT NOT NULL,
    action    TEXT NOT NULL,
    detail    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_activity_at ON activity_log (at DESC);

-- ---------------------------------------------------------------- teams
-- Two business units operate on the same installation but must never see each
-- other's inventory, clients, bookings or money. Every business row carries a
-- `team` code; every query in app.py filters on it. 'all' is not a team — it is
-- the admin's "both teams" view and is only ever stored on a user row.
CREATE TABLE IF NOT EXISTS teams (
    code       TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT
);

INSERT INTO teams (code, name, created_at) VALUES
    ('odisha', 'Odisha Team', CURRENT_DATE::text),
    ('raipur', 'Raipur Team', CURRENT_DATE::text)
ON CONFLICT (code) DO NOTHING;

-- Existing installations pre-date the split: everything already in the database
-- is Bhubaneswar/Odisha inventory, so that is the backfill value.
ALTER TABLE screens      ADD COLUMN IF NOT EXISTS team TEXT NOT NULL DEFAULT 'odisha';
ALTER TABLE clients      ADD COLUMN IF NOT EXISTS team TEXT NOT NULL DEFAULT 'odisha';
ALTER TABLE campaigns    ADD COLUMN IF NOT EXISTS team TEXT NOT NULL DEFAULT 'odisha';
ALTER TABLE slots        ADD COLUMN IF NOT EXISTS team TEXT NOT NULL DEFAULT 'odisha';
ALTER TABLE users        ADD COLUMN IF NOT EXISTS team TEXT NOT NULL DEFAULT 'odisha';
ALTER TABLE activity_log ADD COLUMN IF NOT EXISTS team TEXT NOT NULL DEFAULT 'odisha';

CREATE INDEX IF NOT EXISTS idx_screens_team   ON screens (team);
CREATE INDEX IF NOT EXISTS idx_clients_team   ON clients (team);
CREATE INDEX IF NOT EXISTS idx_campaigns_team ON campaigns (team);
CREATE INDEX IF NOT EXISTS idx_slots_team     ON slots (team);
CREATE INDEX IF NOT EXISTS idx_activity_team  ON activity_log (team);

-- A company name was globally unique when there was one team. Odisha and Raipur
-- may both sell to "Union Bank of India", and those are two separate client
-- records, so uniqueness is now per team.
ALTER TABLE clients DROP CONSTRAINT IF EXISTS clients_company_key;
CREATE UNIQUE INDEX IF NOT EXISTS idx_clients_team_company ON clients (team, company);

-- Admin means "all teams" by definition; keep the stored value in step with it.
UPDATE users SET team = 'all' WHERE role = 'admin' AND team <> 'all';
"""


def init_db() -> None:
    wait_for_db()
    with get_conn() as conn:
        conn.executescript(SCHEMA)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
    print("Schema created on PostgreSQL.")
