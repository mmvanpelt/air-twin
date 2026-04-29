"""
brief_generator.py — Executive brief generator for the Air Twin digital twin.

Pure function module. Takes TwinState and current context, returns a
structured brief dict. No I/O, no side effects, no database access.

Brief principle (from architecture spec):
  The executive brief expresses conclusions and required actions only —
  no data, no timestamps, no narration. The twin engine reasons from data;
  the brief communicates judgement.

Four role views are generated from the same underlying state:
  - executive: conclusion + required actions only
  - operator:  conclusion + active alerts + filter status
  - engineer:  full confidence breakdown + regime history + diagnostics
  - technician: maintenance status + filter age + pending actions

All views are generated in one call and returned as a single dict.
Callers select the view they need. This ensures all roles see the
same underlying assessment — never divergent conclusions.
"""

import logging
from typing import Optional

from backend.twin_engine.confidence import (
    all_active_factors,
    confidence_conclusion,
    dominant_negative_factor,
)
from backend.twin_engine.filter import (
    best_filter_age_hours,
    filter_life_fraction,
    no_anchor_on_record,
)
from backend.twin_engine.models import RegimeType, TwinState
from backend.twin_engine.regime import regime_summary

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public — main entry point
# ---------------------------------------------------------------------------


def _asset_health_brief(state: TwinState, profile: dict) -> dict:
    """
    Generate asset health section for executive brief.
    Returns dict with health, filter, costs, service_level.
    """
    device = profile.get("devices", {}).get("ikea_starkvind_e2007", {})
    consumables = device.get("consumables", {})
    device_life_hours = device.get("device_life_hours", 26280)

    # Filter life
    filter_type = state.installed_filter_type or "particle_only"
    filter_key = "particle_and_gas_filter" if "gas" in filter_type else "particle_filter"
    filter_info = consumables.get(filter_key, {})
    filter_life_hours = filter_info.get("life_hours", 4380)
    filter_cost = filter_info.get("replacement_cost_usd", {"low": 15, "high": 25})

    filter_age_hours = (state.last_known_filter_age or 0) / 60
    filter_remaining_pct = max(0, 100 - (filter_age_hours / filter_life_hours * 100))
    filter_remaining_hours = max(0, filter_life_hours - filter_age_hours)
    filter_remaining_weeks = round(filter_remaining_hours / (24 * 7), 0)

    # Device life
    device_age_hours = filter_age_hours  # approximate
    device_life_remaining_pct = max(0, 100 - (device_age_hours / device_life_hours * 100))
    device_years_remaining = max(0, (device_life_hours - device_age_hours) / 8760)

    # Monthly costs
    monthly_energy_cost = state.monthly_cost_usd
    filter_monthly_cost = round(
        ((filter_cost["low"] + filter_cost["high"]) / 2) / (filter_life_hours / (24 * 30)), 2
    )
    total_monthly = round(monthly_energy_cost + filter_monthly_cost, 2)

    # Service level
    compliance = state.service_level_compliance_pct
    service_met = compliance >= 95.0

    return {
        "asset_status": state.asset_status,
        "filter": {
            "type": filter_type.replace("_", " "),
            "life_remaining_pct": round(filter_remaining_pct, 0),
            "weeks_to_replacement": int(filter_remaining_weeks),
            "replacement_cost_low": filter_cost["low"],
            "replacement_cost_high": filter_cost["high"],
        },
        "device": {
            "life_remaining_pct": round(device_life_remaining_pct, 0),
            "years_remaining": round(device_years_remaining, 1),
        },
        "costs": {
            "energy_monthly_usd": round(monthly_energy_cost, 2),
            "filter_monthly_usd": filter_monthly_cost,
            "total_monthly_usd": total_monthly,
            "total_annual_usd": round(total_monthly * 12, 2),
        },
        "service_level": {
            "compliance_pct": compliance,
            "target_pct": 95.0,
            "met": service_met,
        },
    }


