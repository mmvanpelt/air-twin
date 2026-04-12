import json
import logging
import time
from datetime import datetime, timezone
import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)

TOPIC_HEARTBEAT = "airtwin/control/heartbeat"
TOPIC_RESUME = "airtwin/control/resume"
TOPIC_SHUTDOWN = "airtwin/control/shutdown"

MODE_REMOTE = "remote"
MODE_LOCAL = "local"


class ControlHandler:
    """
    Phase 1 stub - monitors heartbeat, tracks mode, logs transitions.
    No Zigbee commands issued. No local threshold enforcement.
    Phase 3 will add command execution and fallback policy activation.
    """

    def __init__(self, config_path: str, mqtt_client: mqtt.Client):
        self.mqtt_client = mqtt_client
        self.config = self._load_config(config_path)
        self.mode = MODE_REMOTE
        self.last_heartbeat = time.time()
        self.transition_log = []
        self._subscribe()
        log.info("ControlHandler initialised - mode: remote (Phase 1 stub)")

    def _load_config(self, path: str) -> dict:
        with open(path) as f:
            config = json.load(f)
        log.info(f"Control config loaded from {path}")
        return config

    def _subscribe(self):
        self.mqtt_client.subscribe(TOPIC_HEARTBEAT)
        self.mqtt_client.subscribe(TOPIC_RESUME)
        self.mqtt_client.subscribe(TOPIC_SHUTDOWN)
        log.info("ControlHandler subscribed to control topics")

    def on_message(self, topic: str, payload: str):
        """Call this from the main MQTT on_message handler."""
        if topic == TOPIC_HEARTBEAT:
            self.last_heartbeat = time.time()
            log.debug("Heartbeat received")
        elif topic == TOPIC_RESUME:
            if self.mode == MODE_LOCAL:
                self._transition(MODE_REMOTE, reason="resume command received")
                self._publish_transition_log()
        elif topic == TOPIC_SHUTDOWN:
            if self.mode == MODE_REMOTE:
                self._transition(MODE_LOCAL, reason="shutdown command received")

    def check_heartbeat(self):
        """
        Call periodically from main l
cat > pi/control_handler.py << 'PYEOF'
import json
import logging
import time
from datetime import datetime, timezone
import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)

TOPIC_HEARTBEAT = "airtwin/control/heartbeat"
TOPIC_RESUME = "airtwin/control/resume"
TOPIC_SHUTDOWN = "airtwin/control/shutdown"

MODE_REMOTE = "remote"
MODE_LOCAL = "local"


class ControlHandler:
    """
    Phase 1 stub - monitors heartbeat, tracks mode, logs transitions.
    No Zigbee commands issued. No local threshold enforcement.
    Phase 3 will add command execution and fallback policy activation.
    """

    def __init__(self, config_path: str, mqtt_client: mqtt.Client):
        self.mqtt_client = mqtt_client
        self.config = self._load_config(config_path)
        self.mode = MODE_REMOTE
        self.last_heartbeat = time.time()
        self.transition_log = []
        self._subscribe()
        log.info("ControlHandler initialised - mode: remote (Phase 1 stub)")

    def _load_config(self, path: str) -> dict:
        with open(path) as f:
            config = json.load(f)
        log.info(f"Control config loaded from {path}")
        return config

    def _subscribe(self):
        self.mqtt_client.subscribe(TOPIC_HEARTBEAT)
        self.mqtt_client.subscribe(TOPIC_RESUME)
        self.mqtt_client.subscribe(TOPIC_SHUTDOWN)
        log.info("ControlHandler subscribed to control topics")

    def on_message(self, topic: str, payload: str):
        """Call this from the main MQTT on_message handler."""
        if topic == TOPIC_HEARTBEAT:
            self.last_heartbeat = time.time()
            log.debug("Heartbeat received")
        elif topic == TOPIC_RESUME:
            if self.mode == MODE_LOCAL:
                self._transition(MODE_REMOTE, reason="resume command received")
                self._publish_transition_log()
        elif topic == TOPIC_SHUTDOWN:
            if self.mode == MODE_REMOTE:
                self._transition(MODE_LOCAL, reason="shutdown command received")

    def check_heartbeat(self):
        """
        Call periodically from main loop.
        Phase 1: logs transition only, no action taken.
        Phase 3: will activate local fallback policy on transition to local.
        """
        timeout = self.config["heartbeat_timeout_sec"]
        elapsed = time.time() - self.last_heartbeat
        if self.mode == MODE_REMOTE and elapsed > timeout:
            self._transition(MODE_LOCAL, reason=f"heartbeat timeout - {elapsed:.0f}s since last heartbeat")
        elif self.mode == MODE_LOCAL and elapsed <= timeout:
            self._transition(MODE_REMOTE, reason="heartbeat restored")
            self._publish_transition_log()

    def _transition(self, new_mode: str, reason: str):
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "from": self.mode,
            "to": new_mode,
            "reason": reason,
        }
        self.transition_log.append(entry)
        self.mode = new_mode
        log.warning(f"ControlHandler mode transition: {entry['from']} -> {entry['to']} ({reason})")

    def _publish_transition_log(self):
        if not self.transition_log:
            return
        payload = json.dumps({
            "event": "control_mode_log",
            "transitions": self.transition_log,
        })
        self.mqtt_client.publish("airtwin/control/mode_log", payload)
        log.info(f"Published control mode log - {len(self.transition_log)} transitions")
        self.transition_log.clear()

    def get_status(self) -> dict:
        return {
            "control_mode": self.mode,
            "seconds_since_heartbeat": round(time.time() - self.last_heartbeat, 1),
            "transition_count": len(self.transition_log),
        }
