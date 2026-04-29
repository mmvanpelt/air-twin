"""
sds011_reader.py — SDS011 PM2.5 sensor reader with buffer fallback.

Reads PM2.5 at 1Hz, qualifies readings, and publishes over MQTT.
When the Windows backend MQTT broker is unreachable, writes to the
local SQLite buffer instead. Syncs automatically when backend reconnects.

Location: /home/pi/air-twin/pi/sds011_reader.py
"""

import serial
import time
import json
import logging
import statistics
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from enum import Enum

import paho.mqtt.client as mqtt

# Add parent to path for buffer import
sys.path.insert(0, str(Path(__file__).parent))
import buffer as buf

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SERIAL_PORT   = "/dev/ttyUSB_SDS011"
BAUD_RATE     = 9600
WARMUP_S      = 30
WINDOW        = 60        # rolling window size
BROKER_HOST   = "localhost"
BROKER_PORT   = 1883
TOPIC_PM25    = "airtwin/sensor/pm25"
TOPIC_STATUS  = "airtwin/sensor/status"

# Plausibility thresholds
MAX_VALUE         = 999.9
MAX_DELTA         = 150.0   # max change between readings
SPIKE_STD_MULT    = 6.0     # readings > mean + N*std flagged

# Backend heartbeat — if MQTT not connected, buffer mode activates
BACKEND_HEARTBEAT_TOPIC  = "airtwin/backend/heartbeat"
BACKEND_TIMEOUT_S        = 120  # 2 min without heartbeat → buffer mode

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Plausibility
# ---------------------------------------------------------------------------

class PlausibilityReason(str, Enum):
    OK              = "ok"
    OUT_OF_RANGE    = "out_of_range"
    DELTA_EXCEEDED  = "delta_exceeded"
    SPIKE_DETECTED  = "spike_detected"


def check_plausibility(value: float, prev: float | None,
                       mean: float | None, std: float | None) -> tuple[bool, str]:
    if value < 0 or value > MAX_VALUE:
        return False, PlausibilityReason.OUT_OF_RANGE

    if prev is not None and abs(value - prev) > MAX_DELTA:
        return False, PlausibilityReason.DELTA_EXCEEDED

    if mean is not None and std is not None and std > 0:
        if value > mean + SPIKE_STD_MULT * std:
            return False, PlausibilityReason.SPIKE_DETECTED

    return True, PlausibilityReason.OK


# ---------------------------------------------------------------------------
# Serial reader
# ---------------------------------------------------------------------------

def read_sds011(ser: serial.Serial) -> float | None:
    """Read one PM2.5 value from SDS011 serial stream."""
    data = []
    while True:
        byte = ser.read(1)
        if byte == b'\xaa':
            data = [0xaa]
        elif data:
            data.append(ord(byte))
            if len(data) == 10:
                if data[0] == 0xaa and data[1] == 0xc0 and data[9] == 0xab:
                    pm25 = ((data[3] * 256) + data[2]) / 10.0
                    return pm25
                data = []


# ---------------------------------------------------------------------------
# MQTT client with buffer fallback
# ---------------------------------------------------------------------------

