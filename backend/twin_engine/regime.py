"""
regime.py — Finite state machine regime management for the Air Twin twin engine.

Owns all logic related to regime transitions, entry/exit conditions, and
regime duration tracking. Receives TwinState, a Reading, and baseline query
results. Returns updated TwinState with any regime transition applied.

Regime definitions (from §12 of architecture spec):

  INITIALISING — First run, no baseline established. Entry: cold start or
                 unknown regime timeout. Exit: MIN_READINGS plausible readings
                 with stable window → handled by baseline.py locking baseline.

  VALIDATING   — Post-maintenance baseline re-learn. Entry: maintenance event
                 (filter change or technician reset). Exit: 12h stable window
                 with locked baseline.

  BASELINE     — Normal operation. Entry: baseline locked after INITIALISING
                 or VALIDATING. Exit: sustained exceedance or maintenance event.

  DEGRADED     — Sustained PM2.5 elevation above baseline. Entry: rolling mean
                 exceeds baseline_locked + (N × baseline_std) for M consecutive
                 minutes. Exit: return to baseline range for sustained period.

  UNKNOWN      — No readings beyond gap threshold. Entry: reading gap exceeds
                 gap_threshold_s. Exit: readings resume → re-enters INITIALISING
                 (does not resume baseline from stale state).

Regime transitions are logged to state_transitions table by engine.py.
This module emits transition events — it does not write to the database.

No I/O — never reads or writes files directly. engine.py is the sole
I/O boundary.
"""

import logging
from dataclasses import replace
from typing import Optional