def generate(
    state: TwinState,
    device_age_minutes: Optional[int] = None,
    filter_age_minutes: Optional[int] = None,
    filter_life_hours: int = 4380,
    recent_regime_history: Optional[list] = None,
    open_alerts: Optional[list] = None,
    performance_degraded: bool = False,
    diagnostic: Optional[dict] = None,
) -> dict:
    """
    Generate a complete brief for all four role views.

    Args:
        state:                Current TwinState
        device_age_minutes:   Current device_age from purifier state
        filter_age_minutes:   Current filter_age from purifier state
        filter_life_hours:    Filter life hours from device profile
        recent_regime_history: List of recent RegimeTransition dicts (from db)
        open_alerts:          List of open escalation event dicts (from db)
        performance_degraded: True if latest performance ratio below threshold
        diagnostic:           Output of performance.diagnose_degradation() if relevant

    Returns:
        Dict with keys: executive, operator, engineer, technician, meta
    """
    # Derive shared context used across all views
    regime_str = str(state.current_regime).lower().replace("regimetype.", "")
    conclusion = confidence_conclusion(state.confidence, regime=regime_str)
    required_actions = _required_actions(
        state=state,
        device_age_minutes=device_age_minutes,
        filter_age_minutes=filter_age_minutes,
        filter_life_hours=filter_life_hours,
        open_alerts=open_alerts or [],
        performance_degraded=performance_degraded,
        diagnostic=diagnostic,
    )
    filter_status = _filter_status(
        state=state,
        device_age_minutes=device_age_minutes,
        filter_age_minutes=filter_age_minutes,
        filter_life_hours=filter_life_hours,
    )

    return {
        "executive": _executive_view(conclusion, required_actions),
        "operator":  _operator_view(
            conclusion, required_actions, filter_status, open_alerts or []
        ),
        "engineer":  _engineer_view(
            state, conclusion, required_actions, filter_status,
            recent_regime_history or [], diagnostic
        ),
        "technician": _technician_view(
            state, filter_status, open_alerts or []
        ),
        "meta": {
            "confidence": state.confidence,
            "regime": state.current_regime,
            "baseline_locked": state.baseline_locked,
        },
    }


# ---------------------------------------------------------------------------
# Role views
# ---------------------------------------------------------------------------

def _executive_view(conclusion: str, required_actions: list[str]) -> dict:
    """
    Executive view — conclusion and required actions only.
    No data, no timestamps, no narration.
    """
    return {
        "conclusion": conclusion,
        "required_actions": required_actions,
    }


def _operator_view(
    conclusion: str,
    required_actions: list[str],
    filter_status: dict,
    open_alerts: list,
) -> dict:
    """
    Operator view — conclusion, active alerts, filter status.
    Actionable context without engineering detail.
    """
    active_alerts = [
        a.get("description", str(a))
        for a in open_alerts
        if not a.get("resolved", False)
    ]

    return {
        "conclusion": conclusion,
        "required_actions": required_actions,
        "active_alerts": active_alerts,
        "alert_count": len(active_alerts),
        "filter": {
            "life_percent": filter_status["life_percent"],
            "replacement_due": filter_status["replacement_due"],
            "pending_reset": filter_status["pending_reset"],
            "source": filter_status["source"],
        },
    }


def _engineer_view(
    state: TwinState,
    conclusion: str,
    required_actions: list[str],
    filter_status: dict,
    recent_regime_history: list,
    diagnostic: Optional[dict],
) -> dict:
    """
    Engineer view — full confidence breakdown, regime history, diagnostics.
    Everything needed to understand why the twin believes what it believes.
    """
    factors = all_active_factors(state)
    dominant = dominant_negative_factor(state)
    r_summary = regime_summary(state)

    # Regime history summary
    degraded_count = sum(
        1 for r in recent_regime_history
        if r.get("to_regime") == RegimeType.DEGRADED.value
    )

    # Baseline context
    baseline_context = _baseline_context(state)

    # Confidence breakdown — positive and negative factors separated
    positive_factors = [f for f in factors if f["delta"] > 0]
    negative_factors = [f for f in factors if f["delta"] < 0]

    return {
        "conclusion": conclusion,
        "required_actions": required_actions,
        "confidence": {
            "score": state.confidence,
            "dominant_negative_factor": {
                "factor": dominant[0],
                "delta": dominant[1],
                "reason": dominant[2],
            } if dominant else None,
            "positive_factors": positive_factors,
            "negative_factors": negative_factors,
        },
        "regime": {
            **r_summary,
            "degraded_count_recent": degraded_count,
        },
        "baseline": baseline_context,
        "filter": filter_status,
        "performance_diagnostic": diagnostic,
        "room_efficiency_factor": state.room_efficiency_factor,
        "empirical_cadr_manual": state.empirical_cadr_m3h,
        "empirical_cadr_auto": getattr(state, "empirical_cadr_auto_m3h", {}),
    }


def _technician_view(
    state: TwinState,
    filter_status: dict,
    open_alerts: list,
) -> dict:
    """
    Technician view — maintenance status, filter age, pending actions.
    Focused on physical device state and what needs to be done.
    """
    pending_actions = []

    if filter_status["replacement_due"]:
        pending_actions.append("Replace filter — life threshold exceeded")

    if filter_status["pending_reset"]:
        pending_actions.append(
            "Press filter reset button behind front panel — "
            "filter change logged but counter not reset"
        )

    if filter_status["no_anchor"]:
        pending_actions.append(
            "No confirmed filter change on record — "
            "log filter change via QR scan to establish baseline"
        )

    maintenance_alerts = [
        a for a in open_alerts
        if a.get("type") in ("filter", "maintenance", "uncommanded_state_change")
    ]

    return {
        "pending_actions": pending_actions,
        "action_count": len(pending_actions),
        "filter": filter_status,
        "maintenance_alerts": maintenance_alerts,
        "baseline_locked": state.baseline_locked is not None,
        "commissioned": state.commissioned_at is not None,
        "commissioned_at": state.commissioned_at,
        "filter_change_pending_reset": state.filter_change_pending_reset,
    }


