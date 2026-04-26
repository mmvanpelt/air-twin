"""
main.py — FastAPI application entry point for the Air Twin backend.

Wires together:
  - Database initialisation (db.py)
  - MQTT subscriber (mqtt_subscriber.py) — extended to call twin engine
  - Twin engine (twin_engine/engine.py) — receives publish callback
  - Brief generator (brief_generator.py) — called by API routes
  - WebSocket — pushes twin state to connected clients after each cycle

Routes:
  GET  /state                — current twin state (all fields)
  GET  /brief                — four-role brief (executive/operator/engineer/technician)
  GET  /brief/{role}         — single role view
  GET  /health               — liveness check
  POST /maintenance          — log filter change (QR scan endpoint)
  POST /maintenance/reset    — technician baseline reset
  GET  /ws                   — WebSocket for live state updates

Engine is started as part of app lifespan. MQTT subscriber runs in
background thread. Engine processes readings on the subscriber thread
and notifies WebSocket clients via asyncio callback.
"""

import asyncio
import json
import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import paho.mqtt.client as mqtt
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from backend.db import get_connection, init_db, insert_reading
from backend import brief_generator
from backend.twin_engine import engine as engine_mod
from backend.twin_engine.engine import TwinEngine
from backend.twin_engine.models import (
    ControlSource,
    PlausibilityReason,
    Reading,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global state — set during lifespan startup
# ---------------------------------------------------------------------------

_engine: Optional[TwinEngine] = None
_mqtt_client: Optional[mqtt.Client] = None
_ws_clients: set[WebSocket] = set()
_ws_lock = asyncio.Lock()
_latest_state: dict = {}
_event_loop: Optional[asyncio.AbstractEventLoop] = None

CONFIG_PATH = "assets/config.json"


# ---------------------------------------------------------------------------
# WebSocket client management
# ---------------------------------------------------------------------------

async def _broadcast(state_dict: dict) -> None:
    """Broadcast state update to all connected WebSocket clients."""
    global _latest_state
    _latest_state = state_dict
    message = json.dumps(state_dict, default=str)
    async with _ws_lock:
        dead = set()
        for ws in _ws_clients:
            try:
                await ws.send_text(message)
            except Exception:
                dead.add(ws)
        _ws_clients -= dead


def _schedule_broadcast(state_dict: dict) -> None:
    """Thread-safe bridge from subscriber thread to asyncio event loop."""
    if _event_loop is not None and not _event_loop.is_closed():
        asyncio.run_coroutine_threadsafe(
            _broadcast(state_dict), _event_loop
        )


# ---------------------------------------------------------------------------
# MQTT publish callback — injected into engine
# ---------------------------------------------------------------------------

def _publish(topic: str, payload: dict) -> None:
    """Publish a command to the MQTT broker."""
    if _mqtt_client is not None:
        _mqtt_client.publish(topic, json.dumps(payload))
        log.info(f"MQTT publish → {topic}: {payload}")
    else:
        log.warning(f"MQTT client not ready — cannot publish to {topic}")


# ---------------------------------------------------------------------------
# Database persist callback — injected into engine
# ---------------------------------------------------------------------------

_db_conn = None


def _db_persist(reading, state, cycle_events, observation, filter_alerts) -> None:
    """
    Persist cycle outputs to database.
    Called by engine.py after each processing cycle.
    reading is already persisted by mqtt_subscriber — this handles
    events, transitions, and performance observations only.
    """
    global _db_conn
    if _db_conn is None:
        return

    try:
        for event_type, event in cycle_events:
            if event_type == "regime_transition":
                _db_conn.execute(
                    """INSERT INTO state_transitions
                       (ts, from_regime, to_regime, duration_sec, reason)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        event.ts,
                        str(event.from_regime),
                        str(event.to_regime),
                        int(event.duration_minutes * 60),
                        event.reason,
                    ),
                )
            elif event_type == "spike":
                _db_conn.execute(
                    """INSERT INTO events
                       (ts, event_type, detail)
                       VALUES (?, 'spike', ?)""",
                    (
                        event.ts_start,
                        json.dumps({
                            "peak_value": event.peak_value,
                            "duration_minutes": event.duration_minutes,
                            "fan_speed_response": event.fan_speed_response,
                        }),
                    ),
                )
            elif event_type == "escalation":
                _db_conn.execute(
                    """INSERT INTO escalation_events
                       (ts_raised, escalation_type, detail, resolved)
                       VALUES (?, 'sustained_elevation', ?, 0)""",
                    (
                        event.ts_escalated,
                        json.dumps({
                            "duration_minutes": event.duration_minutes,
                            "current_value": event.current_value,
                            "regime": str(event.regime),
                        }),
                    ),
                )
            elif event_type == "uncommanded_state_change":
                _db_conn.execute(
                    """INSERT INTO events
                       (ts, event_type, detail)
                       VALUES (?, 'uncommanded_state_change', ?)""",
                    (
                        event.ts,
                        json.dumps({
                            "previous_speed": event.previous_speed,
                            "new_speed": event.new_speed,
                            "previous_state": event.previous_state,
                            "new_state": event.new_state,
                        }),
                    ),
                )

        if observation is not None:
            _db_conn.execute(
                """INSERT INTO performance_observations
                   (ts_start, ts_peak, ts_end, fan_speed,
                    observed_decay_rate, expected_decay_rate,
                    performance_ratio, filter_type, twin_filter_age_hours)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    observation.ts_start,
                    observation.ts_peak,
                    observation.ts_end,
                    observation.fan_speed,
                    observation.observed_decay_rate,
                    observation.expected_decay_rate,
                    observation.performance_ratio,
                    str(observation.filter_type),
                    observation.twin_filter_age_hours,
                ),
            )

        _db_conn.commit()

    except Exception as e:
        log.error(f"DB persist error: {e}")


