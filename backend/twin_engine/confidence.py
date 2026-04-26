"""
confidence.py — Evidence-based confidence scoring for the Air Twin twin engine.

Owns all logic related to confidence score calculation and maintenance.
Receives signals from baseline.py, performance.py, and filter.py, applies
evidence weights from config, and returns an updated TwinState with a new
confidence score and updated confidence_factors dict.

Confidence is a continuous float 0.0–1.0 updated via exponential smoothing
on every reading cycle. It is evidence-based, not time-based — it degrades
only when evidence suggests something is wrong, not on a schedule.

The confidence_factors dict in TwinState records the last evidence delta
per factor so the engineer view can show exactly why confidence is at its
current level. This is the core of the twin's defensibility.

No I/O — never reads or writes files directly. engine.py is the sole
I/O boundary.
"""

import logging
from dataclasses import replace
from typing import Optional

from twin_engine.models import (
    EvidenceDelta,
    PlausibilityReason,
    Reading,
    TwinState,
    utc_now,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Confidence tier definitions — used by brief_generator.py
# ---------------------------------------------------------------------------

CONFIDENCE_TIERS = [
    (0.9, 1.0, "Air quality is good."),
    (0.7, 0.9, "Air quality is good. Monitoring stable."),
    (0.5, 0.7, "Air quality appears good. Baseline review recommended."),
    (0.3, 0.5, "Air quality assessment uncertain. Engineer review required."),
    (0.0, 0.3, "Insufficient confidence to assess. Baseline re-establishment required."),
]


def confidence_conclusion(confidence: float) -> str:
    """
    Return the tiered conclusion string for a given confidence score.
    Used by brief_generator.py to produce the executive brief conclusion.
    """
    for low, high, conclusion in CONFIDENCE_TIERS:
        if low <= confidence <= high:
            return conclusion
    return CONFIDENCE_TIERS[-1][2]


# ---------------------------------------------------------------------------
# Public — main update function called once per reading cycle by engine.py
# ---------------------------------------------------------------------------

def update(
    state: TwinState,
    reading: Reading,
    signals: list[str],
    baseline_signals: list[str],
    filter_signals: list[str],
    reading_gap_minutes: float,
    sensor_cross_reference_threshold: float,
    evidence_weights: dict,
    smoothing_alpha: float,
    season_mismatch: bool,
) -> TwinState:
    """
    Update confidence score from all evidence sources for one reading cycle.

    Collects evidence deltas from:
      - Reading plausibility (from sds011_reader.py qualification)
      - Sensor cross-reference agreement (SDS011 vs pm25_internal)
      - Rolling std stability
      - Reading schedule adherence
      - Signals from baseline.py (divergence, RoC guard)
      - Signals from performance.py (performance ratio)
      - Signals from filter.py (filter age divergence)
      - Reading gaps
      - Seasonal mismatch

    Applies exponential smoothing to produce updated confidence score.
    Stores all deltas in confidence_factors for engineer view.

    Args:
        state:                           Current TwinState
        reading:                         Current reading
        signals:                         Signals from performance.py
        baseline_signals:                Signals from baseline.py
        filter_signals:                  Signals from filter.py
        reading_gap_minutes:             Minutes since last reading (0 if on schedule)
        sensor_cross_reference_threshold: Max µg/m³ difference for sensor agreement
        evidence_weights:                Dict of factor -> weight from config.json
        smoothing_alpha:                 EMA learning rate for confidence
        season_mismatch:                 True if current season != baseline lock season

    Returns:
        Updated TwinState with new confidence score and confidence_factors
    """
    deltas: list[EvidenceDelta] = []
    all_signals = set(signals) | set(baseline_signals) | set(filter_signals)

    # --- Positive evidence ---

    # Plausibility
    if (reading.is_plausible is True
            and reading.plausibility_reason == PlausibilityReason.OK):
        deltas.append(EvidenceDelta(
            factor="plausibility_ok",
            delta=evidence_weights.get("plausibility_ok", 0.002),
            reason="Reading passed full Pi plausibility check",
        ))

    # Sensor cross-reference agreement
    if reading.pm25_internal is not None and reading.value is not None:
        diff = abs(reading.value - reading.pm25_internal)
        if diff <= sensor_cross_reference_threshold:
            deltas.append(EvidenceDelta(
                factor="sensor_cross_reference_agree",
                delta=evidence_weights.get("sensor_cross_reference_agree", 0.002),
                reason=f"SDS011 and pm25_internal agree within "
                       f"{sensor_cross_reference_threshold} µg/m³ "
                       f"(diff={diff:.1f})",
            ))
        else:
            deltas.append(EvidenceDelta(
                factor="sensor_cross_reference_disagree",
                delta=evidence_weights.get("sensor_cross_reference_disagree", -0.008),
                reason=f"SDS011 ({reading.value:.1f}) and pm25_internal "
                       f"({reading.pm25_internal:.1f}) disagree by {diff:.1f} µg/m³ "
                       f"(threshold={sensor_cross_reference_threshold})",
            ))

    # Rolling std stability
    if reading.rolling_std is not None and reading.rolling_std < 2.0:
        deltas.append(EvidenceDelta(
            factor="rolling_std_low",
            delta=evidence_weights.get("rolling_std_low", 0.001),
            reason=f"Rolling std is low ({reading.rolling_std:.2f} µg/m³) "
                   f"— environment stable",
        ))

    # Readings arriving on schedule
    if reading_gap_minutes <= 1.0:
        deltas.append(EvidenceDelta(
            factor="readings_on_schedule",
            delta=evidence_weights.get("readings_on_schedule", 0.001),
            reason="Readings arriving on schedule",
        ))

    # --- Negative evidence from signals ---

    if "baseline_divergence_sustained" in all_signals:
        deltas.append(EvidenceDelta(
            factor="baseline_divergence_sustained",
            delta=evidence_weights.get("baseline_divergence_sustained", -0.010),
            reason="baseline_current diverging from baseline_locked",
        ))

    if "roc_guard_breach" in all_signals:
        deltas.append(EvidenceDelta(
            factor="roc_guard_breach",
            delta=evidence_weights.get("roc_guard_breach", -0.012),
            reason="Rate-of-change guard breached — baseline rising faster than expected",
        ))

    if "performance_ratio_below_threshold" in all_signals:
        deltas.append(EvidenceDelta(
            factor="performance_ratio_below_threshold",
            delta=evidence_weights.get("performance_ratio_below_threshold", -0.008),
            reason="Observed purifier performance below expected threshold",
        ))

    if "filter_age_divergence" in all_signals:
        deltas.append(EvidenceDelta(
            factor="filter_age_divergence",
            delta=evidence_weights.get("filter_age_divergence", -0.006),
            reason="filter_age and twin_filter_age diverging",
        ))

    # Sustained implausible readings
    if reading.is_plausible is False:
        deltas.append(EvidenceDelta(
            factor="sustained_implausible_readings",
            delta=evidence_weights.get("sustained_implausible_readings", -0.015),
            reason=f"Reading implausible: {reading.plausibility_reason}",
        ))

    # Reading gap — proportional to gap length
    if reading_gap_minutes > 1.0:
        gap_delta = (evidence_weights.get("reading_gap_per_minute", -0.005)
                     * reading_gap_minutes)
        gap_delta = max(gap_delta, -0.5)  # floor — single gap can't destroy confidence
        deltas.append(EvidenceDelta(
            factor="reading_gap",
            delta=gap_delta,
            reason=f"Reading gap of {reading_gap_minutes:.1f} minutes detected",
        ))

    # Seasonal mismatch
    if season_mismatch:
        deltas.append(EvidenceDelta(
            factor="seasonal_mismatch",
            delta=evidence_weights.get("seasonal_mismatch", -0.002),
            reason="Current season differs from baseline lock season",
        ))

    # --- Apply all deltas via exponential smoothing ---
    total_delta = sum(d.delta for d in deltas)
    raw_target = state.confidence + total_delta
    raw_target = max(0.0, min(1.0, raw_target))  # clamp before smoothing

    new_confidence = (smoothing_alpha * raw_target
                      + (1.0 - smoothing_alpha) * state.confidence)
    new_confidence = max(0.0, min(1.0, new_confidence))  # clamp after smoothing

    # --- Update confidence_factors dict ---
    now = utc_now()
    factors = dict(state.confidence_factors)
    for delta in deltas:
        factors[delta.factor] = {
            "delta": delta.delta,
            "reason": delta.reason,
            "ts": now,
        }

    logger.debug(f"Confidence: {state.confidence:.3f} → {new_confidence:.3f} "
                 f"(total_delta={total_delta:+.4f}, "
                 f"factors={len(deltas)})")

    return replace(
        state,
        confidence=new_confidence,
        confidence_factors=factors,
    )


# ---------------------------------------------------------------------------
# Public — query helpers
# ---------------------------------------------------------------------------

def dominant_negative_factor(state: TwinState) -> Optional[tuple[str, float, str]]:
    """
    Return the single most negative confidence factor currently active.
    Used by brief_generator.py and engineer view to surface the primary
    reason confidence is not higher.

    Returns:
        Tuple of (factor_name, delta, reason) or None if no negative factors
    """
    negative = [
        (k, v["delta"], v["reason"])
        for k, v in state.confidence_factors.items()
        if v["delta"] < 0
    ]
    if not negative:
        return None
    return min(negative, key=lambda x: x[1])  # most negative delta


def all_active_factors(state: TwinState) -> list[dict]:
    """
    Return all confidence factors sorted by delta (most negative first).
    Used by engineer view to show full confidence breakdown.
    """
    factors = [
        {
            "factor": k,
            "delta": v["delta"],
            "reason": v["reason"],
            "ts": v.get("ts", ""),
        }
        for k, v in state.confidence_factors.items()
    ]
    return sorted(factors, key=lambda x: x["delta"])