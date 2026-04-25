import json
import logging
import time
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
 
# --- Config ---
BROKER_HOST = "127.0.0.1"
BROKER_PORT = 1883
SOURCE_TOPIC = "zigbee2mqtt/starkvind"
 
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)
 
 
def extract_fields(payload: dict) -> dict:
    """
    Extract and normalise relevant fields from Zigbee2MQTT payload.
 
    Units:
        filter_age  — minutes (convert to hours: filter_age / 60)
        device_age  — minutes (total non-resettable device runtime)
        pm25        — µg/m³ (IKEA internal sensor, cross-reference only)
    """
    return {
        "fan_state":      payload.get("fan_state"),
        "fan_speed":      payload.get("fan_speed"),
        "fan_mode":       payload.get("fan_mode"),
        "filter_age":     payload.get("filter_age"),      # minutes
        "device_age":     payload.get("device_age"),      # minutes
        "replace_filter": payload.get("replace_filter"),
        "pm25_internal":  payload.get("pm25"),            # µg/m³
        "linkquality":    payload.get("linkquality"),
        "air_quality":    payload.get("air_quality"),
    }
 
 
def create_client():
    client = mqtt.Client(CallbackAPIVersion.VERSION2)
 
    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            log.info("MQTT connected")
            client.subscribe(SOURCE_TOPIC)
            log.info(f"Subscribed to {SOURCE_TOPIC}")
        else:
            log.error(f"MQTT connection failed: {reason_code}")
 
    def on_disconnect(client, userdata, flags, reason_code, properties):
        log.warning(f"MQTT disconnected: {reason_code} — will retry")
 
    def on_message(client, userdata, msg):
        try:
            raw = json.loads(msg.payload.decode())
            fields = extract_fields(raw)
 
            state_payload = json.dumps({
                "fan_state":       fields["fan_state"],
                "fan_speed":       fields["fan_speed"],
                "fan_mode":        fields["fan_mode"],
                "filter_age":      fields["filter_age"],      # minutes
                "filter_age_unit": "minutes",
                "device_age":      fields["device_age"],      # minutes
                "device_age_unit": "minutes",
                "replace_filter":  fields["replace_filter"],
                "pm25_internal":   fields["pm25_internal"],
                "linkquality":     fields["linkquality"],
                "air_quality":     fields["air_quality"],
            })
            client.publish("airtwin/purifier/state", state_payload)
            log.info(f"Bridged: fan={fields['fan_state']} speed={fields['fan_speed']} "
                     f"mode={fields['fan_mode']} filter_age={fields['filter_age']}min "
                     f"device_age={fields['device_age']}min "
                     f"pm25_internal={fields['pm25_internal']} replace={fields['replace_filter']}")
 
        except json.JSONDecodeError as e:
            log.error(f"JSON decode error: {e}")
        except Exception as e:
            log.error(f"Bridge error: {e}")
 
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
 
 
def main():
    log.info("Starting zigbee_bridge.py")
    client = create_client()
    client.loop_forever()
 
 
if __name__ == "__main__":
    main()
