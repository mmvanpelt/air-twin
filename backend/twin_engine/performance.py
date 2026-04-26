"""
performance.py — Performance modelling for the Air Twin twin engine.

Owns all logic related to purifier performance evaluation:
  - Expected decay rate from device profile CADR and room volume
  - Observed decay rate from trend_slope during response events
  - Performance ratio: observed / expected
  - Room efficiency factor updates from accumulated observations
  - Empirical CADR promotion for unpublished speeds 2-4

No I/O — never reads or writes files directly. engine.py is the
sole I/O boundary.

The room efficiency factor absorbs the difference between manufacturer
CADR (tested in a sealed chamber) and real-world room performance.
Consistent field deviation from manufacturer anchors is absorbed into
the room efficiency factor — it is never interpreted as a device CADR
correction. The device profile anchor values for speeds 1 and 5 are
treated as ground truth.

Performance observations during auto mode are valid and logged.
fan_speed is always reported by Zigbee2MQTT regardless of fan_mode.
Observations are tagged with control_source so engineer view can
distinguish twin-commanded vs auto-mode events.
"""

import logging
import statistics
from dataclasses import replace
from typing import Optional

from backend.twin_engine.models import (
    ControlSource,
    DeviceProfile,
    FilterType,
    PerformanceObservation,
    Reading,
    TwinState,
    utc_now,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level event buffer — tracks active purifier response events
# Keyed by asset_id.
# ---------------------------------------------------------------------------

class _ResponseEvent:
    """Tracks an in-progress purifier response event."""
    __slots__ = [
        "ts_start", "ts_peak", "peak_value",
        "fan_speed", "filter_type", "twin_filter_age_hours",
        "control_source", "readings"
    ]

    def __init__(self, ts_start, fan_speed, filter_type,
                 twin_filter_age_hours, control_source):
        self.ts_start             = ts_start
        self.ts_peak              = ts_start
        self.peak_value           = 0.0
        self.fan_speed            = fan_speed
        self.filter_type          = filter_type
        self.twin_filter_age_hours = twin_filter_age_hours
        self.control_source       = control_source
        self.readings: list[float] = []


_active_events: dict[str, Optional[_ResponseEvent]] = {}


# ---------------------------------------------------------------------------
# Public — expected decay rate
# ---------------------------------------------------------------------------

def expected_decay_rate(
    fan_speed: int,
    filter_type: FilterType,
    profile: DeviceProfile,
    state: TwinState,
    room_volume_m3: float,
) -> Optional[float]:
    """
    Calculate expected PM2.5 decay rate in µg/m³ per minute.

    Uses empirical CADR from twin_state if available and promoted,
    otherwise uses device profile value (manufacturer or interpolated).
    Applies room_efficiency_factor from twin_state.

    Formula: (CADR_m3h * room_efficiency_factor / room_volume_m3) / 60
    Division by 60 converts from per-hour to per-minute to match
    trend_slope units from the Pi (µg/m³ per second * 60).

    Args:
        fan_speed:      Current fan speed (1-5)
        filter_type:    Installed filter type
        profile:        Loaded DeviceProfile dataclass
        state:          Current TwinState
        room_volume_m3: Room volume in m³ from asset registry

    Returns:
        Expected decay rate in µg/m³ per minute, or None if CADR unavailable
    """
    cadr = _get_cadr(fan_speed, filter_type, profile, state)
    if cadr is None:
        logger.warning(f"No CADR available for speed {fan_speed}, "
                       f"filter {filter_type} — cannot compute expected decay rate")
        return None

    rate = (cadr * state.room_efficiency_factor / room_volume_m3) / 60.0
    logger.debug(f"Expected decay rate: {rate:.4f} µg/m³/min "
                 f"(CADR={cadr:.1f}, REF={state.room_efficiency_factor:.3f}, "
                 f"vol={room_volume_m3})")
    return rate


def get_cadr_with_source(
    fan_speed: int,
    filter_type: FilterType,
    profile: DeviceProfile,
    state: TwinState,
) -> tuple[Optional[float], str]:
    """
    Return CADR value and its source label for engineer view.

    Returns:
        Tuple of (cadr_value, source_label) where source_label is one of:
        "manufacturer", "empirical", "interpolated"
    """
    speed_str = str(fan_speed)

    # Empirical values only apply to intermediate speeds 2-4.
    # Speeds 1 and 5 always return manufacturer anchor.
    if fan_speed in (2, 3, 4):
        empirical = state.empirical_cadr_m3h.get(speed_str)
        if empirical is not None:
            return empirical, "empirical"

    # Fall back to device profile (manufacturer or interpolated)
    cadr_config = _cadr_config_key(filter_type)
    cadr_map = profile.cadr.get(cadr_config, {})
    entry = cadr_map.get(speed_str)
    if entry is None:
        return None, "unknown"

    return entry.value, entry.source


# ---------------------------------------------------------------------------
# Public — observe and evaluate response events
# ---------------------------------------------------------------------------

def on_purifier_active(
    state: TwinState,
    reading: Reading,
    asset_id: str,
    profile: DeviceProfile,
    room_volume_m3: float,
    twin_filter_age_hours: float,
    min_event_duration_minutes: float,
) -> tuple[TwinState, Optional[PerformanceObservation], list[str]]:
    """
    Called each cycle when purifier is ON. Tracks the response event,
    computes performance ratio when the event resolves, and updates
    room efficiency factor and empirical CADR estimates.

    Args:
        state:                      Current TwinState
        reading:                    Current reading
        asset_id:                   Asset identifier
        profile:                    Loaded DeviceProfile
        room_volume_m3:             Room volume in m³
        twin_filter_age_hours:      Twin-calculated filter age
        min_event_duration_minutes: Minimum event duration for valid observation

    Returns:
        Tuple of (updated TwinState, PerformanceObservation or None, signals)
        PerformanceObservation is returned when an event completes.
        Signals are passed to confidence.py.
    """
    signals: list[str] = []
    observation: Optional[PerformanceObservation] = None

    if asset_id not in _active_events:
        _active_events[asset_id] = None

    fan_speed = reading.fan_speed
    filter_type = FilterType(
        state.installed_filter_type
        if hasattr(state, 'installed_filter_type')
        else FilterType.PARTICLE_ONLY
    )
    control_source = reading.control_source or ControlSource.MANUAL

    # Start new event if none active
    if _active_events[asset_id] is None:
        _active_events[asset_id] = _ResponseEvent(
            ts_start=reading.ts,
            fan_speed=fan_speed,
            filter_type=filter_type,
            twin_filter_age_hours=twin_filter_age_hours,
            control_source=control_source,
        )
        logger.debug(f"[{asset_id}] Response event started — "
                     f"speed={fan_speed}, mode={reading.fan_mode}")

    event = _active_events[asset_id]

    # Accumulate readings
    if reading.value is not None:
        event.readings.append(reading.value)
        if reading.value > event.peak_value:
            event.peak_value = reading.value
            event.ts_peak = reading.ts

    return state, observation, signals


def on_purifier_inactive(
    state: TwinState,
    reading: Reading,
    asset_id: str,
    profile: DeviceProfile,
    room_volume_m3: float,
    min_event_duration_minutes: float,
) -> tuple[TwinState, Optional[PerformanceObservation], list[str]]:
    """
    Called each cycle when purifier is OFF or has just turned off.
    Resolves any active response event and computes performance ratio.
    """
    signals: list[str] = []
    observation: Optional[PerformanceObservation] = None

    if asset_id not in _active_events or _active_events[asset_id] is None:
        return state, observation, signals

    event = _active_events[asset_id]
    _active_events[asset_id] = None

    # Check minimum duration
    duration_minutes = len(event.readings) / 60.0  # readings at 1Hz
    if duration_minutes < min_event_duration_minutes:
        logger.debug(f"[{asset_id}] Response event too short "
                     f"({duration_minutes:.1f} min < {min_event_duration_minutes} min) "
                     f"— discarding")
        return state, observation, signals

    # Compute observed decay rate from trend_slope
    # trend_slope from Pi is µg/m³ per second — convert to per minute
    # Use negative slope during purifier active period = decay
    observed_decay = _compute_observed_decay(event.readings)
    if observed_decay is None:
        logger.debug(f"[{asset_id}] Could not compute observed decay — "
                     f"insufficient readings")
        return state, observation, signals

    # Compute expected decay rate
    exp_decay = expected_decay_rate(
        event.fan_speed, event.filter_type, profile, state, room_volume_m3
    )
    if exp_decay is None or exp_decay == 0:
        logger.debug(f"[{asset_id}] Expected decay unavailable — skipping observation")
        return state, observation, signals

    performance_ratio = observed_decay / exp_decay
    logger.info(f"[{asset_id}] Performance observation — "
                f"speed={event.fan_speed}, "
                f"observed={observed_decay:.4f}, "
                f"expected={exp_decay:.4f}, "
                f"ratio={performance_ratio:.3f}")

    # Build observation record
    observation = PerformanceObservation(
        ts_start=event.ts_start,
        ts_peak=event.ts_peak,
        ts_end=reading.ts,
        fan_speed=event.fan_speed,
        observed_decay_rate=observed_decay,
        expected_decay_rate=exp_decay,
        performance_ratio=performance_ratio,
        filter_type=event.filter_type,
        twin_filter_age_hours=event.twin_filter_age_hours,
    )

    # Emit confidence signal if ratio is below threshold
    if performance_ratio < profile.performance_ratio_degradation_threshold:
        signals.append("performance_ratio_below_threshold")
        logger.warning(f"[{asset_id}] Performance ratio below threshold: "
                       f"{performance_ratio:.3f} < "
                       f"{profile.performance_ratio_degradation_threshold}")

    # Update state — room efficiency factor and empirical CADR
    state = _update_room_efficiency_factor(
        state, event.fan_speed, performance_ratio
    )
    state = _maybe_promote_empirical_cadr(
        state, event.fan_speed, observed_decay,
        exp_decay, profile, room_volume_m3
    )

    return state, observation, signals


# ---------------------------------------------------------------------------
# Public — performance diagnostic
# ---------------------------------------------------------------------------

def diagnose_degradation(
    state: TwinState,
    profile: DeviceProfile,
    twin_filter_age_hours: float,
    linkquality: Optional[int],
) -> dict:
    """
    Run the diagnostic decision tree when performance ratio is degraded.
    Returns a dict with hypothesis and recommended action for engineer view.

    Implements the decision tree from §12 of the architecture spec.
    """
    filter_type_key = (
        state.installed_filter_type
        if hasattr(state, 'installed_filter_type')
        else FilterType.PARTICLE_ONLY
    )
    filter_profile = profile.filter_types.get(
        filter_type_key.value
        if hasattr(filter_type_key, 'value') else str(filter_type_key)
    )
    filter_life_hours = filter_profile.filter_life_hours if filter_profile else 4380

    # Branch 1 — filter age approaching life limit
    filter_age_fraction = twin_filter_age_hours / filter_life_hours
    if filter_age_fraction > 0.85:
        return {
            "hypothesis": "filter_exhaustion",
            "confidence": "high",
            "recommendation": "Replace filter",
            "filter_age_hours": twin_filter_age_hours,
            "filter_life_hours": filter_life_hours,
        }

    # Branch 2 — check observation counts across speeds for consistency
    obs_counts = state.performance_observation_counts
    multi_speed_data = {k: v for k, v in obs_counts.items() if v >= 3}

    if len(multi_speed_data) >= 2:
        # Consistent across speeds → systematic issue
        return {
            "hypothesis": "systematic_degradation",
            "confidence": "medium",
            "recommendation": "Inspect filter and air intake",
        }

    # Branch 3 — Zigbee signal degrading
    if linkquality is not None and linkquality < 100:
        return {
            "hypothesis": "communication_fault",
            "confidence": "medium",
            "recommendation": "Check Zigbee signal strength",
            "linkquality": linkquality,
        }

    # Branch 4 — unknown
    return {
        "hypothesis": "unknown_degradation",
        "confidence": "low",
        "recommendation": "Technician inspection required",
    }


# ---------------------------------------------------------------------------
# Internal — CADR lookup
# ---------------------------------------------------------------------------

def _get_cadr(
    fan_speed: int,
    filter_type: FilterType,
    profile: DeviceProfile,
    state: TwinState,
) -> Optional[float]:
    """
    Get CADR for a fan speed and filter type.
    Empirical values from twin_state take precedence over profile values
    for speeds 2-4. Speeds 1 and 5 always use profile values.
    """
    speed_str = str(fan_speed)

    # Empirical values only apply to intermediate speeds 2-4.
    # Speeds 1 and 5 always use manufacturer anchors — they are published
    # values and consistent field deviation is absorbed into room_efficiency_factor,
    # never into the device profile.
    if fan_speed in (2, 3, 4):
        empirical = state.empirical_cadr_m3h.get(speed_str)
        if empirical is not None:
            return empirical
    # Speeds 1 and 5 fall through to device profile unconditionally

    # Device profile value (manufacturer anchor or interpolated)
    cadr_config = _cadr_config_key(filter_type)
    cadr_map = profile.cadr.get(cadr_config, {})
    entry = cadr_map.get(speed_str)
    if entry is None:
        return None
    return entry.value


def _cadr_config_key(filter_type: FilterType) -> str:
    """Map FilterType enum to cadr dict key in device profile."""
    if filter_type == FilterType.PARTICLE_AND_GAS:
        return "particle_and_gas_filter"
    return "particle_filter_only"


# ---------------------------------------------------------------------------
# Internal — observed decay computation
# ---------------------------------------------------------------------------

def _compute_observed_decay(readings: list[float]) -> Optional[float]:
    """
    Compute observed decay rate from a sequence of PM2.5 readings
    collected during a purifier response event.

    Uses linear regression slope over the readings.
    Readings are at 1Hz so slope is µg/m³ per second.
    Returns negative of slope (decay is a drop — we want a positive rate).
    Converted to µg/m³ per minute for comparison with expected_decay_rate.

    Returns None if insufficient readings for reliable estimate.
    """
    if len(readings) < 30:  # minimum 30 seconds
        return None

    n = len(readings)
    x = list(range(n))
    x_mean = (n - 1) / 2.0
    y_mean = statistics.mean(readings)

    numerator = sum((x[i] - x_mean) * (readings[i] - y_mean) for i in range(n))
    denominator = sum((x[i] - x_mean) ** 2 for i in range(n))

    if denominator == 0:
        return None

    slope_per_second = numerator / denominator  # µg/m³ per second
    decay_per_minute = -slope_per_second * 60   # positive if PM2.5 is dropping

    if decay_per_minute <= 0:
        logger.debug(f"Observed decay is non-positive ({decay_per_minute:.4f}) "
                     f"— PM2.5 did not drop during this event")
        return None

    return decay_per_minute


# ---------------------------------------------------------------------------
# Internal — state updates
# ---------------------------------------------------------------------------

def _update_room_efficiency_factor(
    state: TwinState,
    fan_speed: int,
    performance_ratio: float,
) -> TwinState:
    """
    Update room efficiency factor using exponential moving average
    of observed performance ratios.

    The room efficiency factor absorbs geometric and environmental
    inefficiency. It is not a device degradation signal — it represents
    how effectively the device's CADR translates to real room decay.

    Anchored between 0.3 and 1.2 — below 0.3 indicates a measurement
    problem rather than a room characteristic.
    """
    alpha = 0.1  # slow update — room geometry doesn't change
    new_ref = (alpha * performance_ratio
               + (1.0 - alpha) * state.room_efficiency_factor)
    new_ref = max(0.3, min(1.2, new_ref))  # clamp

    logger.debug(f"Room efficiency factor updated: "
                 f"{state.room_efficiency_factor:.3f} → {new_ref:.3f} "
                 f"(observation ratio={performance_ratio:.3f})")

    return replace(state, room_efficiency_factor=new_ref)


def _maybe_promote_empirical_cadr(
    state: TwinState,
    fan_speed: int,
    observed_decay: float,
    expected_decay: float,
    profile: DeviceProfile,
    room_volume_m3: float,
) -> TwinState:
    """
    Accumulate performance observations for intermediate speeds (2-4).
    Promote empirical CADR estimate when min_observations threshold is met.

    Empirical CADR is back-calculated from observed decay rate:
        empirical_cadr = observed_decay_per_min * 60 * room_volume / REF

    Speeds 1 and 5 use manufacturer anchors — never updated empirically.
    """
    if fan_speed not in (2, 3, 4):
        return state

    speed_str = str(fan_speed)

    # Update observation count
    counts = dict(state.performance_observation_counts)
    counts[speed_str] = counts.get(speed_str, 0) + 1

    # Back-calculate empirical CADR
    empirical_cadr = (observed_decay * 60 * room_volume_m3
                      / state.room_efficiency_factor)

    # Update running average of empirical CADR
    empirical = dict(state.empirical_cadr_m3h)
    existing = empirical.get(speed_str)
    if existing is None:
        empirical[speed_str] = empirical_cadr
    else:
        # EMA of empirical estimates
        alpha = 0.2
        empirical[speed_str] = alpha * empirical_cadr + (1 - alpha) * existing

    # Check if threshold met for promotion
    if counts[speed_str] >= profile.empirical_cadr_min_observations:
        logger.info(f"Empirical CADR promoted for speed {fan_speed}: "
                    f"{empirical[speed_str]:.1f} m³/h "
                    f"(after {counts[speed_str]} observations)")

    return replace(
        state,
        performance_observation_counts=counts,
        empirical_cadr_m3h=empirical,
    )