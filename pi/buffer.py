"""
buffer.py — Local SQLite buffer for Air Twin Pi edge processor.

Stores sensor readings and purifier state when the Windows backend
is unreachable. Synced to Windows on reconnect.

Location: /home/pi/air-twin/pi/buffer.py
"""

import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timezone

log = logging.getLogger(__name__)

BUFFER_PATH = Path(__file__).parent.parent / "data" / "buffer.db"


def get_conn() -> sqlite3.Connection:
    BUFFER_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(BUFFER_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_buffer():
    """Create buffer tables. Safe to call on every startup."""
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS buffered_readings (
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
            filter_age_unit TEXT DEFAULT 'minutes',
            device_age      INTEGER,
            device_age_unit TEXT DEFAULT 'minutes',
            pm25_internal   REAL,
            synced          INTEGER NOT NULL DEFAULT 0,
            synced_at       TEXT
        );

        CREATE TABLE IF NOT EXISTS buffer_meta (
            key     TEXT PRIMARY KEY,
            value   TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_buffered_unsynced
            ON buffered_readings(synced, ts);
    """)
    conn.commit()
    conn.close()
    log.info(f"Buffer initialised at {BUFFER_PATH}")


def insert_reading(payload: dict) -> None:
    """Insert one reading into the local buffer."""
    conn = get_conn()
    try:
        conn.execute("""
            INSERT INTO buffered_readings (
                ts, value, is_warmup, changed,
                is_plausible, plausibility_reason,
                rolling_mean, rolling_std, trend_slope,
                purifier_on, fan_speed, fan_mode,
                filter_age, filter_age_unit,
                device_age, device_age_unit,
                pm25_internal
            ) VALUES (
                :timestamp, :value, :is_warmup, :changed,
                :is_plausible, :plausibility_reason,
                :rolling_mean, :rolling_std, :trend_slope,
                :purifier_on, :fan_speed, :fan_mode,
                :filter_age, :filter_age_unit,
                :device_age, :device_age_unit,
                :pm25_internal
            )
        """, payload)
        conn.commit()
    except Exception as e:
        log.error(f"Buffer insert failed: {e}")
    finally:
        conn.close()


def get_unsynced(limit: int = 5000) -> list[dict]:
    """Return unsynced readings ordered by timestamp."""
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT * FROM buffered_readings
            WHERE synced = 0
            ORDER BY ts ASC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_synced(ids: list[int]) -> None:
    """Mark readings as synced after Windows acknowledges receipt."""
    if not ids:
        return
    conn = get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn.executemany("""
            UPDATE buffered_readings
            SET synced = 1, synced_at = ?
            WHERE id = ?
        """, [(now, i) for i in ids])
        conn.commit()
        log.info(f"Marked {len(ids)} readings as synced")
    finally:
        conn.close()


def purge_old_synced(days: int = 7) -> int:
    """Delete synced readings older than N days to keep buffer small."""
    conn = get_conn()
    try:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cursor = conn.execute("""
            DELETE FROM buffered_readings
            WHERE synced = 1 AND synced_at < ?
        """, (cutoff,))
        conn.commit()
        count = cursor.rowcount
        if count > 0:
            log.info(f"Purged {count} old synced readings from buffer")
        return count
    finally:
        conn.close()


def buffer_stats() -> dict:
    """Return buffer statistics."""
    conn = get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM buffered_readings").fetchone()[0]
        unsynced = conn.execute(
            "SELECT COUNT(*) FROM buffered_readings WHERE synced = 0"
        ).fetchone()[0]
        oldest = conn.execute(
            "SELECT MIN(ts) FROM buffered_readings WHERE synced = 0"
        ).fetchone()[0]
        newest = conn.execute(
            "SELECT MAX(ts) FROM buffered_readings WHERE synced = 0"
        ).fetchone()[0]
        return {
            "total": total,
            "unsynced": unsynced,
            "oldest_unsynced": oldest,
            "newest_unsynced": newest,
        }
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_buffer()
    print(f"Buffer initialised at {BUFFER_PATH}")
    print(buffer_stats())