from backend.twin_engine.models import (
    Reading,
    RegimeType,
    TwinState,
    utc_now,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Transition event — returned when a regime change occurs
# ---------------------------------------------------------------------------

class RegimeTransition:
    """
    Emitted by evaluate() when a regime change occurs.
    engine.py writes this to the state_transitions table.
    """
    __slots__ = [
        "from_regime", "to_regime", "ts",
        "duration_minutes", "reason"
    ]

    def __init__(self, from_regime, to_regime, ts, duration_minutes, reason):
        self.from_regime      = from_regime
        self.to_regime        = to_regime
        self.ts               = ts
        self.duration_minutes = duration_minutes
        self.reason           = reason

    def __repr__(self):
        return (f"RegimeTransition({self.from_regime} → {self.to_regime}, "
                f"duration={self.duration_minutes:.1f}min, reason={self.reason})")


# ---------------------------------------------------------------------------
# Module-level exceedance tracker
# Tracks consecutive minutes above threshold for degraded entry.
# Keyed by asset_id.
# ---------------------------------------------------------------------------

_exceedance_minutes: dict[str, float] = {}
_recovery_minutes:   dict[str, float] = {}


def reset_trackers(asset_id: str) -> None:
    """Reset exceedance and recovery trackers. Called on regime transition."""
    _exceedance_minutes[asset_id] = 0.0
    _recovery_minutes[asset_id]   = 0.0


# ---------------------------------------------------------------------------
# Public — main evaluation function called once per reading cycle
# ---------------------------------------------------------------------------

def evaluate(
    state: TwinState,
    reading: Reading,
    asset_id: str,
    baseline_locked: Optional[float],
    baseline_std: Optional[float],
    deviation_from_locked: Optional[float],
    gap_threshold_s: float,
    degraded_entry_std_multiplier: float,
    degraded_entry_duration_minutes: float,
    degraded_exit_duration_minutes: float,
    seconds_since_last_reading: float,
) -> tuple[TwinState, Optional[RegimeTransition]]:
    """
    Evaluate current regime and apply any warranted transition.

    Called every reading cycle. Returns updated TwinState and an optional
    RegimeTransition event if a transition occurred.

    Args:
        state:                           Current TwinState
        reading:                         Current reading
        asset_id:                        Asset identifier
        baseline_locked:                 Locked baseline value (µg/m³) or None
        baseline_std:                    Std at baseline lock time or None
        deviation_from_locked:           Current deviation in std units or None
        gap_threshold_s:                 Seconds before entering UNKNOWN
        degraded_entry_std_multiplier:   N std devs above baseline for degraded entry
        degraded_entry_duration_minutes: M consecutive minutes for degraded entry
        degraded_exit_duration_minutes:  Minutes in range before returning to baseline
        seconds_since_last_reading:      Time since last reading arrived

    Returns:
        Tuple of (updated TwinState, RegimeTransition or None)
    """
    current = state.current_regime

    # --- Check for reading gap → UNKNOWN ---
    if seconds_since_last_reading > gap_threshold_s:
        return _transition_if_needed(
            state, RegimeType.UNKNOWN, asset_id,
            reason=f"Reading gap of {seconds_since_last_reading:.0f}s "
                   f"exceeds threshold {gap_threshold_s:.0f}s"
        )

    # --- UNKNOWN → INITIALISING on reading resumption ---
    if current == RegimeType.UNKNOWN:
        return _transition_if_needed(
            state, RegimeType.INITIALISING, asset_id,
            reason="Readings resumed after gap — re-initialising, "
                   "not resuming from stale baseline"
        )

    # --- INITIALISING → BASELINE when baseline locks ---
    if current == RegimeType.INITIALISING:
        if baseline_locked is not None:
            return _transition_if_needed(
                state, RegimeType.BASELINE, asset_id,
                reason=f"Baseline locked at {baseline_locked:.1f} µg/m³ "
                       f"after {state.baseline_learn_readings} qualifying readings"
            )
        return state, None

    # --- VALIDATING → BASELINE when baseline re-locks ---
    if current == RegimeType.VALIDATING:
        if baseline_locked is not None:
            return _transition_if_needed(
                state, RegimeType.BASELINE, asset_id,
                reason=f"Post-maintenance baseline locked at {baseline_locked:.1f} µg/m³"
            )
        return state, None

    # --- BASELINE → EVENT on exceedance ---
    if current == RegimeType.BASELINE:
        return _evaluate_baseline(
            state, reading, asset_id,
            deviation_from_locked,
            degraded_entry_std_multiplier,
            degraded_entry_duration_minutes,
        )

    # --- EVENT → BASELINE (resolved) or DEGRADED (sustained) ---
    if current == RegimeType.EVENT:
        return _evaluate_event(
            state, reading, asset_id,
            deviation_from_locked,
            degraded_entry_std_multiplier,
            degraded_entry_duration_minutes,
        )

    # --- DEGRADED → BASELINE on sustained recovery ---
    if current == RegimeType.DEGRADED:
        return _evaluate_degraded(
            state, reading, asset_id,
            deviation_from_locked,
            degraded_entry_std_multiplier,
            degraded_exit_duration_minutes,
        )

    return state, None


# ---------------------------------------------------------------------------
# Public — forced transitions (maintenance, technician commands)
# ---------------------------------------------------------------------------

def enter_validating(state: TwinState, asset_id: str, reason: str) -> tuple[TwinState, RegimeTransition]:
    """
    Force transition to VALIDATING. Called by engine.py on maintenance events.
    """
    state, transition = _transition_if_needed(
        state, RegimeType.VALIDATING, asset_id, reason=reason
    )
    reset_trackers(asset_id)
    return state, transition


def enter_initialising(state: TwinState, asset_id: str, reason: str) -> tuple[TwinState, RegimeTransition]:
    """
    Force transition to INITIALISING. Called by engine.py on technician reset
    or cold start with stale state.
    """
    state, transition = _transition_if_needed(
        state, RegimeType.INITIALISING, asset_id, reason=reason
    )
    reset_trackers(asset_id)
    return state, transition


# ---------------------------------------------------------------------------
# Public — query helpers
# ---------------------------------------------------------------------------

def regime_allows_baseline_learning(state: TwinState) -> bool:
    """
    True if the current regime allows baseline learning to proceed.
    Baseline learning only happens in INITIALISING and VALIDATING.
    """
    return state.current_regime in (
        RegimeType.INITIALISING,
        RegimeType.VALIDATING,
    )


def is_operational(state: TwinState) -> bool:
    """
    True if the twin is in an operational regime (BASELINE or DEGRADED).
    Used by engine.py to gate purifier control commands.
    """
    return state.current_regime in (
        RegimeType.BASELINE,
        RegimeType.EVENT,
        RegimeType.DEGRADED,
    )


def regime_summary(state: TwinState) -> dict:
    """
    Return a summary dict of current regime state for API and engineer view.
    """
    return {
        "current_regime":          state.current_regime,
        "regime_entered_ts":       state.regime_entered_ts,
        "regime_duration_minutes": state.regime_duration_minutes,
        "baseline_locked":         state.baseline_locked,
        "baseline_locked_season":  str(state.baseline_locked_season)
                                   if state.baseline_locked_season else None,
        "baseline_current":        state.baseline_current,
        "baseline_std":            state.baseline_std,
    }


# ---------------------------------------------------------------------------
# Internal — baseline regime evaluation
# ---------------------------------------------------------------------------

def _evaluate_baseline(
    state: TwinState,
    reading: Reading,
    asset_id: str,
    deviation_from_locked: Optional[float],
    degraded_entry_std_multiplier: float,
    degraded_entry_duration_minutes: float,
) -> tuple[TwinState, Optional[RegimeTransition]]:
    """
    Evaluate whether BASELINE should transition to DEGRADED.

    Exceedance condition: rolling_mean > baseline_locked + N × baseline_std
    i.e. deviation_from_locked > N

    Must be sustained for M consecutive minutes before transition.
    Uses rolling_mean rather than instantaneous value to avoid reacting
    to spikes that resolve quickly.
    """
    if asset_id not in _exceedance_minutes:
        _exceedance_minutes[asset_id] = 0.0

    if deviation_from_locked is None:
        # No baseline yet — shouldn't be in BASELINE, but handle gracefully
        _exceedance_minutes[asset_id] = 0.0
        return state, None

    if deviation_from_locked > degraded_entry_std_multiplier:
        # Accumulate exceedance time (readings at 1Hz = 1/60 minutes per reading)
        _exceedance_minutes[asset_id] += 1.0 / 60.0

        logger.debug(f"[{asset_id}] Exceedance: {deviation_from_locked:.2f} std above locked "
                     f"({_exceedance_minutes[asset_id]:.1f}/{degraded_entry_duration_minutes} min)")

        if _exceedance_minutes[asset_id] >= degraded_entry_duration_minutes:
            reset_trackers(asset_id)
            return _transition_if_needed(
                state, RegimeType.EVENT, asset_id,
                reason=f"PM2.5 exceeded baseline_locked + "
                       f"{degraded_entry_std_multiplier}×std — "
                       f"monitoring for resolution "
                       f"(deviation={deviation_from_locked:.2f} std)"
            )
    else:
        # Not exceeding — reset counter
        if _exceedance_minutes.get(asset_id, 0) > 0:
            logger.debug(f"[{asset_id}] Exceedance counter reset — "
                         f"deviation={deviation_from_locked:.2f} std "
                         f"(below threshold {degraded_entry_std_multiplier})")
        _exceedance_minutes[asset_id] = 0.0

    # Update regime duration
    state = _update_duration(state)
    return state, None


# ---------------------------------------------------------------------------
# Internal — degraded regime evaluation
# ---------------------------------------------------------------------------


def _evaluate_event(
    state: TwinState,
    reading: Reading,
    asset_id: str,
    deviation_from_locked: Optional[float],
    degraded_entry_std_multiplier: float,
    degraded_entry_duration_minutes: float,
) -> tuple[TwinState, Optional[RegimeTransition]]:
    """
    Evaluate EVENT regime — transient air quality event in progress.
    Resolves to BASELINE if PM2.5 returns to range within window.
    Escalates to DEGRADED if elevation is sustained.
    """
    if asset_id not in _exceedance_minutes:
        _exceedance_minutes[asset_id] = 0.0

    if deviation_from_locked is None:
        state = _update_duration(state)
        return state, None

    if deviation_from_locked > degraded_entry_std_multiplier:
        _exceedance_minutes[asset_id] += 1.0 / 60.0
        if _exceedance_minutes[asset_id] >= degraded_entry_duration_minutes:
            reset_trackers(asset_id)
            return _transition_if_needed(
                state, RegimeType.DEGRADED, asset_id,
                reason=f"EVENT not resolved within {degraded_entry_duration_minutes} min "
                       f"— escalating to DEGRADED"
            )
    else:
        reset_trackers(asset_id)
        return _transition_if_needed(
            state, RegimeType.BASELINE, asset_id,
            reason="Transient event resolved — PM2.5 returned to baseline range"
        )

    state = _update_duration(state)
    return state, None


def _evaluate_degraded(
    state: TwinState,
    reading: Reading,
    asset_id: str,
    deviation_from_locked: Optional[float],
    degraded_entry_std_multiplier: float,
    degraded_exit_duration_minutes: float,
) -> tuple[TwinState, Optional[RegimeTransition]]:
    """
    Evaluate whether DEGRADED should transition back to BASELINE.

    Recovery condition: rolling_mean returns to within N×std of baseline_locked
    Must be sustained for degraded_exit_duration_minutes before transition.
    """
    if asset_id not in _recovery_minutes:
        _recovery_minutes[asset_id] = 0.0

    if deviation_from_locked is None:
        _recovery_minutes[asset_id] = 0.0
        state = _update_duration(state)
        return state, None

    if deviation_from_locked <= degraded_entry_std_multiplier:
        # Accumulate recovery time
        _recovery_minutes[asset_id] += 1.0 / 60.0

        logger.debug(f"[{asset_id}] Recovery: {deviation_from_locked:.2f} std "
                     f"({_recovery_minutes[asset_id]:.1f}/{degraded_exit_duration_minutes} min)")

        if _recovery_minutes[asset_id] >= degraded_exit_duration_minutes:
            reset_trackers(asset_id)
            return _transition_if_needed(
                state, RegimeType.BASELINE, asset_id,
                reason=f"PM2.5 returned to within {degraded_entry_std_multiplier}×std "
                       f"of baseline_locked for {degraded_exit_duration_minutes} minutes"
            )
    else:
        # Still elevated — reset recovery counter
        if _recovery_minutes.get(asset_id, 0) > 0:
            logger.debug(f"[{asset_id}] Recovery counter reset — "
                         f"still {deviation_from_locked:.2f} std above locked")
        _recovery_minutes[asset_id] = 0.0

    state = _update_duration(state)
    return state, None


# ---------------------------------------------------------------------------
# Internal — shared transition helper
# ---------------------------------------------------------------------------

def _transition_if_needed(
    state: TwinState,
    target: RegimeType,
    asset_id: str,
    reason: str,
) -> tuple[TwinState, Optional[RegimeTransition]]:
    """
    Apply a regime transition if target differs from current regime.
    Returns unchanged state and None if already in target regime.
    """
    if state.current_regime == target:
        state = _update_duration(state)
        return state, None

    now = utc_now()
    transition = RegimeTransition(
        from_regime=state.current_regime,
        to_regime=target,
        ts=now,
        duration_minutes=state.regime_duration_minutes,
        reason=reason,
    )

    logger.info(f"[{asset_id}] Regime transition: "
                f"{state.current_regime} → {target} | {reason}")

    state = replace(
        state,
        current_regime=target,
        regime_entered_ts=now,
        regime_duration_minutes=0.0,
    )

    return state, transition


def _update_duration(state: TwinState) -> TwinState:
    """Increment regime_duration_minutes by one reading interval (1Hz = 1/60 min)."""
    return replace(
        state,
        regime_duration_minutes=state.regime_duration_minutes + (1.0 / 60.0)
    )