# ---------------------------------------------------------------------------
# Internal — shared derivations
# ---------------------------------------------------------------------------

def _required_actions(
    state: TwinState,
    device_age_minutes: Optional[int],
    filter_age_minutes: Optional[int],
    filter_life_hours: int,
    open_alerts: list,
    performance_degraded: bool,
    diagnostic: Optional[dict],
) -> list[str]:
    """
    Derive required actions from current twin state.
    Returns a list of action strings in priority order.
    Actions are conclusions — not data. No timestamps, no µg/m³ values.
    """
    actions = []

    # Regime-based actions — highest priority
    if state.current_regime == RegimeType.DEGRADED:
        actions.append("Air quality degraded — investigate source and increase ventilation")

    if state.current_regime == RegimeType.EVENT:
        actions.append(
            "Temporary air quality event in progress — purifier responding. "
            "If source is known (cooking, candle), no action required. "
            "If source unknown, investigate."
        )

    if state.current_regime == RegimeType.UNKNOWN:
        actions.append("Sensor data unavailable — check Pi and sensor connections")

    if state.current_regime in (RegimeType.INITIALISING, RegimeType.VALIDATING):
        actions.append("System establishing baseline — no action required")

    # Confidence-based actions
    if state.confidence < 0.3:
        actions.append(
            "Twin confidence critically low — engineer review required "
            "before relying on any assessment"
        )
    elif state.confidence < 0.5:
        actions.append("Twin confidence low — engineer review recommended")

    # Filter actions
    if device_age_minutes is not None and filter_age_minutes is not None:
        age_hours, _ = best_filter_age_hours(state, device_age_minutes, filter_age_minutes)
        fraction, _ = filter_life_fraction(
            state, device_age_minutes, filter_age_minutes, filter_life_hours
        )
        if fraction >= 1.0:
            actions.append("Replace filter — manufacturer service interval exceeded")
        elif fraction >= 0.85:
            actions.append("Schedule filter replacement — approaching service interval")

    if state.filter_change_pending_reset:
        actions.append(
            "Press filter reset button behind front panel to complete filter change record"
        )

    # Performance diagnostic actions
    if performance_degraded and diagnostic:
        recommendation = diagnostic.get("recommendation")
        if recommendation:
            actions.append(recommendation)

    # Escalation alerts — open items requiring operator response
    unresolved = [a for a in open_alerts if not a.get("resolved", False)]
    if unresolved:
        actions.append(
            f"{len(unresolved)} unresolved alert(s) require operator response"
        )

    return actions


def _filter_status(
    state: TwinState,
    device_age_minutes: Optional[int],
    filter_age_minutes: Optional[int],
    filter_life_hours: int,
) -> dict:
    """Build filter status dict shared across role views."""
    if device_age_minutes is None:
        return {
            "life_percent": None,
            "replacement_due": False,
            "pending_reset": state.filter_change_pending_reset,
            "no_anchor": no_anchor_on_record(state),
            "source": "unavailable",
            "installed_type": str(state.installed_filter_type),
        }

    age_hours, source = best_filter_age_hours(
        state, device_age_minutes, filter_age_minutes
    )
    fraction, _ = filter_life_fraction(
        state, device_age_minutes, filter_age_minutes, filter_life_hours
    )

    return {
        "age_hours": round(age_hours, 1),
        "life_hours": filter_life_hours,
        "life_percent": round(fraction * 100, 1),
        "replacement_due": fraction >= 1.0,
        "approaching_replacement": fraction >= 0.85,
        "pending_reset": state.filter_change_pending_reset,
        "no_anchor": no_anchor_on_record(state),
        "source": source,
        "installed_type": str(state.installed_filter_type),
    }


def _baseline_context(state: TwinState) -> dict:
    """Build baseline context dict for engineer view."""
    import datetime

    season_mismatch = False
    if state.baseline_locked_season is not None:
        current_month = datetime.datetime.now(datetime.timezone.utc).month
        from backend.twin_engine.models import season_from_month
        current_season = season_from_month(current_month)
        season_mismatch = current_season != state.baseline_locked_season

    return {
        "locked": state.baseline_locked is not None,
        "locked_value": state.baseline_locked,
        "locked_ts": state.baseline_locked_ts,
        "locked_season": str(state.baseline_locked_season)
                         if state.baseline_locked_season else None,
        "season_mismatch": season_mismatch,
        "current": state.baseline_current,
        "std": state.baseline_std,
        "learn_readings": state.baseline_learn_readings,
    }