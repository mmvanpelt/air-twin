/**
 * state.js — Shared twin state store and event bus.
 *
 * Single source of truth for the current twin state frame.
 * All modules read from AirTwinState and subscribe to changes.
 * No module writes state directly — only demo.js and live.js
 * call AirTwinState.update().
 */

const AirTwinState = (() => {
  // Current state frame
  let _state = {
    ts: null,
    pm25: null,
    pm25_internal: null,
    regime: 'initialising',
    confidence: 0.5,
    confidence_conclusion: 'System establishing baseline — no action required.',
    baseline_locked: null,
    baseline_current: null,
    baseline_std: null,
    baseline_locked_season: null,
    fan_speed: null,
    fan_mode: null,
    purifier_on: false,
    filter_status: {
      life_percent: null,
      replacement_due: false,
      pending_reset: false,
      no_anchor: true,
      source: 'unknown',
      installed_type: 'particle_only',
      age_hours: null,
    },
    confidence_factors: {},
    regime_summary: {},
    room_efficiency_factor: 1.0,
    commissioned_at: null,
    // Extended fields from full /state endpoint
    baseline_learn_readings: 0,
    filter_change_pending_reset: false,
    last_known_filter_age: null,
    installed_filter_type: 'particle_only',
    last_fan_speed_commanded: null,
    spike_intervention_enabled: false,
    spike_observation_count: 0,
  };

  // Subscribers keyed by event name
  const _subs = {};

  /**
   * Subscribe to state change events.
   * @param {string} event - 'update' | 'regime-change' | 'mode-change'
   * @param {Function} fn
   */
  function on(event, fn) {
    if (!_subs[event]) _subs[event] = [];
    _subs[event].push(fn);
  }

  function _emit(event, data) {
    (_subs[event] || []).forEach(fn => fn(data));
  }

  /**
   * Update state from a new frame (WebSocket message or demo frame).
   * Emits 'update' always, 'regime-change' when regime changes.
   */
  function update(frame) {
    const prevRegime = _state.regime;

    // Merge frame into state — handle both full /state endpoint
    // and compact WebSocket frames
    if (frame.current_regime !== undefined) {
      // Full state from /state endpoint
      _state = {
        ..._state,
        regime: normaliseRegime(frame.current_regime),
        confidence: frame.confidence ?? _state.confidence,
        confidence_conclusion: frame.confidence_conclusion || _state.confidence_conclusion,
        baseline_locked: frame.baseline_locked,
        baseline_current: frame.baseline_current,
        baseline_std: frame.baseline_std,
        baseline_locked_season: frame.baseline_locked_season,
        baseline_learn_readings: frame.baseline_learn_readings ?? _state.baseline_learn_readings,
        filter_change_pending_reset: frame.filter_change_pending_reset ?? false,
        last_known_filter_age: frame.last_known_filter_age,
        installed_filter_type: frame.installed_filter_type || 'particle_only',
        room_efficiency_factor: frame.room_efficiency_factor ?? 1.0,
        commissioned_at: frame.commissioned_at,
        confidence_factors: frame.confidence_factors || {},
        spike_intervention_enabled: frame.spike_intervention_enabled ?? false,
        spike_observation_count: frame.spike_observation_count ?? 0,
        last_fan_speed_commanded: frame.last_fan_speed_commanded,
      };
    } else {
      // Compact WebSocket frame (from engine._public_state)
      _state = {
        ..._state,
        asset_health: frame.asset_health || _state.asset_health,
        baseline_std: frame.baseline_std ?? _state.baseline_std,
        baseline_locked_season: frame.baseline_locked_season || _state.baseline_locked_season,
        last_known_filter_age: frame.last_known_filter_age ?? _state.last_known_filter_age,
        installed_filter_type: frame.installed_filter_type || _state.installed_filter_type,
        filter_change_pending_reset: frame.filter_change_pending_reset ?? _state.filter_change_pending_reset,
        asset_status: frame.asset_status || _state.asset_status,
        service_level_compliance_pct: frame.service_level_compliance_pct ?? _state.service_level_compliance_pct,
        monthly_energy_kwh: frame.monthly_energy_kwh ?? _state.monthly_energy_kwh,
        monthly_cost_usd: frame.monthly_cost_usd ?? _state.monthly_cost_usd,
        ts: frame.ts || _state.ts,
        pm25: frame.pm25 ?? frame.value ?? _state.pm25,
        pm25_internal: frame.pm25_internal ?? _state.pm25_internal,
        regime: normaliseRegime(frame.regime || _deriveRegime(frame, _state)),
        confidence: frame.confidence ?? _state.confidence,
        confidence_conclusion: frame.confidence_conclusion || _state.confidence_conclusion,
        baseline_locked: frame.baseline_locked ?? _state.baseline_locked,
        baseline_current: frame.baseline_current ?? _state.baseline_current,
        fan_speed: frame.fan_speed ?? _state.fan_speed,
        fan_mode: frame.fan_mode ?? _state.fan_mode,
        purifier_on: frame.purifier_on != null ? Boolean(frame.purifier_on) : _state.purifier_on,
        filter_status: frame.filter_status || _state.filter_status,
        confidence_factors: frame.confidence_factors || _state.confidence_factors,
        regime_summary: frame.regime_summary || _state.regime_summary,
        room_efficiency_factor: frame.room_efficiency_factor ?? _state.room_efficiency_factor,
        commissioned_at: frame.commissioned_at || _state.commissioned_at,
      };
    }

    _emit('update', _state);

    if (_state.regime !== prevRegime) {
      _emit('regime-change', { from: prevRegime, to: _state.regime });
    }
  }

  /** Normalise regime string from various formats. */
  function normaliseRegime(raw) {
    if (!raw) return 'initialising';
    return String(raw)
      .toLowerCase()
      .replace('regimetype.', '')
      .trim();
  }

  /** Get current state (read-only snapshot). */
  function get() {
    return { ..._state };
  }

  /**
   * Map regime string to CSS class and display label.
   */
  function regimeClass(regime) {
    const map = {
      baseline:     'baseline',
      event:        'event',
      degraded:     'degraded',
      initialising: 'initialising',
      validating:   'validating',
      unknown:      'unknown',
    };
    return map[regime] || 'unknown';
  }

  function regimeLabel(regime) {
    const map = {
      baseline:     'BASELINE',
      event:        'EVENT',
      degraded:     'DEGRADED',
      initialising: 'INITIALISING',
      validating:   'VALIDATING',
      unknown:      'UNKNOWN',
    };
    return map[regime] || regime.toUpperCase();
  }

  /**
   * Map regime + confidence to accent colour.
   */
  function regimeColor(regime, confidence) {
    const colors = {
      baseline:     confidence > 0.7 ? '#22c55e' : '#14b8a6',
      event:        '#eab308',
      degraded:     '#f97316',
      initialising: '#3b82f6',
      validating:   '#3b82f6',
      unknown:      '#6b7280',
    };
    if (confidence < 0.3) return '#ef4444';
    return colors[regime] || '#6b7280';
  }

  function _deriveRegime(frame, currentState) {
    if (currentState.baseline_locked == null) return 'initialising';
    const val = frame.value || frame.pm25 || 0;
    const baseline = currentState.baseline_locked;
    const std = currentState.baseline_std || 1;
    const deviation = (val - baseline) / std;
    if (deviation > 3) return 'event';
    return 'baseline';
  }
  
  return { on, update, get, normaliseRegime, regimeClass, regimeLabel, regimeColor };
})();