"""
events.py — Event detection for the Air Twin twin engine.

Owns all logic related to detecting and classifying events:
  - Spike detection: transient PM2.5 exceedance that resolves quickly
  - Sustained elevation: exceedance that doesn't resolve → regime change trigger
  - Uncommanded state changes: fan state/speed changed without twin command
  - Command acknowledgement: verify purifier responded to twin command
  - Escalation: non-resolving degraded events that require operator awareness

Events are returned to engine.py which writes them to the appropriate
database tables (events, escalation_events, control_log).

No I/O — never reads or writes files directly. engine.py is the sole
I/O boundary.

Relationship to regime.py:
  - Spike events do NOT change regime — twin watches for resolution
  - Sustained elevation that doesn't resolve → regime.py transitions to DEGRADED
  - Events module detects and classifies; regime module owns state transitions
"""

import logging
from dataclasses import dataclass
from typing import Optional

from backend.twin_engine.models import (
    ControlSource,
    Reading,
    RegimeType,
    TwinState,
    utc_now,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event dataclasses — returned to engine.py for database persistence
# ---------------------------------------------------------------------------

@dataclass
class SpikeEvent:
    """A transient PM2.5 exceedance that resolved within the spike window."""
    ts_start:        str
    ts_peak:         str
    ts_resolved:     str
    peak_value:      float
    baseline_locked: float
    duration_minutes: float
    fan_speed_response: Optional[int]  # speed commanded during response


@dataclass
class EscalationEvent:
    """
    A degraded condition that has persisted beyond escalation_awareness_minutes.
    Requires operator awareness.
    """
    ts_start:         str
    ts_escalated:     str
    current_value:    float
    baseline_locked:  float
    duration_minutes: float
    regime:           RegimeType


@dataclass
class UncommandedStateChange:
    """
    Fan state or speed changed without a twin engine command.
    Cannot distinguish thermal protection, manual knob, IKEA app,
    or another Zigbee controller — reported honestly.
    """
    ts:               str
    previous_speed:   Optional[int]
    new_speed:        Optional[int]
    previous_state:   Optional[str]   # ON / OFF
    new_state:        Optional[str]
    child_lock_on:    Optional[bool]  # True narrows cause (rules out manual knob)
    cause_confirmed:  str             # always "unconfirmed" until operator responds


@dataclass
class CommandAcknowledgement:
    """Result of a command acknowledgement check."""
    ts:               str
    commanded_speed:  int
    reported_speed:   Optional[int]
    acknowledged:     bool
    retry_attempted:  bool


# ---------------------------------------------------------------------------
# Module-level spike tracker — keyed by asset_id
# ---------------------------------------------------------------------------

class _SpikeTracker:
    """Tracks an in-progress spike event."""
    __slots__ = [
        "ts_start", "ts_peak", "peak_value",
        "minutes_active", "fan_speed_response"
    ]

    def __init__(self, ts_start, initial_value):
        self.ts_start          = ts_start
        self.ts_peak           = ts_start
        self.peak_value        = initial_value
        self.minutes_active    = 0.0
        self.fan_speed_response = None


_spike_trackers:     dict[str, Optional[_SpikeTracker]] = {}
_escalation_timers:  dict[str, float] = {}   # minutes in DEGRADED per asset
_last_known_speed:   dict[str, Optional[int]] = {}
_last_known_state:   dict[str, Optional[str]] = {}
_pending_ack:        dict[str, Optional[dict]] = {}  # asset → {speed, ts, retried}


# ---------------------------------------------------------------------------
# Public — spike detection (called in BASELINE regime)
# ---------------------------------------------------------------------------

def on_baseline_reading(
    state: TwinState,
    reading: Reading,
    asset_id: str,
    deviation_from_locked: Optional[float],
    spike_entry_std_multiplier: float,
    spike_resolution_window_minutes: float,
) -> tuple[Optional[SpikeEvent], bool]:
    """
    Monitor for spike events while in BASELINE regime.

    A spike is a transient exceedance that resolves within
    spike_resolution_window_minutes. If it does not resolve,
    regime.py will eventually transition to DEGRADED.

    Args:
        state:                          Current TwinState
        reading:                        Current reading
        asset_id:                       Asset identifier
        deviation_from_locked:          Current deviation in std units
        spike_entry_std_multiplier:     Std devs above baseline to start tracking
        spike_resolution_window_minutes: Minutes before spike escalates to regime change

    Returns:
        Tuple of:
          - SpikeEvent if a spike just resolved, else None
          - bool: True if spike is currently active (purifier response warranted)
    """
    if asset_id not in _spike_trackers:
        _spike_trackers[asset_id] = None

    tracker = _spike_trackers[asset_id]
    above_threshold = (
        deviation_from_locked is not None
        and deviation_from_locked > spike_entry_std_multiplier
    )

    if above_threshold:
        if tracker is None:
            # Spike starting
            _spike_trackers[asset_id] = _SpikeTracker(
                ts_start=reading.ts,
                initial_value=reading.value,
            )
            tracker = _spike_trackers[asset_id]
            logger.info(f"[{asset_id}] Spike detected — "
                        f"value={reading.value:.1f}, "
                        f"deviation={deviation_from_locked:.2f} std")

        # Update tracker
        tracker.minutes_active += 1.0 / 60.0
        if reading.value > tracker.peak_value:
            tracker.peak_value = reading.value
            tracker.ts_peak    = reading.ts
        if reading.fan_speed is not None:
            tracker.fan_speed_response = reading.fan_speed

        return None, True  # spike active, no resolution yet

    else:
        if tracker is not None:
            # Spike resolved
            event = SpikeEvent(
                ts_start=tracker.ts_start,
                ts_peak=tracker.ts_peak,
                ts_resolved=reading.ts,
                peak_value=tracker.peak_value,
                baseline_locked=state.baseline_locked or 0.0,
                duration_minutes=tracker.minutes_active,
                fan_speed_response=tracker.fan_speed_response,
            )
            _spike_trackers[asset_id] = None
            logger.info(f"[{asset_id}] Spike resolved — "
                        f"peak={event.peak_value:.1f}, "
                        f"duration={event.duration_minutes:.1f} min")
            return event, False

        return None, False  # no spike active


def clear_spike(asset_id: str) -> None:
    """
    Clear spike tracker without logging resolution.
    Called by engine.py when regime transitions to DEGRADED — the spike
    did not resolve and is no longer classified as a spike.
    """
    _spike_trackers[asset_id] = None
    logger.debug(f"[{asset_id}] Spike tracker cleared — "
                 f"escalated to regime change")


# ---------------------------------------------------------------------------
# Public — escalation tracking (called in DEGRADED regime)
# ---------------------------------------------------------------------------

def on_degraded_reading(
    state: TwinState,
    reading: Reading,
    asset_id: str,
    escalation_awareness_minutes: float,
) -> Optional[EscalationEvent]:
    """
    Track time spent in DEGRADED regime.
    Emit EscalationEvent when escalation_awareness_minutes is exceeded.
    Emitted once per degraded episode — not on every reading.

    Args:
        state:                        Current TwinState
        reading:                      Current reading
        asset_id:                     Asset identifier
        escalation_awareness_minutes: Minutes in DEGRADED before operator alert

    Returns:
        EscalationEvent if threshold just crossed, else None
    """
    if asset_id not in _escalation_timers:
        _escalation_timers[asset_id] = 0.0

    prev_minutes = _escalation_timers[asset_id]
    _escalation_timers[asset_id] += 1.0 / 60.0
    curr_minutes = _escalation_timers[asset_id]

    # Emit escalation event exactly once when threshold is crossed
    if (prev_minutes < escalation_awareness_minutes
            <= curr_minutes):
        event = EscalationEvent(
            ts_start=state.regime_entered_ts or reading.ts,
            ts_escalated=reading.ts,
            current_value=reading.value,
            baseline_locked=state.baseline_locked or 0.0,
            duration_minutes=curr_minutes,
            regime=state.current_regime,
        )
        logger.warning(f"[{asset_id}] Escalation event — "
                       f"DEGRADED for {curr_minutes:.1f} min, "
                       f"value={reading.value:.1f}")
        return event

    return None


def reset_escalation_timer(asset_id: str) -> None:
    """Reset escalation timer on regime exit. Called by engine.py."""
    _escalation_timers[asset_id] = 0.0
    logger.debug(f"[{asset_id}] Escalation timer reset")


# ---------------------------------------------------------------------------
# Public — uncommanded state change detection
# ---------------------------------------------------------------------------

def check_uncommanded_state_change(
    reading: Reading,
    asset_id: str,
    state: TwinState,
) -> Optional[UncommandedStateChange]:
    """
    Detect fan state or speed changes the twin did not initiate.

    Compares current reading against last_fan_speed_commanded in TwinState.
    Any change not matching the last twin command is flagged as uncommanded.

    child_lock status narrows the cause:
      child_lock ON + uncommanded change → manual knob ruled out
      child_lock OFF → could be manual knob, thermal, app, or other controller

    Cannot definitively identify cause — always reported as "unconfirmed".

    Args:
        reading:    Current reading with purifier state
        asset_id:   Asset identifier
        state:      Current TwinState (has last_fan_speed_commanded)

    Returns:
        UncommandedStateChange if detected, else None
    """
    if reading.fan_speed is None and reading.purifier_on is None:
        return None

    prev_speed = _last_known_speed.get(asset_id)
    prev_state = _last_known_state.get(asset_id)
    curr_speed = reading.fan_speed
    curr_state = "ON" if reading.purifier_on else "OFF"

    # Update last known
    _last_known_speed[asset_id] = curr_speed
    _last_known_state[asset_id] = curr_state

    # No previous state — first reading, nothing to compare
    if prev_speed is None and prev_state is None:
        return None

    speed_changed = (curr_speed != prev_speed and prev_speed is not None)
    state_changed = (curr_state != prev_state and prev_state is not None)

    if not speed_changed and not state_changed:
        return None

    # Check if this matches a twin command
    if (state.last_fan_speed_commanded is not None
            and curr_speed == state.last_fan_speed_commanded):
        return None  # matches commanded speed — not uncommanded

    # Uncommanded change detected
    event = UncommandedStateChange(
        ts=reading.ts,
        previous_speed=prev_speed,
        new_speed=curr_speed,
        previous_state=prev_state,
        new_state=curr_state,
        child_lock_on=None,  # not in reading payload — engine.py enriches if available
        cause_confirmed="unconfirmed",
    )

    logger.warning(f"[{asset_id}] Uncommanded state change — "
                   f"speed: {prev_speed} → {curr_speed}, "
                   f"state: {prev_state} → {curr_state}")
    return event


# ---------------------------------------------------------------------------
# Public — command acknowledgement
# ---------------------------------------------------------------------------

def register_command(
    asset_id: str,
    commanded_speed: int,
    ts: str,
) -> None:
    """
    Register a twin command for acknowledgement tracking.
    Called by engine.py immediately after publishing a fan speed command.

    Args:
        asset_id:        Asset identifier
        commanded_speed: Fan speed that was commanded
        ts:              UTC ISO8601 timestamp of command
    """
    _pending_ack[asset_id] = {
        "speed":   commanded_speed,
        "ts":      ts,
        "retried": False,
    }
    logger.debug(f"[{asset_id}] Command registered for ack: speed={commanded_speed}")


def check_acknowledgement(
    reading: Reading,
    asset_id: str,
    ack_timeout_s: float,
    current_ts_epoch: float,
    command_ts_epoch: float,
) -> Optional[CommandAcknowledgement]:
    """
    Check if a pending command has been acknowledged by the purifier.

    Called each reading cycle when a command is pending.
    Returns CommandAcknowledgement when timeout is reached or speed confirmed.

    Args:
        reading:          Current reading
        asset_id:         Asset identifier
        ack_timeout_s:    Seconds to wait for acknowledgement
        current_ts_epoch: Current time as epoch float
        command_ts_epoch: Command issue time as epoch float

    Returns:
        CommandAcknowledgement if timeout reached or confirmed, else None
    """
    pending = _pending_ack.get(asset_id)
    if pending is None:
        return None

    commanded_speed = pending["speed"]
    elapsed = current_ts_epoch - command_ts_epoch

    # Check if purifier reported the commanded speed
    if reading.fan_speed == commanded_speed:
        _pending_ack[asset_id] = None
        ack = CommandAcknowledgement(
            ts=reading.ts,
            commanded_speed=commanded_speed,
            reported_speed=reading.fan_speed,
            acknowledged=True,
            retry_attempted=pending["retried"],
        )
        logger.debug(f"[{asset_id}] Command acknowledged: speed={commanded_speed}")
        return ack

    # Check timeout
    if elapsed >= ack_timeout_s:
        if not pending["retried"]:
            # First timeout — flag for retry (engine.py retries)
            pending["retried"] = True
            logger.warning(f"[{asset_id}] Command not acknowledged after "
                           f"{elapsed:.0f}s — retry warranted")
            return CommandAcknowledgement(
                ts=reading.ts,
                commanded_speed=commanded_speed,
                reported_speed=reading.fan_speed,
                acknowledged=False,
                retry_attempted=False,
            )
        else:
            # Second timeout after retry — flag unresponsive
            _pending_ack[asset_id] = None
            logger.error(f"[{asset_id}] Command unresponsive after retry: "
                         f"commanded={commanded_speed}, "
                         f"reported={reading.fan_speed}")
            return CommandAcknowledgement(
                ts=reading.ts,
                commanded_speed=commanded_speed,
                reported_speed=reading.fan_speed,
                acknowledged=False,
                retry_attempted=True,
            )

    return None  # still waiting within timeout window


def clear_pending_command(asset_id: str) -> None:
    """Clear pending command without acknowledgement check. Used on regime reset."""
    _pending_ack[asset_id] = None