# ---------------------------------------------------------------------------
# Reading builder — converts merged MQTT dict to Reading dataclass
# ---------------------------------------------------------------------------

def _build_reading(merged: dict) -> Reading:
    """
    Convert the merged dict from mqtt_subscriber._handle_pm25()
    to a typed Reading dataclass for the twin engine.
    """
    is_plausible_raw = merged.get("is_plausible")
    if is_plausible_raw is None:
        is_plausible = None
    else:
        is_plausible = bool(is_plausible_raw)

    reason_raw = merged.get("plausibility_reason", "warmup")
    try:
        plausibility_reason = PlausibilityReason(reason_raw)
    except ValueError:
        plausibility_reason = PlausibilityReason.WARMUP

    purifier_on_raw = merged.get("purifier_on")
    if purifier_on_raw is None:
        purifier_on = None
    else:
        purifier_on = bool(purifier_on_raw)

    return Reading(
        ts=merged.get("timestamp", datetime.now(timezone.utc).isoformat()),
        value=float(merged.get("value") or 0.0),
        is_warmup=bool(merged.get("is_warmup", False)),
        changed=bool(merged.get("changed", True)),
        is_plausible=is_plausible,
        plausibility_reason=plausibility_reason,
        rolling_mean=float(merged.get("rolling_mean") or 0.0),
        rolling_std=float(merged.get("rolling_std") or 0.0),
        trend_slope=float(merged.get("trend_slope") or 0.0),
        purifier_on=purifier_on,
        fan_speed=merged.get("fan_speed"),
        fan_mode=merged.get("fan_mode"),
        filter_age=merged.get("filter_age"),
        filter_age_unit=merged.get("filter_age_unit", "minutes"),
        device_age=merged.get("device_age"),
        device_age_unit=merged.get("device_age_unit", "minutes"),
        pm25_internal=merged.get("pm25_internal"),
        linkquality=merged.get("linkquality"),
        control_source=None,
    )


# ---------------------------------------------------------------------------
# Extended MQTT subscriber with engine hook
# ---------------------------------------------------------------------------

from backend.mqtt_subscriber import MQTTSubscriber


