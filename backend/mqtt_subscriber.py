import json
import logging
import threading
import time
from datetime import datetime, timezone
 
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
 
from backend.db import get_connection, insert_reading, insert_event
 
log = logging.getLogger(__name__)
 
# --- Config ---
BROKER_HOST = "192.168.1.85"
BROKER_PORT = 1883
TOPIC_PM25 = "airtwin/sensor/pm25"
TOPIC_PURIFIER = "airtwin/purifier/state"
 
 
class MQTTSubscriber:
    """
    Subscribes to PM2.5 and purifier state topics.
    Merges latest purifier state into each PM2.5 reading before persisting.
 
    Units note:
        filter_age and device_age are stored in minutes as received from the bridge.
        Convert to hours in the twin engine: value / 60
        Never convert here — single conversion point principle.
    """
 
    def __init__(self):
        self._lock = threading.Lock()
        self._purifier_state = {}
        self._conn = get_connection()
        self._client = self._create_client()
 
    def _create_client(self) -> mqtt.Client:
        client = mqtt.Client(CallbackAPIVersion.VERSION2)
 
        def on_connect(client, userdata, flags, reason_code, properties):
            if reason_code == 0:
                log.info("MQTT subscriber connected")
                client.subscribe(TOPIC_PM25)
                client.subscribe(TOPIC_PURIFIER)
                log.info(f"Subscribed to {TOPIC_PM25} and {TOPIC_PURIFIER}")
            else:
                log.error(f"MQTT connect failed: {reason_code}")
 
        def on_disconnect(client, userdata, flags, reason_code, properties):
            log.warning(f"MQTT disconnected: {reason_code} — will retry")
 
        def on_message(client, userdata, msg):
            try:
                payload = json.loads(msg.payload.decode())
                if msg.topic == TOPIC_PURIFIER:
                    self._handle_purifier(payload)
                elif msg.topic == TOPIC_PM25:
                    self._handle_pm25(payload)
            except json.JSONDecodeError as e:
                log.error(f"JSON decode error on {msg.topic}: {e}")
            except Exception as e:
                log.error(f"Message handler error: {e}")
 
        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message
        client.reconnect_delay_set(min_delay=1, max_delay=60)
 
        delay = 1
        while True:
            try:
                client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
                break
            except Exception as e:
                log.warning(f"MQTT connect failed: {e} — retrying in {delay}s")
                time.sleep(delay)
                delay = min(delay * 2, 60)
 
        return client
 
    def _handle_purifier(self, payload: dict):
        """Cache latest purifier state — merged into next PM2.5 reading."""
        with self._lock:
            self._purifier_state = payload
        log.debug(f"Purifier state updated: fan={payload.get('fan_state')} "
                  f"speed={payload.get('fan_speed')} "
                  f"filter_age={payload.get('filter_age')}min "
                  f"device_age={payload.get('device_age')}min")
 
    def _handle_pm25(self, payload: dict):
        """Merge purifier state into PM2.5 payload and persist."""
        with self._lock:
            purifier = dict(self._purifier_state)
 
        merged = {
            "timestamp":           payload.get("timestamp", datetime.now(timezone.utc).isoformat()),
            "value":               payload.get("value"),
            "is_warmup":           int(payload.get("is_warmup", False)),
            "changed":             int(payload.get("changed", True)),
            "is_plausible":        payload.get("is_plausible"),
            "plausibility_reason": payload.get("plausibility_reason"),
            "rolling_mean":        payload.get("rolling_mean"),
            "rolling_std":         payload.get("rolling_std"),
            "trend_slope":         payload.get("trend_slope"),
            "purifier_on":         1 if purifier.get("fan_state") == "ON" else (0 if purifier.get("fan_state") == "OFF" else None),
            "fan_speed":           purifier.get("fan_speed"),
            "fan_mode":            purifier.get("fan_mode"),
            "filter_age":          purifier.get("filter_age"),          # minutes
            "filter_age_unit":     purifier.get("filter_age_unit", "minutes"),
            "device_age":          purifier.get("device_age"),          # minutes
            "device_age_unit":     purifier.get("device_age_unit", "minutes"),
            "pm25_internal":       purifier.get("pm25_internal"),
            "control_source":      None,  # Phase 3: twin_engine or local_fallback
        }
 
        insert_reading(self._conn, merged)
        log.info(f"Persisted: pm25={merged['value']} purifier_on={merged['purifier_on']} "
                 f"speed={merged['fan_speed']} filter_age={merged['filter_age']}min "
                 f"device_age={merged['device_age']}min")
 
    def start(self):
        """Start the MQTT loop in a background thread."""
        self._client.loop_start()
        log.info("MQTT subscriber running")
 
    def stop(self):
        """Clean shutdown."""
        self._client.loop_stop()
        self._client.disconnect()
        self._conn.close()
        log.info("MQTT subscriber stopped")
 
 
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    subscriber = MQTTSubscriber()
    subscriber.start()
 
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        subscriber.stop()