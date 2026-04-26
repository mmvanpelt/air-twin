"""
baseline.py — Baseline management for the Air Twin twin engine.

Owns all logic related to baseline_locked and baseline_current.
Receives TwinState and a Reading, returns an updated TwinState.
No I/O — never reads or writes files directly. engine.py is the
sole I/O boundary.

Two references are maintained simultaneously:
  - baseline_locked: hard reference for regime decisions. Set at
    initialisation. Only reset on maintenance event or technician
    reset command. Never updated by normal operation.
  - baseline_current: slow EMA of recent readings. Trend context
    only. Never used for regime decisions directly. Divergence from
    baseline_locked is itself a confidence signal.

Baseline learning uses ONLY readings where plausibility_reason == "ok".
window_filling readings are excluded — the Pi had insufficient history
to fully validate them.
"""

import logging
import statistics
from collections import deque
from dataclasses import replace
from typing import Optional

from backend.twin_engine.models import (
    PlausibilityReason,
    Reading,
    RegimeType,
    Season,
    TwinState,
    season_from_month,
    utc_now,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level rolling buffer for baseline learning
# Keyed by asset_id to support multiple assets in future.
# Cleared on regime transition to INITIALISING or VALIDATING.
# ---------------------------------------------------------------------------
_learn_buffers: dict[str, deque] = {}


def _get_learn_buffer(asset_id: str, maxlen: int) -> deque:
    if asset_id not in _learn_buffers:
        _learn_buffers[asset_id] = deque(maxlen=maxlen)
    return _learn_buffers[asset_id]


def clear_learn_buffer(asset_id: str) -> None:
    """Clear the learning buffer for an asset. Called by engine.py on
    maintenance events and technician reset commands."""
    if asset_id in _learn_buffers:
        _learn_buffers[asset_id].clear()
        logger.info(f"[{asset_id}] Baseline learn buffer cleared")


# ---------------------------------------------------------------------------
# Public — called once per reading cycle by engine.py
# ---------------------------------------------------------------------------

def update(
    state: TwinState,
    reading: Reading,
    asset_id: str,
    min_readings_to_lock: int,
    lock_variance_threshold_std: float,
    ema_alpha: float,
    rate_of_change_guard_ug_m3_per_hour: float,
) -> tuple[TwinState, list[str]]:
    """
    Process one reading and return updated baseline state.

    Args:
        state:                            Current TwinState
        reading:                          Incoming qualified reading
        asset_id:                         Asset identifier (for buffer keying)
        min_readings_to_lock:             Minimum qualifying readings before lock attempt
        lock_variance_threshold_std:      Max rolling std during learn window
        ema_alpha:                        EMA learning rate for baseline_current
        rate_of_change_guard_ug_m3_per_hour: RoC guard threshold

    Returns:
        Tuple of (updated TwinState, list of signal strings for confidence.py)
        Signals are string keys matching confidence evidence_weights in config.json.
    """
    signals: list[str] = []

    # Only qualifying readings contribute to baseline learning
    if not _is_qualifying(reading):
        return state, signals

    # --- Update baseline_current (slow EMA — trend context only) ---
    state = _update_baseline_current(state, reading, ema_alpha)

    # --- Baseline learning and lock attempt ---
    if state.baseline_locked is None:
        state, signals = _attempt_baseline_lock(
            state, reading, asset_id,
            min_readings_to_lock, lock_variance_threshold_std, signals
        )
    else:
        # --- Locked baseline — monitor for divergence ---
        state, signals = _monitor_divergence(
            state, reading, signals,
            rate_of_change_guard_ug_m3_per_hour
        )

    return state, signals


# ---------------------------------------------------------------------------
# Public — maintenance event / technician reset
# ---------------------------------------------------------------------------

def reset_for_maintenance(state: TwinState, asset_id: str) -> TwinState:
    """
    Reset baseline for a filter change or technician reset command.
    Clears baseline_locked so the twin re-learns from scratch.
    baseline_current is preserved as a warm starting point.
    Called by engine.py when a maintenance event is logged.
    """
    clear_learn_buffer(asset_id)
    state = replace(
        state,
        baseline_locked=None,
        baseline_locked_ts=None,
        baseline_locked_month=None,
        baseline_locked_season=None,
        baseline_std=None,
        baseline_learn_readings=0,
        baseline_learn_started_ts=None,
        current_regime=RegimeType.VALIDATING,
        regime_entered_ts=utc_now(),
        regime_duration_minutes=0.0,
    )
    logger.info(f"[{asset_id}] Baseline reset for maintenance — entering VALIDATING")
    return state


# ---------------------------------------------------------------------------
# Public — query helpers used by regime.py and confidence.py
# ---------------------------------------------------------------------------

def is_locked(state: TwinState) -> bool:
    """True if baseline_locked has been established."""
    return state.baseline_locked is not None


def deviation_from_locked(state: TwinState, value: float) -> Optional[float]:
    """
    Return how many baseline_std units the value is above baseline_locked.
    Positive = above locked baseline. Negative = below.
    Returns None if baseline not locked or std is zero/None.
    """
    if state.baseline_locked is None or state.baseline_std is None:
        return None
    if state.baseline_std == 0:
        return None
    return (value - state.baseline_locked) / state.baseline_std


def current_divergence(state: TwinState) -> Optional[float]:
    """
    Return divergence of baseline_current from baseline_locked in std units.
    Used by confidence.py as a sustained divergence signal.
    Returns None if either baseline is not established.
    """
    if state.baseline_current is None:
        return None
    return deviation_from_locked(state, state.baseline_current)


def season_mismatch(state: TwinState) -> bool:
    """
    True if the current season differs from the season at baseline lock.
    Used by confidence.py as a persistent small negative signal.
    """
    if state.baseline_locked_season is None:
        return False
    import datetime
    current_month = datetime.datetime.now(datetime.timezone.utc).month
    current_season = season_from_month(current_month)
    return current_season != state.baseline_locked_season


# ---------------------------------------------------------------------------
# Internal — qualifying reading check
# ---------------------------------------------------------------------------

def _is_qualifying(reading: Reading) -> bool:
    """
    A reading qualifies for baseline contribution if and only if
    plausibility_reason == "ok". window_filling is excluded — the Pi
    had insufficient history to fully validate those readings.
    """
    return (
        reading.is_plausible is True
        and reading.plausibility_reason == PlausibilityReason.OK
    )


# ---------------------------------------------------------------------------
# Internal — baseline_current EMA
# ---------------------------------------------------------------------------

def _update_baseline_current(
    state: TwinState,
    reading: Reading,
    ema_alpha: float,
) -> TwinState:
    """Update the slow EMA baseline_current from a qualifying reading."""
    if state.baseline_current is None:
        # First qualifying reading — seed directly
        new_current = reading.value
    else:
        new_current = (ema_alpha * reading.value
                       + (1.0 - ema_alpha) * state.baseline_current)
    return replace(state, baseline_current=new_current)


# ---------------------------------------------------------------------------
# Internal — baseline lock attempt
# ---------------------------------------------------------------------------

def _attempt_baseline_lock(
    state: TwinState,
    reading: Reading,
    asset_id: str,
    min_readings_to_lock: int,
    lock_variance_threshold_std: float,
    signals: list[str],
) -> tuple[TwinState, list[str]]:
    """
    Accumulate qualifying readings in the learn buffer.
    Attempt to lock baseline when min_readings_to_lock is reached
    and variance is within threshold.
    """
    buf = _get_learn_buffer(asset_id, maxlen=min_readings_to_lock)

    # Start tracking learn window timestamp
    if state.baseline_learn_started_ts is None:
        state = replace(state, baseline_learn_started_ts=utc_now())

    buf.append(reading.value)
    state = replace(state, baseline_learn_readings=len(buf))

    if len(buf) < min_readings_to_lock:
        logger.debug(f"[{asset_id}] Baseline learning: "
                     f"{len(buf)}/{min_readings_to_lock} qualifying readings")
        return state, signals

    # Buffer is full — check variance
    std = statistics.stdev(buf)
    if std > lock_variance_threshold_std:
        logger.info(f"[{asset_id}] Baseline lock attempt failed — "
                    f"std={std:.2f} > threshold={lock_variance_threshold_std}. "
                    f"Window not stable. Continuing to accumulate.")
        # Slide the window — oldest reading dropped by deque maxlen
        return state, signals

    # Variance acceptable — lock baseline using median (resistant to outliers)
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    locked_value = statistics.median(buf)

    state = replace(
        state,
        baseline_locked=locked_value,
        baseline_locked_ts=now.isoformat(),
        baseline_locked_month=now.month,
        baseline_locked_season=season_from_month(now.month),
        baseline_std=std if std > 0 else 0.1,  # floor to avoid division by zero
        baseline_learn_readings=len(buf),
    )

    logger.info(f"[{asset_id}] Baseline locked — "
                f"value={locked_value:.1f} µg/m³, std={std:.2f}, "
                f"season={state.baseline_locked_season}, "
                f"month={now.month}, "
                f"readings={len(buf)}")

    clear_learn_buffer(asset_id)
    return state, signals


# ---------------------------------------------------------------------------
# Internal — divergence monitoring (post-lock)
# ---------------------------------------------------------------------------

def _monitor_divergence(
    state: TwinState,
    reading: Reading,
    signals: list[str],
    rate_of_change_guard_ug_m3_per_hour: float,
) -> tuple[TwinState, list[str]]:
    """
    Monitor baseline_current for sustained divergence from baseline_locked
    and rate-of-change guard breaches. Emits signals for confidence.py.

    Both upward AND downward divergence are flagged:
      - Upward sustained divergence = genuine degradation or environmental change
      - Downward sustained divergence = genuine improvement OR sensor drift
    Both are surfaced to engineer role — twin does not silently absorb either.
    """
    div = current_divergence(state)

    if div is not None:
        # Sustained divergence in either direction
        DIVERGENCE_SIGNAL_THRESHOLD = 2.0  # std units
        if abs(div) > DIVERGENCE_SIGNAL_THRESHOLD:
            signals.append("baseline_divergence_sustained")
            direction = "above" if div > 0 else "below"
            logger.debug(f"Baseline divergence: current is {div:.1f} std {direction} locked")

    # Rate-of-change guard on baseline_current
    if state.baseline_current is not None and state.baseline_locked is not None:
        # trend_slope from Pi is µg/m³ per second — convert to per hour
        roc_per_hour = reading.trend_slope * 3600
        if roc_per_hour > rate_of_change_guard_ug_m3_per_hour:
            signals.append("roc_guard_breach")
            logger.warning(f"Rate-of-change guard breach: "
                           f"{roc_per_hour:.1f} µg/m³/hr > "
                           f"threshold {rate_of_change_guard_ug_m3_per_hour}")

    return state, signals