import sqlite3
import logging
from pathlib import Path

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "airtwin.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create all tables and views. Safe to call on every startup."""
    conn = get_connection()
    c = conn.cursor()

    # --- Raw readings ---
    c.executescript("""
        CREATE TABLE IF NOT EXISTS raw_readings (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT NOT NULL,
            value           REAL NOT NULL,
            is_warmup       INTEGER NOT NULL DEFAULT 0,
            changed         INTEGER NOT NULL DEFAULT 1,
            is_plausible    INTEGER,
            plausibility_reason TEXT,
            rolling_mean    REAL,
            rolling_std     REAL,
            trend_slope     REAL,
            purifier_on     INTEGER,
            fan_speed       INTEGER,
            fan_mode        TEXT,
            filter_age      INTEGER,
            pm25_internal   REAL,
            control_source  TEXT
        );

        -- Events: threshold crossings, regime transitions, sparse
        CREATE TABLE IF NOT EXISTS events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            event_type  TEXT NOT NULL,
            detail      TEXT,
            value       REAL,
            regime      TEXT
        );

        -- Maintenance events: filter changes, actor, before/after state
        CREATE TABLE IF NOT EXISTS maintenance_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT NOT NULL,
            event_type      TEXT NOT NULL,
            actor           TEXT,
            filter_type     TEXT,
            filter_age_at_change INTEGER,
            pm25_before     REAL,
            pm25_after      REAL,
            notes           TEXT
        );

        -- State transitions: every FSM regime change
        CREATE TABLE IF NOT EXISTS state_transitions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT NOT NULL,
            from_regime     TEXT,
            to_regime       TEXT NOT NULL,
            reason          TEXT,
            duration_sec    INTEGER
        );

        -- Escalation events: operator alerts and responses
        CREATE TABLE IF NOT EXISTS escalation_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_raised       TEXT NOT NULL,
            ts_resolved     TEXT,
            escalation_type TEXT NOT NULL,
            detail          TEXT,
            operator_response TEXT,
            resolved        INTEGER NOT NULL DEFAULT 0
        );

        -- Control log: every purifier command issued
        CREATE TABLE IF NOT EXISTS control_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT NOT NULL,
            command         TEXT NOT NULL,
            value           TEXT,
            control_source  TEXT NOT NULL,
            pm25_at_command REAL,
            observed_response TEXT
        );

        -- Views
        CREATE VIEW IF NOT EXISTS recent_readings AS
            SELECT * FROM raw_readings
            ORDER BY ts DESC
            LIMIT 300;

        CREATE VIEW IF NOT EXISTS open_escalations AS
            SELECT * FROM escalation_events
            WHERE resolved = 0
            ORDER BY ts_raised DESC;

        CREATE VIEW IF NOT EXISTS filter_history AS
            SELECT * FROM maintenance_events
            WHERE event_type = 'filter_change'
            ORDER BY ts DESC;
    """)

    conn.commit()
    conn.close()
    log.info(f"Database initialised at {DB_PATH}")


def insert_reading(conn: sqlite3.Connection, payload: dict):
    """Insert one qualified reading from the MQTT subscriber."""
    conn.execute("""
        INSERT INTO raw_readings (
            ts, value, is_warmup, changed, is_plausible, plausibility_reason,
            rolling_mean, rolling_std, trend_slope,
            purifier_on, fan_speed, fan_mode, filter_age, pm25_internal, control_source
        ) VALUES (
            :timestamp, :value, :is_warmup, :changed, :is_plausible, :plausibility_reason,
            :rolling_mean, :rolling_std, :trend_slope,
            :purifier_on, :fan_speed, :fan_mode, :filter_age, :pm25_internal, :control_source
        )
    """, payload)
    conn.commit()


def insert_event(conn: sqlite3.Connection, event_type: str, detail: str = None,
                 value: float = None, regime: str = None):
    from datetime import datetime, timezone
    conn.execute("""
        INSERT INTO events (ts, event_type, detail, value, regime)
        VALUES (?, ?, ?, ?, ?)
    """, (datetime.now(timezone.utc).isoformat(), event_type, detail, value, regime))
    conn.commit()


def insert_state_transition(conn: sqlite3.Connection, from_regime: str,
                             to_regime: str, reason: str = None, duration_sec: int = None):
    from datetime import datetime, timezone
    conn.execute("""
        INSERT INTO state_transitions (ts, from_regime, to_regime, reason, duration_sec)
        VALUES (?, ?, ?, ?, ?)
    """, (datetime.now(timezone.utc).isoformat(), from_regime, to_regime, reason, duration_sec))
    conn.commit()


def insert_control_log(conn: sqlite3.Connection, command: str, value: str,
                       control_source: str, pm25_at_command: float = None,
                       observed_response: str = None):
    from datetime import datetime, timezone
    conn.execute("""
        INSERT INTO control_log (ts, command, value, control_source, pm25_at_command, observed_response)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (datetime.now(timezone.utc).isoformat(), command, value, control_source,
          pm25_at_command, observed_response))
    conn.commit()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
    print(f"Database created at {DB_PATH}")