"""
Heartbeat publisher stub - Phase 1.
Structure is in place. Activates fully in Phase 3 when
the twin engine is running and issuing control commands.
"""
import json
import logging
import threading
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)

TOPIC_HEARTBEAT = "airtwin/control/heartbeat"
TOPIC_RESUME = "airtwin/control/resume"
TOPIC_SHUTDOWN = "airtwin/control/shutdown"
HEARTBEAT_INTERVAL_SEC = 10


class HeartbeatPublisher:
    """
    Phase 1 stub - class and MQTT topics defined.
    start() is a no-op in Phase 1.
    Phase 3: call start() from main.py on startup,
    stop() on clean shutdown.
    """

    def __init__(self, mqtt_client: mqtt.Client):
        self.mqtt_client = mqtt_client
        self._running = False
        self._thread = None
        log.info("HeartbeatPublisher initialised (Phase 1 stub - not yet active)")

    def start(self):
        # Phase 1: no-op
        # Phase 3: uncomment below
        # self._running = True
        # self._thread = threading.Thread(target=self._run, daemon=True)
        # self._thread.start()
        # self.mqtt_client.publish(TOPIC_RESUME, json.dumps({
        #     "timestamp": datetime.now(timezone.utc).isoformat()
        # }))
        log.info("HeartbeatPublisher.start() called - no-op in Phase 1")

    def stop(self):
        # Phase 1: no-op
        # Phase 3: uncomment below
        # self._running = False
        # self.mqtt_client.publish(TOPIC_SHUTDOWN, json.dumps({
        #     "timestamp": datetime.now(timezone.utc).isoformat()
        # }))
        log.info("HeartbeatPublisher.stop() called - no-op in Phase 1")

    def _run(self):
        """Phase 3: background thread - publishes heartbeat every 10s."""
        log.info("Heartbeat publisher running")
        while self._running:
            payload = json.dumps({
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
            self.mqtt_client.publish(TOPIC_HEARTBEAT, payload)
            log.debug("Heartbeat published")
            time.sleep(HEARTBEAT_INTERVAL_SEC)