class BufferedPublisher:
    """
    MQTT publisher with automatic buffer fallback.

    When the Windows backend is reachable, publishes directly over MQTT.
    When unreachable (heartbeat timeout), writes to local SQLite buffer.
    Automatically switches back to MQTT when backend reconnects.
    """

    def __init__(self):
        self._connected = False
        self._buffer_mode = False
        self._last_heartbeat = 0.0
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                   client_id="sds011_reader")
        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message    = self._on_message

        buf.init_buffer()
        log.info("BufferedPublisher initialised")

    def connect(self):
        self._client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
        self._client.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            self._connected = True
            log.info("MQTT connected")
            # Subscribe to backend heartbeat
            client.subscribe(BACKEND_HEARTBEAT_TOPIC)
            log.info(f"Subscribed to {BACKEND_HEARTBEAT_TOPIC}")
        else:
            log.warning(f"MQTT connect failed: {reason_code}")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self._connected = False
        log.warning("MQTT disconnected — activating buffer mode")
        self._buffer_mode = True

    def _on_message(self, client, userdata, message):
        if message.topic == BACKEND_HEARTBEAT_TOPIC:
            self._last_heartbeat = time.time()
            if self._buffer_mode:
                log.info("Backend heartbeat received — switching back to MQTT mode")
                self._buffer_mode = False

    def _check_heartbeat(self):
        """Activate buffer mode if backend heartbeat times out."""
        if self._last_heartbeat == 0:
            # Never received — give grace period on startup
            if time.time() > WARMUP_S + BACKEND_TIMEOUT_S:
                if not self._buffer_mode:
                    log.warning("No backend heartbeat — activating buffer mode")
                    self._buffer_mode = True
            return

        elapsed = time.time() - self._last_heartbeat
        if elapsed > BACKEND_TIMEOUT_S and not self._buffer_mode:
            log.warning(f"Backend heartbeat timeout ({elapsed:.0f}s) — buffer mode")
            self._buffer_mode = True

    def publish(self, payload: dict):
        """Publish reading to MQTT or buffer depending on backend availability."""
        self._check_heartbeat()

        if self._connected and not self._buffer_mode:
            # Normal mode — publish over MQTT
            try:
                msg = json.dumps(payload)
                self._client.publish(TOPIC_PM25, msg, qos=1)
            except Exception as e:
                log.error(f"MQTT publish failed: {e} — buffering")
                buf.insert_reading(payload)
        else:
            # Buffer mode — write to local SQLite
            buf.insert_reading(payload)
            stats = buf.buffer_stats()
            if stats["unsynced"] % 100 == 0:
                log.info(f"Buffer mode: {stats['unsynced']} readings buffered")

    def stop(self):
        self._client.loop_stop()
        self._client.disconnect()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    log.info(f"Opening serial port {SERIAL_PORT}")
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=2)
    except serial.SerialException as e:
        log.error(f"Serial port error: {e}")
        sys.exit(1)

    publisher = BufferedPublisher()
    publisher.connect()

    # Rolling window state
    window: deque = deque(maxlen=WINDOW)
    prev_value: float | None = None
    start_time = time.time()
    reading_count = 0

    log.info(f"Warming up for {WARMUP_S}s...")

    while True:
        try:
            value = read_sds011(ser)
            if value is None:
                continue

            reading_count += 1
            elapsed = time.time() - start_time
            is_warmup = elapsed < WARMUP_S

            # Rolling stats
            window.append(value)
            rolling_mean = statistics.mean(window) if len(window) > 1 else value
            rolling_std  = statistics.stdev(window) if len(window) > 1 else 0.0
            trend_slope  = None

            if len(window) >= 10:
                n = len(window)
                xs = list(range(n))
                ys = list(window)
                x_mean = sum(xs) / n
                y_mean = sum(ys) / n
                num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
                den = sum((x - x_mean) ** 2 for x in xs)
                trend_slope = num / den if den != 0 else 0.0

            # Plausibility
            mean_for_check = rolling_mean if len(window) > 5 else None
            std_for_check  = rolling_std  if len(window) > 5 else None
            is_plausible, reason = check_plausibility(
                value, prev_value, mean_for_check, std_for_check
            )

            # Changed flag
            changed = prev_value is None or abs(value - prev_value) >= 0.1

            ts = datetime.now(timezone.utc).isoformat()

            payload = {
                "timestamp":           ts,
                "value":               round(value, 1),
                "is_warmup":           int(is_warmup),
                "changed":             int(changed),
                "is_plausible":        int(is_plausible),
                "plausibility_reason": reason,
                "rolling_mean":        round(rolling_mean, 3),
                "rolling_std":         round(rolling_std, 3),
                "trend_slope":         round(trend_slope, 6) if trend_slope is not None else None,
                "purifier_on":         None,
                "fan_speed":           None,
                "fan_mode":            None,
                "filter_age":          None,
                "filter_age_unit":     "minutes",
                "device_age":          None,
                "device_age_unit":     "minutes",
                "pm25_internal":       None,
                "control_source":      None,
            }

            if not is_warmup:
                publisher.publish(payload)

            prev_value = value
            time.sleep(1)

        except serial.SerialException as e:
            log.error(f"Serial error: {e}")
            time.sleep(5)
        except KeyboardInterrupt:
            log.info("Stopping")
            publisher.stop()
            ser.close()
            sys.exit(0)
        except Exception as e:
            log.error(f"Unexpected error: {e}")
            time.sleep(1)


if __name__ == "__main__":
    main()