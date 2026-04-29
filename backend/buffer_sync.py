"""
buffer_sync.py — Windows backend sync from Pi buffer.

Pulls unsynced readings from the Pi buffer API on startup
and every hour. Inserts them into the main database with
original timestamps, preserving the full sensor history.

Called from main.py on startup and via background thread.

Location: backend/buffer_sync.py
"""

import json
import logging
import threading
import time
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

log = logging.getLogger(__name__)

PI_BUFFER_URL   = "http://192.168.1.85:5001"
SYNC_INTERVAL_S = 3600  # sync every hour
BATCH_SIZE      = 1000  # readings per sync batch


# ---------------------------------------------------------------------------
# HTTP helpers — stdlib only, no requests dependency
# ---------------------------------------------------------------------------

def _get(path: str, timeout: int = 10) -> dict | None:
    try:
        url = f"{PI_BUFFER_URL}{path}"
        with urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (URLError, HTTPError, Exception) as e:
        log.debug(f"Buffer API GET {path} failed: {e}")
        return None


def _post(path: str, data: dict, timeout: int = 10) -> dict | None:
    try:
        url = f"{PI_BUFFER_URL}{path}"
        body = json.dumps(data).encode("utf-8")
        req = Request(url, data=body, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (URLError, HTTPError, Exception) as e:
        log.debug(f"Buffer API POST {path} failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Sync logic
# ---------------------------------------------------------------------------

def is_pi_reachable() -> bool:
    """Check if Pi buffer API is reachable."""
    result = _get("/health", timeout=3)
    return result is not None and result.get("status") == "ok"


def sync_once(db_conn) -> int:
    """
    Pull one batch of buffered readings from Pi and insert into main DB.
    Returns number of readings synced.
    """
    if not is_pi_reachable():
        log.debug("Pi buffer API unreachable — skipping sync")
        return 0

    result = _get(f"/buffer/readings?limit={BATCH_SIZE}")
    if not result:
        return 0

    readings = result.get("readings", [])
    if not readings:
        log.debug("No unsynced readings in Pi buffer")
        return 0

    log.info(f"Syncing {len(readings)} buffered readings from Pi")

    synced_ids = []
    inserted = 0

    for r in readings:
        try:
            # Check if this timestamp already exists in main DB
            existing = db_conn.execute(
                "SELECT id FROM raw_readings WHERE ts = ?", (r["ts"],)
            ).fetchone()

            if existing:
                # Already in DB — just mark as synced on Pi
                synced_ids.append(r["id"])
                continue

            # Insert into main database
            db_conn.execute("""
                INSERT INTO raw_readings (
                    ts, value, is_warmup, changed,
                    is_plausible, plausibility_reason,
                    rolling_mean, rolling_std, trend_slope,
                    purifier_on, fan_speed, fan_mode,
                    filter_age, filter_age_unit,
                    device_age, device_age_unit,
                    pm25_internal, control_source
                ) VALUES (
                    :ts, :value, :is_warmup, :changed,
                    :is_plausible, :plausibility_reason,
                    :rolling_mean, :rolling_std, :trend_slope,
                    :purifier_on, :fan_speed, :fan_mode,
                    :filter_age, :filter_age_unit,
                    :device_age, :device_age_unit,
                    :pm25_internal, :control_source
                )
            """, {
                "ts":                  r.get("ts"),
                "value":               r.get("value"),
                "is_warmup":           r.get("is_warmup", 0),
                "changed":             r.get("changed", 1),
                "is_plausible":        r.get("is_plausible"),
                "plausibility_reason": r.get("plausibility_reason"),
                "rolling_mean":        r.get("rolling_mean"),
                "rolling_std":         r.get("rolling_std"),
                "trend_slope":         r.get("trend_slope"),
                "purifier_on":         r.get("purifier_on"),
                "fan_speed":           r.get("fan_speed"),
                "fan_mode":            r.get("fan_mode"),
                "filter_age":          r.get("filter_age"),
                "filter_age_unit":     r.get("filter_age_unit", "minutes"),
                "device_age":          r.get("device_age"),
                "device_age_unit":     r.get("device_age_unit", "minutes"),
                "pm25_internal":       r.get("pm25_internal"),
                "control_source":      "pi_buffer_sync",
            })

            synced_ids.append(r["id"])
            inserted += 1

        except Exception as e:
            log.error(f"Failed to insert buffered reading ts={r.get('ts')}: {e}")

    if synced_ids:
        db_conn.commit()

        # Acknowledge synced IDs to Pi
        ack = _post("/buffer/ack", {"ids": synced_ids})
        if ack:
            log.info(f"Synced {inserted} new readings, {len(synced_ids)} acknowledged")
        else:
            log.warning("Failed to acknowledge sync to Pi — readings may re-sync")

        # Log sync event to maintenance_events
        try:
            db_conn.execute("""
                INSERT INTO maintenance_events
                    (ts, event_type, actor, notes)
                VALUES (?, 'buffer_sync', 'system', ?)
            """, (
                datetime.now(timezone.utc).isoformat(),
                json.dumps({
                    "readings_synced": inserted,
                    "readings_acknowledged": len(synced_ids),
                    "source": "pi_buffer",
                }),
            ))
            db_conn.commit()
        except Exception as e:
            log.debug(f"Could not log sync event: {e}")

    return inserted


def publish_heartbeat(mqtt_client):
    """
    Publish backend heartbeat to Pi so sds011_reader knows Windows is alive.
    Called from main.py MQTT publish client.
    """
    try:
        mqtt_client.publish(
            "airtwin/backend/heartbeat",
            json.dumps({"ts": datetime.now(timezone.utc).isoformat()}),
            qos=1,
        )
    except Exception as e:
        log.debug(f"Heartbeat publish failed: {e}")


# ---------------------------------------------------------------------------
# Background sync thread
# ---------------------------------------------------------------------------

class BufferSyncThread(threading.Thread):
    """
    Background thread that syncs Pi buffer to main DB hourly.
    Also publishes heartbeat to Pi every 30 seconds.
    """

    def __init__(self, db_conn, mqtt_client):
        super().__init__(daemon=True, name="BufferSyncThread")
        self._db_conn = db_conn
        self._mqtt_client = mqtt_client
        self._stop_event = threading.Event()
        self._last_sync = 0.0
        self._heartbeat_interval = 30  # seconds

    def run(self):
        log.info("Buffer sync thread started")

        # Sync immediately on startup
        try:
            count = sync_once(self._db_conn)
            if count > 0:
                log.info(f"Startup sync: {count} readings recovered from Pi buffer")
            self._last_sync = time.time()
        except Exception as e:
            log.error(f"Startup sync failed: {e}")

        last_heartbeat = 0.0

        while not self._stop_event.is_set():
            now = time.time()

            # Publish heartbeat every 30s
            if now - last_heartbeat >= self._heartbeat_interval:
                publish_heartbeat(self._mqtt_client)
                last_heartbeat = now

            # Sync buffer every hour
            if now - self._last_sync >= SYNC_INTERVAL_S:
                try:
                    count = sync_once(self._db_conn)
                    if count > 0:
                        log.info(f"Hourly sync: {count} readings recovered")
                    self._last_sync = now
                except Exception as e:
                    log.error(f"Hourly sync failed: {e}")

            self._stop_event.wait(timeout=5)

        log.info("Buffer sync thread stopped")

    def stop(self):
        self._stop_event.set()


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(f"Pi reachable: {is_pi_reachable()}")
    if is_pi_reachable():
        status = _get("/buffer/status")
        print(f"Buffer status: {status}")