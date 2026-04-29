/**
 * components.js — Shared render functions for Air Twin HUD.
 *
 * Each component:
 *   - Owns one piece of data (filter life, device age, costs, etc.)
 *   - Renders to any DOM element passed to it
 *   - Is called by view functions — never calls view functions itself
 *   - Uses a single source of truth for its data
 *
 * Data priority for shared fields:
 *   1. asset_health from /brief/executive (most complete, computed server-side)
 *   2. filter_status from WebSocket frame (real-time, less complete)
 *   3. Raw state fields (fallback)
 */

const AirTwinComponents = (() => {

  // ---------------------------------------------------------------------------
  // Data accessors — single source of truth for each shared field
  // ---------------------------------------------------------------------------

  function _assetHealth(state) {
    return state.asset_health || window._lastAssetHealth || null;
  }

  function _filterData(state) {
    const ah = _assetHealth(state);
    return ah?.filter || null;
  }

  function _deviceData(state) {
    const ah = _assetHealth(state);
    return ah?.device || null;
  }

  function _costsData(state) {
    const ah = _assetHealth(state);
    return ah?.costs || null;
  }

  function _serviceLevel(state) {
    const ah = _assetHealth(state);
    return ah?.service_level || null;
  }

  // Filter life remaining % — single calculation used everywhere
  function filterRemainingPct(state) {
    // Priority 1: pre-calculated from WebSocket frame
    if (state.filter_life_remaining_pct != null) return state.filter_life_remaining_pct;
    // Priority 2: from brief asset_health
    const fd = _filterData(state);
    if (fd?.life_remaining_pct != null) return fd.life_remaining_pct;
    // Priority 3: calculate from filter_status
    const fs = state.filter_status;
    if (fs?.filter_life_percent != null) return Math.max(0, 100 - fs.filter_life_percent);
    return null;
  }

  // Device age in hours — single calculation used everywhere
  function deviceAgeHours(state) {
    const dev = _deviceData(state);
    if (dev?.age_hours != null) return dev.age_hours;
    if (state.last_known_device_age != null) return state.last_known_device_age / 60;
    if (state.last_known_filter_age != null) return state.last_known_filter_age / 60;
    return null;
  }

  // Filter age in hours
  function filterAgeHours(state) {
    const fs = state.filter_status;
    if (fs?.twin_filter_age_hours != null) return fs.twin_filter_age_hours;
    if (state.last_known_filter_age != null) return state.last_known_filter_age / 60;
    return null;
  }

  // Asset status label and color
  function assetStatusLabel(state) {
    const status = state.asset_status || 'unknown';
    const labels = {
      operating_normally: 'Operating normally',
      responding:         'Responding to air quality event',
      performance_low:    'Performance below expected',
      filter_due:         'Filter replacement due',
      offline:            'Offline — check connections',
      unknown:            'Status unknown',
    };
    return labels[status] || status.replace(/_/g, ' ');
  }

  function assetStatusColor(state) {
    const status = state.asset_status || 'unknown';
    const colors = {
      operating_normally: 'var(--c-baseline-hi)',
      responding:         'var(--c-event)',
      performance_low:    'var(--c-degraded)',
      filter_due:         'var(--c-degraded)',
      offline:            'var(--c-critical)',
      unknown:            'var(--c-unknown)',
    };
    return colors[status] || 'var(--text-secondary)';
  }

  // ---------------------------------------------------------------------------
  // Shared render functions — called by any view that needs this data
  // ---------------------------------------------------------------------------

  /**
   * Render filter life bar + percentage.
   * barId: the progress bar fill element
   * pctId: the percentage text element
   */
  function renderFilterLifeBar(state, barId, pctId) {
    const pct = filterRemainingPct(state);
    const barEl = document.getElementById(barId);
    const pctEl = document.getElementById(pctId);

    if (barEl && pct != null) {
      barEl.style.width = `${Math.max(0, pct)}%`;
      barEl.style.background = pct < 15 ? 'var(--c-degraded)' :
                                pct < 30 ? 'var(--c-event)' :
                                'var(--c-baseline-hi)';
    }
    if (pctEl) {
      pctEl.textContent = pct != null ? `${pct.toFixed(0)}% remaining` : '—';
    }
  }

  /**
   * Render filter age in hours.
   * elementId: text element to update
   * verbose: if true, include source note for engineer view
   */
  function renderFilterAge(state, elementId, verbose = false) {
    const el = document.getElementById(elementId);
    if (!el) return;
    const hours = filterAgeHours(state);
    if (hours == null) { el.textContent = '—'; return; }
    el.textContent = verbose ? `${hours.toFixed(0)}h` : `${hours.toFixed(0)}h`;
  }

  /**
   * Render filter replacement info.
   * weeksId: weeks to replacement element
   * costId: replacement cost element
   */
  function renderFilterReplacement(state, weeksId, costId) {
    const fd = _filterData(state);
    const weeksEl = document.getElementById(weeksId);
    const costEl = document.getElementById(costId);

    if (weeksEl) {
      weeksEl.textContent = fd?.weeks_to_replacement != null ?
        `Est. replacement in ${fd.weeks_to_replacement} weeks` : '—';
    }
    if (costEl) {
      // Filter is a discrete purchase, not a monthly cost
      if (fd?.replacement_cost_low != null) {
        costEl.textContent = `Filter replacement: $${fd.replacement_cost_low}–$${fd.replacement_cost_high} per unit`;
      } else {
        costEl.textContent = '—';
      }
    }
  }

  /**
   * Render device age in hours.
   * elementId: text element to update
   */
  function renderDeviceAge(state, elementId) {
    const el = document.getElementById(elementId);
    if (!el) return;
    const hours = deviceAgeHours(state);
    el.textContent = hours != null ? `${hours.toFixed(0)}h` : '—';
  }

  /**
   * Render asset life remaining bar + percentage + years.
   * barId: progress bar fill element (optional)
   * pctId: percentage text element (optional)
   * yearsId: years remaining text element (optional)
   */
  function renderAssetLife(state, barId, pctId, yearsId) {
    const dev = _deviceData(state);
    const pct = dev?.life_remaining_pct ?? null;
    const years = dev?.years_remaining ?? null;

    if (barId) {
      const barEl = document.getElementById(barId);
      if (barEl && pct != null) {
        barEl.style.width = `${Math.max(0, pct)}%`;
        barEl.style.background = pct < 20 ? 'var(--c-degraded)' :
                                  pct < 35 ? 'var(--c-event)' :
                                  'var(--c-initialising)';
      }
    }
    if (pctId) {
      const pctEl = document.getElementById(pctId);
      if (pctEl) pctEl.textContent = pct != null ? `${pct.toFixed(0)}%` : '—';
    }
    if (yearsId) {
      const yearsEl = document.getElementById(yearsId);
      if (yearsEl) yearsEl.textContent = years != null ? `${years.toFixed(1)} years` : '—';
    }
  }

  /**
   * Render operating costs.
   * mode: 'engineer' (shows energy/month, filter/unit, total/year, kWh)
   *       'executive' (shows energy/month, filter note, total/year)
   */
  function renderCosts(state, mode) {
    const costs = _costsData(state);
    const fd = _filterData(state);

    if (mode === 'engineer') {
      _setText('eng-cost-energy', costs?.energy_monthly_usd != null ?
        `$${costs.energy_monthly_usd.toFixed(2)}/mo` : '—');

      // Filter is a discrete purchase — show per unit cost not monthly
      if (fd?.replacement_cost_low != null) {
        _setText('eng-cost-filter',
          `$${fd.replacement_cost_low}–$${fd.replacement_cost_high} per filter`);
      } else {
        _setText('eng-cost-filter', '—');
      }

      _setText('eng-cost-total', costs?.total_annual_usd != null ?
        `$${costs.total_annual_usd.toFixed(2)}/yr` : '—');
      _setText('eng-cost-kwh', state.monthly_energy_kwh != null ?
        `${state.monthly_energy_kwh.toFixed(2)} kWh/mo` : '—');

    } else if (mode === 'executive') {
      _setText('exec-cost-energy', costs?.energy_monthly_usd != null ?
        `$${costs.energy_monthly_usd.toFixed(2)}/mo` : '—');

      // Filter — show as discrete purchase with timing
      if (fd?.replacement_cost_low != null && fd?.weeks_to_replacement != null) {
        _setText('exec-cost-filter',
          `$${fd.replacement_cost_low}–$${fd.replacement_cost_high} in ~${fd.weeks_to_replacement} weeks`);
      } else {
        _setText('exec-cost-filter', '—');
      }

      _setText('exec-cost-total', costs?.energy_monthly_usd != null ?
        `$${costs.energy_monthly_usd.toFixed(2)}/mo energy` : '—');
      _setText('exec-cost-annual', costs?.total_annual_usd != null ?
        `$${costs.total_annual_usd.toFixed(2)}/yr total` : '—');
    }
  }

  /**
   * Render service level compliance.
   * Shared between engineer and executive views.
   * prefix: element ID prefix ('eng' or 'exec')
   */
  function renderServiceLevel(state, prefix) {
    const sl = _serviceLevel(state);

    // Fallback to raw state if brief not yet loaded
    const compliance = sl?.compliance_pct ?? state.service_level_compliance_pct ?? null;
    const target = sl?.target_pct ?? 95;
    const met = sl?.met ?? (compliance != null ? compliance >= target : null);

    if (prefix === 'exec') {
      const barEl = document.getElementById('exec-service-bar');
      const pctEl = document.getElementById('exec-service-pct');
      const labelEl = document.getElementById('exec-service-label');

      if (barEl && compliance != null) {
        barEl.style.width = `${compliance}%`;
        barEl.style.background = met ? 'var(--c-baseline-hi)' : 'var(--c-degraded)';
      }
      if (pctEl) pctEl.textContent = compliance != null ? `${compliance.toFixed(1)}%` : '—';
      if (labelEl) {
        if (compliance == null) {
          labelEl.textContent = 'Service level data loading...';
        } else if (met) {
          labelEl.textContent = `Air quality SLA met — ${compliance.toFixed(1)}% compliance vs ${target}% target (30-day rolling)`;
        } else {
          labelEl.textContent = `⚠ Air quality SLA breach — ${compliance.toFixed(1)}% vs ${target}% target. Review air quality events.`;
        }
      }
    } else if (prefix === 'eng') {
      _setText('eng-service-level', compliance != null ?
        `${compliance.toFixed(1)}% (30-day vs ${target}% target)` : '—');
    }
  }

  /**
   * Render asset status.
   * Shared between engineer and executive views.
   */
  function renderAssetStatus(state, statusId, metaId) {
    const statusEl = document.getElementById(statusId);
    const metaEl = document.getElementById(metaId);
    const dev = _deviceData(state);

    if (statusEl) {
      statusEl.textContent = assetStatusLabel(state);
      statusEl.style.color = assetStatusColor(state);
    }
    if (metaEl) {
      metaEl.textContent = dev?.years_remaining != null ?
        `Est. ${dev.years_remaining.toFixed(1)} years remaining asset life` : '—';
    }
  }

  /**
   * Render filter type label — normalises enum strings.
   */
  function renderFilterType(state, elementId) {
    const el = document.getElementById(elementId);
    if (!el) return;
    const raw = state.installed_filter_type ||
                state.filter_status?.installed_filter_type || '—';
    el.textContent = raw
      .replace('FilterType.', '')
      .replace(/_/g, ' ')
      .toLowerCase();
  }

  // ---------------------------------------------------------------------------
  // Utility
  // ---------------------------------------------------------------------------

  function _setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  }

  // ---------------------------------------------------------------------------
  // Public API
  // ---------------------------------------------------------------------------

  return {
    // Data accessors
    filterRemainingPct,
    deviceAgeHours,
    filterAgeHours,
    assetStatusLabel,
    assetStatusColor,

    // Render functions
    renderFilterLifeBar,
    renderFilterAge,
    renderFilterReplacement,
    renderDeviceAge,
    renderAssetLife,
    renderCosts,
    renderServiceLevel,
    renderAssetStatus,
    renderFilterType,
  };

})();
