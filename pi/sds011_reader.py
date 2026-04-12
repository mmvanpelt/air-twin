import serial
import struct
import time
import json
import math
import logging
from collections import deque
from datetime import datetime, timezone
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

# --- Config ---
SERIAL_PORT = "/dev/ttyUSB_SDS011"
BAUD_RATE = 9600
BROKER_HOST = "127.0.0.1"
BROKER_PORT = 1883
TOPIC_PM25 = "airtwin/sensor/pm25"
READ_INTERVAL_SEC = 60
WARMUP_INTERVAL_SEC = 2

WARMUP_READINGS = 15
ROLLING_WINDOW_SIZE = 30
DELTA_MULTIPLIER = 4.0
Z_SCORE_THRESHOLD = 4.0
MIN_WINDOW_FILL = 10
SENSOR_MIN = 0.0
SENSOR_MAX = 999.9
ABSOLUTE_FLOOR = 5.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)


# --- Warmup Filter ---
class WarmupFilter:
    def __init__(self, warmup_count=WARMUP_READINGS):
        self.warmup_count = warmup_count
        self.count = 0
        self.complete = False

    def is_warmed_up(self):
        if self.complete:
            return True
        self.count += 1
        if self.count <= self.warmup_count:
            log.info(f"Warmup reading {self.count}/{self.warmup_count} — discarding")
            return False
        self.complete = True
        log.info("Warmup complete")
        return True


# --- Hardware Bounds Check ---
class HardwareBoundsCheck:
    def check(self, value):
        if value < SENSOR_MIN or value > SENSOR_MAX:
            log.warning(f"Hardware bounds exceeded: {value} µg/m³ — rejected")
            return False
        return True


# --- Rolling Window (Welford algorithm) ---
class RollingWindow:
    def __init__(self, size=ROLLING_WINDOW_SIZE):
        self.size = size
        self.window = deque(maxlen=size)
        self.mean = 0.0
        self.M2 = 0.0
        self.count = 0

    def add(self, value):
        if len(self.window) == self.size:
            old = self.window[0]
            self.count -= 1
            delta = old - self.mean
            self.mean -= delta / self.count if self.count > 0 else 0
            self.M2 -= delta * (old - self.mean)

        self.window.append(value)
        self.count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        delta2 = value - self.mean
        self.M2 += delta * delta2

    def get_stats(self):
        if self.count < 2:
            return self.mean, 0.0
        variance = self.M2 / (self.count - 1)
        std = math.sqrt(max(0.0, variance))
        return self.mean, std

    def filled(self):
        return len(self.window) >= MIN_WINDOW_FILL


# --- Plausibility Checker ---
class PlausibilityChecker:
    def __init__(self, window: RollingWindow):
        self.window = window

    def check(self, value):
        if not self.window.filled():
            if value < ABSOLUTE_FLOOR and value > SENSOR_MIN:
                log.info(f"Below absolute floor ({value}) — accepted in early window")
            return True, "window_filling"

        mean, std = self.window.get_stats()
        delta = value - mean

        if std > 0 and delta > DELTA_MULTIPLIER * std:
            log.warning(f"Plausibility fail: {value} µg/m³ delta={delta:.2f} std={std:.2f}")
            return False, "delta_exceeded"

        return True, "ok"


# --- Trend Calculator ---
class TrendCalculator:
    def __init__(self, window: RollingWindow):
        self.window = window

    def slope(self):
        data = list(self.window.window)
        n = len(data)
        if n < 2:
            return 0.0
        x_mean = (n - 1) / 2.0
        y_mean = sum(data) / n
        numerator = sum((i - x_mean) * (data[i] - y_mean) for i in range(n))
        denominator = sum((i - x_mean) ** 2 for i in range(n))
        return numerator / denominator if denominator != 0 else 0.0


# --- SDS011 Reader ---
def read_sds011(ser):
    """Read one PM2.5 frame from SDS011. Returns value or None."""
    while True:
        byte = ser.read(1)
        if byte == b'\xaa':
            frame = ser.read(9)
            if len(frame) == 9 and frame[0] == 0xc0 and frame[8] == 0xab:
                pm25_raw = struct.unpack('<H', frame[1:3])[0]
                return round(pm25_raw / 10.0, 1)
    return None


# --- MQTT setup ---
def create_mqtt_client():
    client = mqtt.Client(CallbackAPIVersion.VERSION2)

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            log.info("MQTT connected")
        else:
            log.error(f"MQTT connection failed: {reason_code}")

    def on_disconnect(client, userdata, flags, reason_code, properties):
        log.warning(f"MQTT disconnected: {reason_code} — will retry")

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
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

    client.loop_start()
    return client


# --- Serial port with retry ---
def open_serial(port, baud, retries=10, delay=5):
    for attempt in range(1, retries + 1):
        try:
            ser = serial.Serial(port, baud, timeout=2)
            log.info(f"Serial open on {port}")
            return ser
        except serial.SerialException as e:
            log.warning(f"Serial open failed (attempt {attempt}/{retries}): {e} — retrying in {delay}s")
            time.sleep(delay)
    raise RuntimeError(f"Could not open {port} after {retries} attempts")


# --- Main loop ---
def main():
    log.info("Starting sds011_reader.py")

    warmup = WarmupFilter()
    bounds = HardwareBoundsCheck()
    window = RollingWindow()
    plausibility = PlausibilityChecker(window)
    trend = TrendCalculator(window)
    mqtt_client = create_mqtt_client()

    while True:
        try:
            ser = open_serial(SERIAL_PORT, BAUD_RATE)
            while True:
                start = time.time()

                try:
                    value = read_sds011(ser)
                except Exception as e:
                    log.error(f"Serial read error: {e} — reopening port")
                    ser.close()
                    break

                if value is None:
                    log.warning("Failed to read from SDS011")
                    time.sleep(READ_INTERVAL_SEC)
                    continue

                if not warmup.is_warmed_up():
                    time.sleep(WARMUP_INTERVAL_SEC)
                    continue

                # Flush stale buffer on first real reading
                if warmup.complete and warmup.count == WARMUP_READINGS + 1:
                    ser.reset_input_buffer()
                    log.info("Serial buffer flushed — first real reading incoming")

                if not bounds.check(value):
                    time.sleep(READ_INTERVAL_SEC)
                    continue

                is_plausible, plausibility_reason = plausibility.check(value)
                rolling_mean, rolling_std = window.get_stats()
                trend_slope = trend.slope()

                window.add(value)

                payload = {
                    "value": value,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "is_plausible": is_plausible,
                    "plausibility_reason": plausibility_reason,
                    "rolling_mean": round(rolling_mean, 2),
                    "rolling_std": round(rolling_std, 2),
                    "trend_slope": round(trend_slope, 4),
                    "purifier_on": None,
                    "fan_speed": None,
                }

                mqtt_client.publish(TOPIC_PM25, json.dumps(payload))
                log.info(f"Published: {payload}")

                elapsed = time.time() - start
                time.sleep(max(0, READ_INTERVAL_SEC - elapsed))

        except RuntimeError as e:
            log.error(f"Fatal serial error: {e} — retrying in 60s")
            time.sleep(60)


if __name__ == "__main__":
    main()