class EnginedSubscriber(MQTTSubscriber):
    """
    Extends MQTTSubscriber to call the twin engine after each merged reading.
    The engine receives a typed Reading and processes the full cycle.
    Database persistence of the raw reading is handled by the parent class.
    """

    def __init__(self, engine: TwinEngine):
        super().__init__()
        self._engine = engine

    def _handle_pm25(self, payload: dict):
        """Override parent to hook engine after persist."""
        super()._handle_pm25(payload)

        # Build Reading and call engine
        try:
            import threading as _threading
            with self._lock:
                purifier = dict(self._purifier_state)

            merged = {
                "timestamp":           payload.get("timestamp"),
                "value":               payload.get("value"),
                "is_warmup":           payload.get("is_warmup", False),
                "changed":             payload.get("changed", True),
                "is_plausible":        payload.get("is_plausible"),
                "plausibility_reason": payload.get("plausibility_reason"),
                "rolling_mean":        payload.get("rolling_mean"),
                "rolling_std":         payload.get("rolling_std"),
                "trend_slope":         payload.get("trend_slope"),
                "purifier_on":         1 if purifier.get("fan_state") == "ON"
                                       else (0 if purifier.get("fan_state") == "OFF"
                                             else None),
                "fan_speed":           purifier.get("fan_speed"),
                "fan_mode":            purifier.get("fan_mode"),
                "filter_age":          purifier.get("filter_age"),
                "filter_age_unit":     purifier.get("filter_age_unit", "minutes"),
                "device_age":          purifier.get("device_age"),
                "device_age_unit":     purifier.get("device_age_unit", "minutes"),
                "pm25_internal":       purifier.get("pm25_internal"),
                "linkquality":         purifier.get("linkquality"),
                "control_source":      None,
            }

            reading = _build_reading(merged)
            self._engine.process_reading(reading)

        except Exception as e:
            log.error(f"Engine process_reading error: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# App lifespan — startup and shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _engine, _mqtt_client, _db_conn, _event_loop

    log.info("Air Twin backend starting up")
    _event_loop = asyncio.get_event_loop()

    # Initialise database
    init_db()
    _db_conn = get_connection()
    log.info("Database initialised")

    # Create engine with injected callbacks
    _engine = TwinEngine(
        config_path=CONFIG_PATH,
        publish_callback=_publish,
        db_persist=_db_persist,
        state_callback=_broadcast,
    )
    log.info("Twin engine created")

    # Create MQTT publish client (separate from subscriber)
    from paho.mqtt.enums import CallbackAPIVersion
    _mqtt_client = mqtt.Client(CallbackAPIVersion.VERSION2)
    _mqtt_client.connect("192.168.1.85", 1883, keepalive=60)
    _mqtt_client.loop_start()
    log.info("MQTT publish client connected")

    # Start subscriber with engine hook
    subscriber = EnginedSubscriber(_engine)
    subscriber.start()
    log.info("MQTT subscriber started")

    yield

    # Shutdown
    log.info("Air Twin backend shutting down")
    subscriber.stop()
    _mqtt_client.loop_stop()
    _mqtt_client.disconnect()
    if _db_conn:
        _db_conn.close()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Air Twin API",
    description="Digital twin backend for IKEA Starkvind air purifier",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """Liveness check."""
    return {
        "status": "ok",
        "ts": datetime.now(timezone.utc).isoformat(),
        "engine": _engine is not None,
    }


@app.get("/state")
def get_state():
    """Current full twin state."""
    if _engine is None:
        raise HTTPException(503, "Engine not ready")
    return _engine.get_state()


@app.get("/brief")
def get_brief():
    """Four-role brief generated from current twin state."""
    if _engine is None:
        raise HTTPException(503, "Engine not ready")

    state = _engine._state
    filter_status = _engine.get_filter_status()

    return brief_generator.generate(
        state=state,
        device_age_minutes=None,
        filter_age_minutes=None,
        filter_life_hours=4380,
        recent_regime_history=_get_recent_regime_history(),
        open_alerts=_get_open_alerts(),
    )


@app.get("/brief/{role}")
def get_brief_role(role: str):
    """Single role view from the brief."""
    valid_roles = {"executive", "operator", "engineer", "technician"}
    if role not in valid_roles:
        raise HTTPException(400, f"Invalid role. Valid roles: {valid_roles}")

    full_brief = get_brief()
    return full_brief[role]


@app.get("/regime")
def get_regime():
    """Regime summary for engineer view."""
    if _engine is None:
        raise HTTPException(503, "Engine not ready")
    return _engine.get_regime_summary()


@app.get("/confidence")
def get_confidence():
    """Confidence score and factor breakdown for engineer view."""
    if _engine is None:
        raise HTTPException(503, "Engine not ready")
    return {
        "score": _engine._state.confidence,
        "conclusion": _engine.get_confidence_conclusion(),
        "dominant_negative": _engine.get_dominant_negative_factor(),
        "factors": _engine.get_confidence_factors(),
    }


@app.get("/filter")
def get_filter():
    """Filter status."""
    if _engine is None:
        raise HTTPException(503, "Engine not ready")
    return _engine.get_filter_status()


# ---------------------------------------------------------------------------
# Maintenance routes
# ---------------------------------------------------------------------------

class FilterChangeRequest(BaseModel):
    device_age_minutes: int
    filter_type: str = "particle_only"
    actor: str = "operator"


class TechnicianResetRequest(BaseModel):
    device_age_minutes: int
    actor: str = "technician"


@app.post("/maintenance")
def log_filter_change(req: FilterChangeRequest):
    """
    Log a filter change maintenance event.
    Called by iPhone QR scan workflow.
    Triggers baseline re-learn (enters VALIDATING regime).
    """
    if _engine is None:
        raise HTTPException(503, "Engine not ready")

    _engine.on_filter_change(
        device_age_minutes=req.device_age_minutes,
        filter_type=req.filter_type,
        actor=req.actor,
    )

    # Log to maintenance_events table
    if _db_conn:
        _db_conn.execute(
            """INSERT INTO maintenance_events
               (ts, event_type, actor, notes)
               VALUES (?, 'filter_change', ?, ?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                req.actor,
                json.dumps({
                    "filter_type": req.filter_type,
                    "device_age_minutes": req.device_age_minutes,
                }),
            ),
        )
        _db_conn.commit()

    return {"status": "ok", "message": "Filter change logged — baseline re-learn initiated"}


@app.post("/maintenance/reset")
def technician_reset(req: TechnicianResetRequest):
    """
    Technician-initiated baseline reset without filter change.
    Use when baseline has drifted and needs re-establishment.
    """
    if _engine is None:
        raise HTTPException(503, "Engine not ready")

    _engine.on_technician_reset(
        device_age_minutes=req.device_age_minutes,
        actor=req.actor,
    )

    if _db_conn:
        _db_conn.execute(
            """INSERT INTO maintenance_events
               (ts, event_type, actor, notes)
               VALUES (?, 'technician_reset', ?, ?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                req.actor,
                json.dumps({"device_age_minutes": req.device_age_minutes}),
            ),
        )
        _db_conn.commit()

    return {"status": "ok", "message": "Technician reset logged — baseline re-initialising"}


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for live twin state updates.
    Sends current state immediately on connect, then streams
    updates after each processing cycle.
    """
    await websocket.accept()
    async with _ws_lock:
        _ws_clients.add(websocket)
    log.info(f"WebSocket client connected — {len(_ws_clients)} total")

    try:
        # Send current state immediately on connect
        if _latest_state:
            await websocket.send_text(json.dumps(_latest_state, default=str))

        # Keep connection alive — updates pushed by _broadcast()
        while True:
            await websocket.receive_text()

    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.debug(f"WebSocket error: {e}")
    finally:
        async with _ws_lock:
            _ws_clients.discard(websocket)
        log.info(f"WebSocket client disconnected — {len(_ws_clients)} remaining")


# ---------------------------------------------------------------------------
# Internal — database helpers for brief generation
# ---------------------------------------------------------------------------

def _get_recent_regime_history(limit: int = 20) -> list:
    """Fetch recent regime transitions from database."""
    if _db_conn is None:
        return []
    try:
        rows = _db_conn.execute(
            """SELECT ts, from_regime, to_regime, duration_minutes, reason
               FROM state_transitions
               ORDER BY ts DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _get_open_alerts() -> list:
    """Fetch unresolved escalation events from database."""
    if _db_conn is None:
        return []
    try:
        rows = _db_conn.execute(
            """SELECT * FROM open_escalations"""
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )