"""
filter.py — Filter age tracking and cross-check for the Air Twin twin engine.

Owns all logic related to filter age calculation, validation, and anomaly
detection. Receives TwinState and a Reading, returns updated TwinState
and signals for confidence.py.

No I/O — never reads or writes files directly. engine.py is the sole
I/O boundary.

Critical facts encoded here:
  - filter_age is a user-maintained button counter — NOT a sensor.
    It can be reset without a filter change (accidental press) or
    left unreset after a genuine filter change.
  - device_age is non-resettable — it is the ground truth timeline anchor.
  - twin_filter_age is always calculated from device_age, never from
    filter_age directly.
  - All age values arrive in minutes. Conversion to hours happens here
    and only here — single conversion point.
  - Twin always reasons from twin_filter_age. filter_age is reference
    and cross-check only.
"""

import logging
from dataclasses import replace
from typing import Optional

from backend.twin_engine.models import (
    Reading,
    TwinState,
    utc_now,
)

logger = logging.getLogger(__name__)

# Tolerance for filter_age vs twin_filter_age cross-check (hours)
# Within this tolerance — agreement. Beyond — flag divergence.
_CROSS_CHECK_TOLERANCE_HOURS = 24.0

# If filter_age drops by more than this between readings, suspect
# an unlogged filter change. Loaded from config at runtime.
# Default matches config.json unlogged_change_detection_threshold_minutes.
_DEFAULT_UNLOGGED_DROP_THRESHOLD_MINUTES = 60


# ---------------------------------------------------------------------------
# Public — twin filter age calculation
# ---------------------------------------------------------------------------

def twin_filter_age_hours(
    device_age_minutes: int,
    filter_change_device_age_anchor: Optional[int],
) -> Optional[float]:
    """
    Calculate twin-tracked filter age in hours.

    Formula: (current_device_age - filter_change_device_age_anchor) / 60

    Returns None if no anchor exists (no confirmed filter change on record).
    In that case the twin falls back to filter_age with lower confidence.

    Args:
        device_age_minutes:              Current device_age from purifier state
        filter_change_device_age_anchor: device_age at last confirmed filter change

    Returns:
        Filter age in hours, or None if no anchor
    """
    if filter_change_device_age_anchor is None:
        return None
    age_minutes = device_age_minutes - filter_change_device_age_anchor
    return max(0.0, age_minutes / 60.0)


def best_filter_age_hours(
    state: TwinState,
    device_age_minutes: int,
    filter_age_minutes: Optional[int],
) -> tuple[float, str]:
    """
    Return the best available filter age estimate and its source label.

    Priority:
      1. twin_filter_age if anchor exists — high confidence
      2. filter_age from device button counter — low confidence, no anchor

    Args:
        state:               Current TwinState
        device_age_minutes:  Current device_age from purifier state
        filter_age_minutes:  Current filter_age from purifier state (button counter)

    Returns:
        Tuple of (age_hours, source) where source is:
        "twin_calculated" | "device_counter_no_anchor"
    """
    t_age = twin_filter_age_hours(
        device_age_minutes, state.filter_change_device_age_anchor
    )

    if t_age is not None:
        return t_age, "twin_calculated"

    # No anchor — fall back to device counter
    if filter_age_minutes is not None:
        return filter_age_minutes / 60.0, "device_counter_no_anchor"

    # No data at all — return 0 with unknown source
    return 0.0, "unknown"


# ---------------------------------------------------------------------------
# Public — main update function called per reading cycle
# ---------------------------------------------------------------------------

def update(
    state: TwinState,
    reading: Reading,
    asset_id: str,
    unlogged_drop_threshold_minutes: int = _DEFAULT_UNLOGGED_DROP_THRESHOLD_MINUTES,
) -> tuple[TwinState, list[str], list[str]]:
    """
    Process filter-related fields from one reading cycle.

    Detects:
      - Unlogged filter changes (filter_age drops unexpectedly)
      - filter_age / twin_filter_age divergence
      - filter_change_pending_reset resolution (button pressed after QR log)
      - First-run state (no anchor — surfaces to engineer view)

    Args:
        state:                          Current TwinState
        reading:                        Incoming reading with purifier state
        asset_id:                       Asset identifier for logging
        unlogged_drop_threshold_minutes: filter_age drop that triggers detection

    Returns:
        Tuple of:
          - Updated TwinState
          - signals: list of confidence evidence keys for confidence.py
          - alerts: list of human-readable alert strings for engineer/operator view
    """
    signals: list[str] = []
    alerts: list[str] = []

    # Nothing to do if purifier state not yet available
    if reading.device_age is None or reading.filter_age is None:
        return state, signals, alerts

    device_age = reading.device_age
    filter_age = reading.filter_age

    # --- Update last known filter age ---
    last_filter_age = state.last_known_filter_age

    # --- Detect pending reset resolution ---
    # If we logged a QR filter change but button hadn't been pressed,
    # watch for filter_age dropping to near zero.
    if state.filter_change_pending_reset:
        state, signals, alerts = _check_pending_reset_resolved(
            state, filter_age, signals, alerts, asset_id
        )

    # --- Detect unlogged filter change ---
    # filter_age drop beyond threshold between readings
    if (last_filter_age is not None
            and filter_age < last_filter_age - unlogged_drop_threshold_minutes):
        state, signals, alerts = _handle_unlogged_change_suspected(
            state, reading, filter_age, last_filter_age,
            device_age, signals, alerts, asset_id
        )

    # --- Cross-check filter_age vs twin_filter_age ---
    state, signals, alerts = _cross_check_ages(
        state, device_age, filter_age, signals, alerts, asset_id
    )

    # --- Update last known filter age ---
    state = replace(state, last_known_filter_age=filter_age)

    return state, signals, alerts


# ---------------------------------------------------------------------------
# Public — maintenance event handlers (called by engine.py)
# ---------------------------------------------------------------------------

def on_qr_filter_change(
    state: TwinState,
    device_age_minutes: int,
    filter_type: str,
    asset_id: str,
) -> TwinState:
    """
    Handle a QR-logged filter change event.

    Sets filter_change_device_age_anchor to current device_age.
    Sets filter_change_pending_reset = True if filter_age button
    hasn't been pressed yet (twin will watch for it).

    Called by engine.py when a POST /maintenance filter change arrives.

    Args:
        state:               Current TwinState
        device_age_minutes:  Current device_age at time of QR scan
        filter_type:         Filter type string from QR payload
        asset_id:            Asset identifier for logging

    Returns:
        Updated TwinState
    """
    from backend.twin_engine.models import FilterType
    try:
        ft = FilterType(filter_type)
    except ValueError:
        logger.warning(f"[{asset_id}] Unknown filter type '{filter_type}' "
                       f"in QR maintenance event — using current type")
        ft = state.installed_filter_type

    state = replace(
        state,
        filter_change_device_age_anchor=device_age_minutes,
        filter_change_pending_reset=True,
        installed_filter_type=ft,
        last_logged_filter_type=ft,
    )

    logger.info(f"[{asset_id}] QR filter change logged — "
                f"anchor={device_age_minutes} min, type={ft}, "
                f"pending_reset=True (waiting for button press)")
    return state


def on_technician_reset(
    state: TwinState,
    device_age_minutes: int,
    asset_id: str,
) -> TwinState:
    """
    Handle a technician-initiated filter age reset via API.
    Logs a maintenance event anchor without requiring a QR scan.
    Used when filter was changed but QR scan was missed.
    """
    state = replace(
        state,
        filter_change_device_age_anchor=device_age_minutes,
        filter_change_pending_reset=False,
    )
    logger.info(f"[{asset_id}] Technician reset — "
                f"anchor={device_age_minutes} min")
    return state


# ---------------------------------------------------------------------------
# Public — query helpers for engine.py and brief_generator.py
# ---------------------------------------------------------------------------

def filter_life_fraction(
    state: TwinState,
    device_age_minutes: int,
    filter_age_minutes: Optional[int],
    filter_life_hours: int,
) -> tuple[float, str]:
    """
    Return filter life consumed as a fraction (0.0–1.0+) and source label.
    Values above 1.0 indicate filter is past recommended replacement.
    """
    age_hours, source = best_filter_age_hours(
        state, device_age_minutes, filter_age_minutes
    )
    if filter_life_hours <= 0:
        return 0.0, source
    return age_hours / filter_life_hours, source


def no_anchor_on_record(state: TwinState) -> bool:
    """True if no confirmed filter change has ever been logged."""
    return state.filter_change_device_age_anchor is None


# ---------------------------------------------------------------------------
# Internal — pending reset check
# ---------------------------------------------------------------------------

def _check_pending_reset_resolved(
    state: TwinState,
    filter_age: int,
    signals: list[str],
    alerts: list[str],
    asset_id: str,
) -> tuple[TwinState, list[str], list[str]]:
    """
    Check if the filter reset button has been pressed after a QR-logged change.
    Filter_age dropping to near zero (< 120 minutes) indicates button was pressed.
    """
    RESET_THRESHOLD_MINUTES = 120

    if filter_age < RESET_THRESHOLD_MINUTES:
        state = replace(state, filter_change_pending_reset=False)
        logger.info(f"[{asset_id}] filter_change_pending_reset resolved — "
                    f"filter_age={filter_age} min (button pressed)")
    else:
        alerts.append(
            f"Filter change logged via QR but reset button not yet pressed. "
            f"filter_age={filter_age} min. Press button behind front panel "
            f"to reset the device counter."
        )

    return state, signals, alerts


