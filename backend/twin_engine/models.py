"""
models.py — Shared dataclasses and enums for the Air Twin twin engine.

This module defines the shared vocabulary used across all twin engine modules.
It has no dependencies on any other twin engine module — everything else imports
from here. This prevents circular imports and gives every module a common type
system to work against.

All state that persists across restarts lives in TwinState. Modules receive
state as arguments and return updated state — they never read or write
twin_state.json directly. engine.py is the sole I/O boundary.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import datetime


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class RegimeType(str, Enum):
    """
    Valid twin engine regimes.

    String enum so values serialise cleanly to/from JSON without a custom
    encoder. RegimeType.BASELINE == "baseline" is true.
    """
    INITIALISING = "initialising"
    VALIDATING   = "validating"
    BASELINE     = "baseline"
    EVENT        = "event"
    DEGRADED     = "degraded"
    UNKNOWN      = "unknown"


class PlausibilityReason(str, Enum):
    """
    Plausibility tags assigned by sds011_reader.py on the Pi.
    Reproduced here so the twin engine can reference them by name
    rather than by raw string comparison.
    """
    OK              = "ok"
    WINDOW_FILLING  = "window_filling"
    DELTA_EXCEEDED  = "delta_exceeded"
    WARMUP          = "warmup"
    HARDWARE_BOUNDS = "hardware_bounds"


class ControlSource(str, Enum):
    """Who issued the last purifier command."""
    TWIN_ENGINE    = "twin_engine"
    LOCAL_FALLBACK = "local_fallback"
    MANUAL         = "manual"


class FilterType(str, Enum):
    """Filter configurations the device profile recognises."""
    PARTICLE_ONLY      = "particle_only"
    PARTICLE_AND_GAS   = "particle_and_gas"


class Season(str, Enum):
    SPRING = "spring"
    SUMMER = "summer"
    AUTUMN = "autumn"
    WINTER = "winter"


# ---------------------------------------------------------------------------
# Incoming data from MQTT subscriber
# ---------------------------------------------------------------------------

@dataclass
class Reading:
    """
    A single qualified PM2.5 reading as published by sds011_reader.py
    and merged with purifier state by mqtt_subscriber.py.

    All fields are optional because the subscriber may receive sensor
    and purifier payloads independently. The twin engine handles None
    fields explicitly — it never assumes a complete reading.
    """
    # Sensor fields
    ts:                   str             # UTC ISO8601
    value:                float           # PM2.5 µg/m³ from SDS011
    is_warmup:            bool
    changed:              bool
    is_plausible:         Optional[bool]  # None during warmup
    plausibility_reason:  PlausibilityReason
    rolling_mean:         float
    rolling_std:          float
    trend_slope:          float           # µg/m³ per second from Pi rolling window

    # Purifier state — merged by subscriber, may be None if no purifier msg yet
    purifier_on:          Optional[bool]
    fan_speed:            Optional[int]   # 1-5
    fan_mode:             Optional[str]   # auto / manual
    filter_age:           Optional[int]   # minutes — device button counter
    filter_age_unit:      Optional[str]   # always "minutes"
    device_age:           Optional[int]   # minutes — non-resettable
    device_age_unit:      Optional[str]   # always "minutes"
    pm25_internal:        Optional[float] # µg/m³ — IKEA internal sensor
    linkquality:          Optional[int]   # 0-255
    control_source:       Optional[ControlSource]


@dataclass
class PurifierState:
    """
    Current known state of the physical purifier.
    Maintained by engine.py from the most recent MQTT message.
    """
    fan_state:    str            # ON / OFF
    fan_speed:    int            # 1-5
    fan_mode:     str            # auto / manual
    filter_age:   int            # minutes
    device_age:   int            # minutes
    pm25_internal: Optional[float]
    linkquality:  int
    last_updated: str            # UTC ISO8601


# ---------------------------------------------------------------------------
# Evidence and confidence
# ---------------------------------------------------------------------------

@dataclass
class EvidenceDelta:
    """
    A single confidence evidence contribution from one factor in one cycle.
    The twin engine accumulates these per cycle and applies them to the
    confidence score. Stored in TwinState.confidence_factors so the
    engineer view can show exactly why confidence is at its current level.
    """
    factor:    str    # e.g. "plausibility_ok", "sensor_cross_reference_disagree"
    delta:     float  # signed — positive increases confidence, negative decreases
    reason:    str    # human-readable explanation for engineer view


# ---------------------------------------------------------------------------
# Performance observation
# ---------------------------------------------------------------------------

@dataclass
class PerformanceObservation:
    """
    A single purifier response event observation.
    Written to the performance_observations database table by engine.py.
    Used by performance.py to build empirical CADR estimates and update
    the room efficiency factor.
    """
    ts_start:           str    # UTC ISO8601 — spike/elevation detected
    ts_peak:            str    # UTC ISO8601 — peak PM2.5 value
    ts_end:             str    # UTC ISO8601 — resolved or regime change
    fan_speed:          int    # speed commanded during response
    peak_value:         float  # µg/m³
    observed_decay_rate: float # µg/m³ per minute — from trend_slope
    expected_decay_rate: float # µg/m³ per minute — from CADR / room_volume
    performance_ratio:  float  # observed / expected
    filter_type:        FilterType
    twin_filter_age_hours: float


# ---------------------------------------------------------------------------
# Persisted twin state
# ---------------------------------------------------------------------------

@dataclass
class TwinState:
    """
    The complete persisted state of the twin engine.
    Loaded from twin_state.json on startup. Written by engine.py after
    every processing cycle. Modules receive this as an argument and
    return an updated copy — they never read or write the file directly.

    All fields have defaults so a fresh TwinState can be constructed
    on first run without a pre-existing file.
    """

    # --- Regime ---
    current_regime:             RegimeType = RegimeType.INITIALISING
    regime_entered_ts:          Optional[str] = None   # UTC ISO8601
    regime_duration_minutes:    float = 0.0

    # --- Confidence ---
    confidence:                 float = 0.5            # 0.0 - 1.0
    confidence_factors:         dict = field(default_factory=dict)
    # confidence_factors schema:
    # { "factor_name": { "delta": float, "reason": str, "ts": str } }

    # --- Baseline ---
    baseline_locked:            Optional[float] = None  # µg/m³ median
    baseline_locked_ts:         Optional[str] = None    # UTC ISO8601
    baseline_locked_month:      Optional[int] = None    # 1-12
    baseline_locked_season:     Optional[Season] = None
    baseline_current:           Optional[float] = None  # slow EMA
    baseline_std:               Optional[float] = None  # std at lock time
    baseline_learn_readings:    int = 0                 # qualifying readings accumulated
    baseline_learn_started_ts:  Optional[str] = None

    # --- Filter ---
    filter_change_device_age_anchor: Optional[int] = None  # minutes
    filter_change_pending_reset:     bool = False
    last_known_filter_age:           Optional[int] = None  # minutes
    installed_filter_type:           FilterType = FilterType.PARTICLE_ONLY
    last_logged_filter_type:         Optional[FilterType] = None

    # --- Performance ---
    room_efficiency_factor:     float = 1.0

    # Empirical CADR estimates for manual mode speeds 2-4 (m³/h).
    # Keyed by str(fan_speed). Supersede interpolated priors from device
    # profile once empirical_cadr_min_observations threshold is met.
    # Speeds 1 and 5 always use manufacturer anchors — never updated here.
    empirical_cadr_m3h:         dict = field(default_factory=dict)
    # { "2": float, "3": float, "4": float }

    # Empirical performance curves for auto mode steps 1-9 (m³/h).
    # Keyed by str(auto_step). Built entirely from observations —
    # no manufacturer anchors exist for auto mode steps.
    empirical_cadr_auto_m3h:    dict = field(default_factory=dict)
    # { "1": float, "2": float, ..., "9": float }

    # Observation counts per speed — manual and auto tracked separately
    performance_observation_counts:      dict = field(default_factory=dict)
    # { "manual": {"1": int, ..., "5": int}, "auto": {"1": int, ..., "9": int} }

    # --- Asset status ---
    # Derived each cycle — separate from environment regime
    # operating_normally | responding | performance_low | filter_due | offline | unknown
    asset_status:               str = "unknown"
    service_level_compliance_pct: float = 100.0
    # Fraction of readings in rolling window below pm25 threshold
    monthly_energy_kwh:         float = 0.0
    monthly_cost_usd:           float = 0.0

    # --- Control ---
    last_fan_speed_commanded:   Optional[int] = None
    last_fan_mode:              Optional[str] = None
    last_command_ts:            Optional[str] = None
    last_command_acknowledged:  Optional[bool] = None

    # --- Spike response model ---
    # Learned model of adequate purifier auto response per spike magnitude bracket.
    # Keyed by "auto_step:bracket_index" → list of observed performance ratios.
    # Twin observes Phase A until min_spike_observations_to_learn reached,
    # then derives intervention threshold from this data in Phase B.
    spike_response_observations:    dict = field(default_factory=dict)
    # { "6:1": [0.82, 0.79, 0.85, ...], "7:2": [0.91, ...] }

    # Derived learned thresholds — median of observations per key.
    # None until sufficient observations exist per key.
    spike_intervention_thresholds:  dict = field(default_factory=dict)
    # { "6:1": 0.82, "7:2": 0.91 }

    # Total spike observations logged (across all brackets/steps)
    spike_observation_count:        int = 0

    # Whether twin has earned intervention rights
    # Set True automatically when spike_observation_count >= min threshold
    spike_intervention_enabled:     bool = False

    # Current spike observation in progress
    # Tracks decay rate measurements during active observation window
    spike_observation_active:       bool = False
    spike_observation_started_ts:   Optional[str] = None
    spike_observation_auto_step:    Optional[int] = None
    spike_observation_magnitude:    Optional[float] = None
    spike_observation_decay_rates:  list = field(default_factory=list)

    # --- Readings ---
    last_reading_ts:            Optional[str] = None
    last_reading_value:         Optional[float] = None

    # --- Commissioning ---
    commissioned_at:            Optional[str] = None   # UTC ISO8601 — first valid reading

    def to_dict(self) -> dict:
        """
        Serialise TwinState to a JSON-safe dict.
        Converts enums to their string values. All other types are
        already JSON-safe (float, int, str, bool, None, dict).
        Called by engine.py before writing twin_state.json.
        """
        return {
            # Regime
            "current_regime":             self.current_regime.value,
            "regime_entered_ts":          self.regime_entered_ts,
            "regime_duration_minutes":    self.regime_duration_minutes,
            # Confidence
            "confidence":                 self.confidence,
            "confidence_factors":         self.confidence_factors,
            # Baseline
            "baseline_locked":            self.baseline_locked,
            "baseline_locked_ts":         self.baseline_locked_ts,
            "baseline_locked_month":      self.baseline_locked_month,
            "baseline_locked_season":     self.baseline_locked_season.value
                                          if self.baseline_locked_season else None,
            "baseline_current":           self.baseline_current,
            "baseline_std":               self.baseline_std,
            "baseline_learn_readings":    self.baseline_learn_readings,
            "baseline_learn_started_ts":  self.baseline_learn_started_ts,
            # Filter
            "filter_change_device_age_anchor": self.filter_change_device_age_anchor,
            "filter_change_pending_reset":     self.filter_change_pending_reset,
            "last_known_filter_age":           self.last_known_filter_age,
            "installed_filter_type":           self.installed_filter_type.value,
            "last_logged_filter_type":         self.last_logged_filter_type.value
                                               if self.last_logged_filter_type else None,
            # Performance
            "room_efficiency_factor":          self.room_efficiency_factor,
            "empirical_cadr_m3h":              self.empirical_cadr_m3h,
            "empirical_cadr_auto_m3h":         self.empirical_cadr_auto_m3h,
            "performance_observation_counts":  self.performance_observation_counts,
            # Asset status
            "asset_status":               self.asset_status,
            "service_level_compliance_pct": self.service_level_compliance_pct,
            "monthly_energy_kwh":         self.monthly_energy_kwh,
            "monthly_cost_usd":           self.monthly_cost_usd,
            # Control
            "last_fan_speed_commanded":   self.last_fan_speed_commanded,
            "last_fan_mode":              self.last_fan_mode,
            "last_command_ts":            self.last_command_ts,
            "last_command_acknowledged":  self.last_command_acknowledged,
            # Spike response model
            "spike_response_observations":   self.spike_response_observations,
            "spike_intervention_thresholds": self.spike_intervention_thresholds,
            "spike_observation_count":       self.spike_observation_count,
            "spike_intervention_enabled":    self.spike_intervention_enabled,
            "spike_observation_active":      self.spike_observation_active,
            "spike_observation_started_ts":  self.spike_observation_started_ts,
            "spike_observation_auto_step":   self.spike_observation_auto_step,
            "spike_observation_magnitude":   self.spike_observation_magnitude,
            "spike_observation_decay_rates": self.spike_observation_decay_rates,
            # Readings
            "last_reading_ts":            self.last_reading_ts,
            "last_reading_value":         self.last_reading_value,
            # Commissioning
            "commissioned_at":            self.commissioned_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TwinState":
        """
        Deserialise TwinState from a JSON dict loaded from twin_state.json.
        Converts string values back to enums. Missing keys fall back to
        field defaults — allows safe loading of older state files when new
        fields are added.
        Called by engine.py on startup.
        """
        def get(key, default=None):
            return d.get(key, default)

        # Parse optional enums safely
        season_raw = get("baseline_locked_season")
        season = Season(season_raw) if season_raw else None

        filter_type_raw = get("installed_filter_type", FilterType.PARTICLE_ONLY.value)
        try:
            installed_filter_type = FilterType(filter_type_raw)
        except ValueError:
            installed_filter_type = FilterType.PARTICLE_ONLY

        last_filter_raw = get("last_logged_filter_type")
        last_logged_filter_type = FilterType(last_filter_raw) if last_filter_raw else None

        regime_raw = get("current_regime", RegimeType.INITIALISING.value)
        try:
            current_regime = RegimeType(regime_raw)
        except ValueError:
            current_regime = RegimeType.INITIALISING

        return cls(
            # Regime
            current_regime=current_regime,
            regime_entered_ts=get("regime_entered_ts"),
            regime_duration_minutes=get("regime_duration_minutes", 0.0),
            # Confidence
            confidence=get("confidence", 0.5),
            confidence_factors=get("confidence_factors", {}),
            # Baseline
            baseline_locked=get("baseline_locked"),
            baseline_locked_ts=get("baseline_locked_ts"),
            baseline_locked_month=get("baseline_locked_month"),
            baseline_locked_season=season,
            baseline_current=get("baseline_current"),
            baseline_std=get("baseline_std"),
            baseline_learn_readings=get("baseline_learn_readings", 0),
            baseline_learn_started_ts=get("baseline_learn_started_ts"),
            # Filter
            filter_change_device_age_anchor=get("filter_change_device_age_anchor"),
            filter_change_pending_reset=get("filter_change_pending_reset", False),
            last_known_filter_age=get("last_known_filter_age"),
            installed_filter_type=installed_filter_type,
            last_logged_filter_type=last_logged_filter_type,
            # Performance
            room_efficiency_factor=get("room_efficiency_factor", 1.0),
            empirical_cadr_m3h=get("empirical_cadr_m3h", {}),
            empirical_cadr_auto_m3h=get("empirical_cadr_auto_m3h", {}),
            performance_observation_counts=get("performance_observation_counts", {}),
            # Asset status
            asset_status=get("asset_status", "unknown"),
            service_level_compliance_pct=get("service_level_compliance_pct", 100.0),
            monthly_energy_kwh=get("monthly_energy_kwh", 0.0),
            monthly_cost_usd=get("monthly_cost_usd", 0.0),
            # Control
            last_fan_speed_commanded=get("last_fan_speed_commanded"),
            last_fan_mode=get("last_fan_mode"),
            last_command_ts=get("last_command_ts"),
            last_command_acknowledged=get("last_command_acknowledged"),
            # Spike response model
            spike_response_observations=get("spike_response_observations", {}),
            spike_intervention_thresholds=get("spike_intervention_thresholds", {}),
            spike_observation_count=get("spike_observation_count", 0),
            spike_intervention_enabled=get("spike_intervention_enabled", False),
            spike_observation_active=get("spike_observation_active", False),
            spike_observation_started_ts=get("spike_observation_started_ts"),
            spike_observation_auto_step=get("spike_observation_auto_step"),
            spike_observation_magnitude=get("spike_observation_magnitude"),
            spike_observation_decay_rates=get("spike_observation_decay_rates", []),
            # Readings
            last_reading_ts=get("last_reading_ts"),
            last_reading_value=get("last_reading_value"),
            # Commissioning
            commissioned_at=get("commissioned_at"),
        )


# ---------------------------------------------------------------------------
# Device profile and asset — loader output types
# ---------------------------------------------------------------------------

@dataclass
class CadrEntry:
    """A single CADR value with provenance."""
    value:         Optional[float]  # m³/h — None for unpublished intermediate speeds
    source:        str              # "manufacturer" | "interpolated"
    source_detail: str


@dataclass
class FilterProfile:
    """Performance and life characteristics for one filter type."""
    label:              str
    description:        str
    filter_life_hours:  int
    filter_life_source: str
    cadr_profile:       str         # key into cadr dict


@dataclass
class DeviceProfile:
    """
    Parsed device profile as returned by loader.get_device_profile().
    The twin engine works against this dataclass — never against raw JSON.
    """
    model_id:               str
    manufacturer:           str
    model:                  str
    device_type:            str
    sku:                    str
    zigbee_friendly_name:   str

    # Manual mode fan speeds — 1-5 for Starkvind
    fan_speeds_valid:       list[int]
    fan_speed_min:          int
    fan_speed_max:          int

    # Auto mode fan speeds — separate scale, separate empirical curve
    # 1-9 for Starkvind. Empty list if device has no distinct auto scale.
    auto_fan_speeds_valid:  list[int]
    auto_fan_speed_min:     int
    auto_fan_speed_max:     int
    auto_mode_cadr_source:  str   # "empirical_only" | "manufacturer"

    # cadr[filter_config][str(fan_speed)] → CadrEntry
    cadr:                   dict[str, dict[str, CadrEntry]]
    interpolation_policy:   str     # "power_law" | "linear" | "none"

    filter_types:           dict[str, FilterProfile]
    default_filter_type:    str

    performance_ratio_degradation_threshold: float
    empirical_cadr_min_observations:         int
    clogged_filter_physics:                  str

    command_acknowledgement_timeout_s:       int
    valid_fan_modes:                         list[str]


@dataclass
class AssetPlacement:
    """Physical placement of an asset in the room."""
    section:              Optional[str]    # e.g. "narrow"
    position_m:           Optional[list]   # [x, y, z] world coords from Blender
    blender_object_name:  Optional[str]
    position_source:      str              # "blender_export" | "pending_blender_export"


@dataclass
class AssetFilterState:
    """Filter state as recorded in asset_registry.json."""
    installed_type:                  FilterType
    filter_change_device_age_anchor: Optional[int]
    last_logged_filter_type:         Optional[FilterType]


@dataclass
class Asset:
    """
    Parsed asset registry entry as returned by loader.get_asset().
    The twin engine works against this dataclass — never against raw JSON.
    """
    asset_id:               str
    device_profile_id:      str
    sku:                    str
    zigbee_ieee:            str
    zigbee_friendly_name:   str
    room_volume_m3:         float
    room_shape:             str
    placement:              AssetPlacement
    filter_state:           AssetFilterState
    commissioned_at:        Optional[str]
    notes:                  Optional[str]


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def utc_now() -> str:
    """Return current UTC time as ISO8601 string."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def season_from_month(month: int) -> Season:
    """Derive meteorological season from calendar month (northern hemisphere)."""
    if month in (12, 1, 2):
        return Season.WINTER
    elif month in (3, 4, 5):
        return Season.SPRING
    elif month in (6, 7, 8):
        return Season.SUMMER
    else:
        return Season.AUTUMN