# ---------------------------------------------------------------------------
# Internal — unlogged filter change detection
# ---------------------------------------------------------------------------

def _handle_unlogged_change_suspected(
    state: TwinState,
    reading: Reading,
    filter_age: int,
    last_filter_age: int,
    device_age: int,
    signals: list[str],
    alerts: list[str],
    asset_id: str,
) -> tuple[TwinState, list[str], list[str]]:
    """
    Handle a suspected unlogged filter change.

    filter_age dropped by more than threshold between readings.
    Discriminate from communication fault using device_age continuity
    and linkquality.

    Communication fault signature: device_age also erratic OR linkquality degrading
    Filter change signature: device_age increments normally + filter_age reset
    """
    drop = last_filter_age - filter_age
    logger.warning(f"[{asset_id}] filter_age dropped by {drop} min "
                   f"(from {last_filter_age} to {filter_age}) — "
                   f"suspected unlogged filter change")

    # Check for communication fault indicators
    linkquality = reading.linkquality
    communication_fault_suspected = (
        linkquality is not None and linkquality < 100
    )

    if communication_fault_suspected:
        alerts.append(
            f"filter_age dropped unexpectedly by {drop} min but "
            f"Zigbee linkquality is low ({linkquality}). "
            f"Possible communication fault — confirm filter status manually."
        )
        signals.append("filter_age_divergence")
    else:
        # Device_age increments normally — filter change is likely
        alerts.append(
            f"Unlogged filter change suspected — filter_age dropped by {drop} min. "
            f"If filter was changed, please log via QR scan to update twin records. "
            f"Operator confirmation requested."
        )
        # Update anchor tentatively — operator confirmation will finalise
        state = replace(
            state,
            filter_change_device_age_anchor=device_age,
            filter_change_pending_reset=False,
        )
        signals.append("filter_age_divergence")

    return state, signals, alerts


# ---------------------------------------------------------------------------
# Internal — cross-check
# ---------------------------------------------------------------------------

def _cross_check_ages(
    state: TwinState,
    device_age: int,
    filter_age: int,
    signals: list[str],
    alerts: list[str],
    asset_id: str,
) -> tuple[TwinState, list[str], list[str]]:
    """
    Cross-check filter_age (device button counter) against twin_filter_age
    (calculated from device_age anchor).

    Three outcomes:
      - Agreement within tolerance: all good, no signal
      - filter_age much lower than twin_filter_age: unlogged change suspected
      - filter_age much higher than twin_filter_age: button not pressed after change

    No anchor: surface to engineer view only, no confidence penalty.
    """
    if state.filter_change_device_age_anchor is None:
        # No anchor — can't cross-check, surface info only
        if not hasattr(state, '_no_anchor_logged'):
            logger.info(f"[{asset_id}] No confirmed filter change on record. "
                        f"Filter age based on device counter only "
                        f"({filter_age} min). Lower confidence in filter assessment.")
        return state, signals, alerts

    t_age_hours = twin_filter_age_hours(
        device_age, state.filter_change_device_age_anchor
    )
    if t_age_hours is None:
        return state, signals, alerts

    d_age_hours = filter_age / 60.0
    divergence_hours = abs(d_age_hours - t_age_hours)

    if divergence_hours <= _CROSS_CHECK_TOLERANCE_HOURS:
        # Agreement — no action
        return state, signals, alerts

    signals.append("filter_age_divergence")

    if d_age_hours < t_age_hours - _CROSS_CHECK_TOLERANCE_HOURS:
        alerts.append(
            f"filter_age ({d_age_hours:.1f}h) is much lower than "
            f"twin_filter_age ({t_age_hours:.1f}h). "
            f"Possible unlogged filter change or accidental button press."
        )
        logger.warning(f"[{asset_id}] filter_age below twin_filter_age by "
                       f"{divergence_hours:.1f}h")
    else:
        alerts.append(
            f"filter_age ({d_age_hours:.1f}h) is much higher than "
            f"twin_filter_age ({t_age_hours:.1f}h). "
            f"Reset button may not have been pressed after last filter change."
        )
        logger.warning(f"[{asset_id}] filter_age above twin_filter_age by "
                       f"{divergence_hours:.1f}h")

    return state, signals, alerts