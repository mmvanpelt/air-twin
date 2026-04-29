/**
 * ui.js — HUD panels, role tabs, and all DOM updates.
 *
 * Subscribes to AirTwinState and updates every panel reactively.
 * Shared rendering logic lives in components.js.
 * This file handles only view-specific elements and orchestration.
 *
 * No direct state writes — reads only.
 */

const AirTwinUI = (() => {

  function init() {
    _initRoleTabs();
    AirTwinState.on('update', _onStateUpdate);
    AirTwinState.on('regime-change', _onRegimeChange);
    _initMaintenanceForm();
  }

  // ---------------------------------------------------------------------------
  // Role tab switching
  // ---------------------------------------------------------------------------

  function _initRoleTabs() {
    const tabs = document.querySelectorAll('.tab');
    tabs.forEach(tab => {
      tab.addEventListener('click', () => {
        const role = tab.dataset.role;
        tabs.forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        document.querySelectorAll('.role-view').forEach(v => {
          v.classList.toggle('active', v.dataset.role === role);
        });
      });
    });
  }

  // ---------------------------------------------------------------------------
  // Main state update handler
  // ---------------------------------------------------------------------------

  function _onStateUpdate(state) {
    _updateHeader(state);
    _updateRegimeBadge(state);
    _updateOperator(state);
    _updateExecutive(state);
    _updateEngineer(state);
    _updateTechnician(state);
  }

  function _onRegimeChange({ from, to }) {
    const badge = document.getElementById('regime-badge');
    if (badge) {
      badge.style.transform = 'scale(1.08)';
      setTimeout(() => { badge.style.transform = ''; }, 300);
    }
  }

  // ---------------------------------------------------------------------------
  // Header
  // ---------------------------------------------------------------------------

  function _updateHeader(state) {
    const tsEl = document.getElementById('ts-display');
    if (tsEl && state.ts) {
      try {
        const d = new Date(state.ts);
        tsEl.textContent = d.toLocaleTimeString('en-GB', { hour12: false });
      } catch (e) {
        tsEl.textContent = '--:--:--';
      }
    }
  }

  // ---------------------------------------------------------------------------
  // Regime badge
  // ---------------------------------------------------------------------------

  function _updateRegimeBadge(state) {
    const badge = document.getElementById('regime-badge');
    if (!badge) return;
    const cls = AirTwinState.regimeClass(state.regime);
    badge.className = `regime-badge ${cls}`;
    badge.textContent = AirTwinState.regimeLabel(state.regime);
  }

  // ---------------------------------------------------------------------------
  // Operator view
  // ---------------------------------------------------------------------------

  function _updateOperator(state) {
    // PM2.5
    const pm25El = document.getElementById('op-pm25');
    if (pm25El) {
      pm25El.textContent = state.pm25 != null ? state.pm25.toFixed(1) : '—';
      pm25El.style.color = _pm25Color(state.pm25);
    }

    // Regime
    const regimeEl = document.getElementById('op-regime');
    if (regimeEl) {
      regimeEl.textContent = AirTwinState.regimeLabel(state.regime);
      regimeEl.style.color = AirTwinState.regimeColor(state.regime, state.confidence);
    }

    // Fan
    const fanEl = document.getElementById('op-fan');
    if (fanEl) {
      if (state.purifier_on && state.fan_speed != null) {
        const isAuto = state.fan_mode === 'auto';
        fanEl.textContent = `${state.fan_speed} ${isAuto ? 'A' : 'M'}`;
        fanEl.style.color = 'var(--text-primary)';
      } else if (state.purifier_on === false) {
        fanEl.textContent = 'OFF';
        fanEl.style.color = 'var(--text-dim)';
      } else {
        fanEl.textContent = '—';
        fanEl.style.color = 'var(--text-dim)';
      }
    }

    // Conclusion
    const concEl = document.getElementById('op-conclusion');
    if (concEl) {
      concEl.textContent = state.confidence_conclusion || '—';
      concEl.style.borderLeftColor = AirTwinState.regimeColor(state.regime, state.confidence);
    }

    // Filter life bar — shared component
    AirTwinComponents.renderFilterLifeBar(state, 'op-filter-bar', 'op-filter-pct');

    // Filter meta — simple status note
    const metaEl = document.getElementById('op-filter-meta');
    if (metaEl) {
      const fs = state.filter_status;
      const parts = [];
      if (fs?.no_anchor_on_record) parts.push('no change on record');
      if (state.filter_change_pending_reset) parts.push('reset pending');
      metaEl.textContent = parts.join(' · ') || '';
    }

    // Alerts
    const alertsEl = document.getElementById('op-alerts');
    if (alertsEl) {
      const alerts = _buildAlerts(state);
      alertsEl.innerHTML = alerts.length === 0 ?
        '<div class="alert-empty">No active alerts</div>' :
        alerts.map(a => `<div class="alert-item">${_esc(a)}</div>`).join('');
    }
  }

  // ---------------------------------------------------------------------------
  // Executive view
  // ---------------------------------------------------------------------------

  function _updateExecutive(state) {
    // Air quality conclusion
    _setText('exec-conclusion', state.confidence_conclusion || '—');

    // Asset status — shared component
    AirTwinComponents.renderAssetStatus(state, 'exec-asset-status', 'exec-asset-meta');

    // Asset life bar
    AirTwinComponents.renderAssetLife(state, 'exec-asset-life-bar', 'exec-asset-life-pct', 'exec-asset-life-years');

    // Costs — executive format (discrete filter purchase)
    AirTwinComponents.renderCosts(state, 'executive');

    // Service level — shared component
    AirTwinComponents.renderServiceLevel(state, 'exec');

    // Required actions — from brief if available, else build locally
    const actionsEl = document.getElementById('exec-actions');
    if (actionsEl) {
      const actions = state.executive_actions?.length ?
        state.executive_actions : _buildExecActions(state);
      actionsEl.innerHTML = actions.length === 0 ?
        '<div class="action-empty">No actions required</div>' :
        actions.map(a => `<div class="action-item">${_esc(a)}</div>`).join('');
    }
  }

  // ---------------------------------------------------------------------------
  // Engineer view
  // ---------------------------------------------------------------------------

  function _updateEngineer(state) {
    // Confidence arc
    const arcEl = document.getElementById('eng-arc');
    const valEl = document.getElementById('eng-confidence-val');
    if (arcEl && state.confidence != null) {
      const pct = Math.max(0, Math.min(1, state.confidence));
      const arcLen = 126;
      const filled = pct * arcLen;
      arcEl.style.strokeDasharray = `${filled} ${arcLen - filled + 1}`;
      arcEl.style.stroke = AirTwinState.regimeColor(state.regime, state.confidence);
    }
    if (valEl && state.confidence != null) {
      valEl.textContent = `${(state.confidence * 100).toFixed(0)}%`;
    }

    // Confidence factors
    const factorsEl = document.getElementById('eng-factors');
    if (factorsEl && state.confidence_factors) {
      const factors = Object.entries(state.confidence_factors)
        .map(([k, v]) => ({ key: k, delta: v.delta || 0, reason: v.reason || '' }))
        .sort((a, b) => a.delta - b.delta);

      factorsEl.innerHTML = factors.length === 0 ?
        '<div style="color:var(--text-dim);font-size:11px">No factors yet</div>' :
        factors.map(f => {
          const sign = f.delta >= 0 ? '+' : '';
          const cls = f.delta >= 0 ? 'pos' : 'neg';
          return `<div class="factor-row" title="${_esc(f.reason)}">
            <span class="factor-name">${_esc(f.key.replace(/_/g, ' '))}</span>
            <span class="factor-delta ${cls}">${sign}${f.delta.toFixed(3)}</span>
          </div>`;
        }).join('');
    }

    // Baseline
    _setText('bl-locked', state.baseline_locked != null ?
      `${state.baseline_locked.toFixed(2)} µg/m³` : 'Not locked');
    _setText('bl-current', state.baseline_current != null ?
      `${state.baseline_current.toFixed(2)} µg/m³` : '—');
    _setText('bl-std', state.baseline_std != null ?
      `±${state.baseline_std.toFixed(2)}` : '—');
    _setText('bl-season', state.baseline_locked_season || '—');

    // Performance
    _setText('eng-ref', state.room_efficiency_factor != null ?
      state.room_efficiency_factor.toFixed(3) : '—');

    // Filter age — shared component
    AirTwinComponents.renderFilterAge(state, 'eng-filter-age');

    // Filter life remaining % — not usage %
    const fs = state.filter_status;
    const remaining = fs?.filter_life_percent != null ?
      (100 - fs.filter_life_percent).toFixed(0) : null;
    _setText('eng-filter-life', remaining != null ? `${remaining}% remaining` : '—');

    // Filter type
    AirTwinComponents.renderFilterType(state, null); // handled inline below
    if (fs?.installed_filter_type) {
      // no separate element for engineer — shown in filter-age context
    }

    // Asset status — shared component
    _setText('eng-asset-status', AirTwinComponents.assetStatusLabel(state));

    // Asset life — shared component
    AirTwinComponents.renderAssetLife(state, null, 'eng-device-life-pct', 'eng-device-years');

    // Service level — shared component
    AirTwinComponents.renderServiceLevel(state, 'eng');

    // Costs — engineer format
    AirTwinComponents.renderCosts(state, 'engineer');
  }

  // ---------------------------------------------------------------------------
  // Technician view
  // ---------------------------------------------------------------------------

  function _updateTechnician(state) {
    // Actions
    const actionsEl = document.getElementById('tech-actions');
    if (actionsEl) {
      const actions = _buildTechActions(state);
      actionsEl.innerHTML = actions.length === 0 ?
        '<div class="action-empty">No pending actions</div>' :
        actions.map(a => `<div class="action-item">${_esc(a)}</div>`).join('');
    }

    // Filter life bar — shared component
    AirTwinComponents.renderFilterLifeBar(state, 'tech-filter-bar', 'tech-filter-pct');

    // Filter replacement info — shared component
    AirTwinComponents.renderFilterReplacement(state, 'tech-filter-weeks', 'tech-filter-cost');

    // Device status
    _setText('tech-commissioned', state.commissioned_at ?
      new Date(state.commissioned_at).toLocaleDateString() : '—');

    // Filter type — shared component
    AirTwinComponents.renderFilterType(state, 'tech-filter-type');

    // Filter age — shared component
    AirTwinComponents.renderFilterAge(state, 'tech-filter-age');

    // Device age — shared component
    AirTwinComponents.renderDeviceAge(state, 'tech-device-age');

    // Pending reset
    _setText('tech-pending-reset', state.filter_change_pending_reset ? '⚠ YES' : 'No');
    const pendingEl = document.getElementById('tech-pending-reset');
    if (pendingEl) {
      pendingEl.style.color = state.filter_change_pending_reset ?
        'var(--c-event)' : 'var(--text-secondary)';
    }
  }

  // ---------------------------------------------------------------------------
  // Maintenance form
  // ---------------------------------------------------------------------------

  function _initMaintenanceForm() {
    const btn = document.getElementById('tech-log-change');
    if (!btn) return;

    btn.addEventListener('click', async () => {
      const filterType = document.getElementById('tech-filter-select')?.value || 'particle_only';
      const resultEl = document.getElementById('tech-maint-result');
      const state = AirTwinState.get();

      btn.disabled = true;
      btn.textContent = 'LOGGING...';

      try {
        const resp = await fetch('http://localhost:8000/maintenance', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            device_age_minutes: state.last_known_device_age ||
                                state.last_known_filter_age || 0,
            filter_type: filterType,
            actor: 'technician',
          }),
        });
        const data = await resp.json();
        if (resultEl) {
          resultEl.textContent = data.message || 'Logged successfully';
          resultEl.style.color = 'var(--c-baseline-hi)';
        }
      } catch (e) {
        if (resultEl) {
          resultEl.textContent = 'Backend unavailable in demo mode';
          resultEl.style.color = 'var(--text-dim)';
        }
      }

      btn.disabled = false;
      btn.textContent = 'LOG CHANGE';
    });
  }

  // ---------------------------------------------------------------------------
  // Alert and action builders
  // ---------------------------------------------------------------------------

  function _buildAlerts(state) {
    const alerts = [];
    if (state.regime === 'degraded') alerts.push('Air quality degraded — investigate source');
    if (state.regime === 'event') alerts.push('Temporary air quality event in progress');
    if (state.filter_status?.replacement_due) alerts.push('Filter replacement due');
    if (state.filter_change_pending_reset) alerts.push('Filter reset button not pressed after change');
    if (state.confidence < 0.3) alerts.push('Twin confidence critically low — engineer review required');
    return alerts;
  }

  function _buildExecActions(state) {
    // Forward-looking financial actions — fallback if brief not loaded
    const actions = [];
    const ah = state.asset_health || window._lastAssetHealth;
    if (!ah) return actions;

    const dev = ah.device || {};
    const fd = ah.filter || {};
    const sl = ah.service_level || {};

    const lifePct = dev.life_remaining_pct || 100;
    const yearsLeft = dev.years_remaining || 99;
    if (lifePct < 10) {
      actions.push(`Asset replacement required — ${lifePct.toFixed(0)}% life remaining.`);
    } else if (lifePct < 20) {
      actions.push(`Asset replacement approaching — ${lifePct.toFixed(0)}% remaining (est. ${yearsLeft.toFixed(1)} years). Begin procurement planning.`);
    } else if (lifePct < 35) {
      actions.push(`Asset life at ${lifePct.toFixed(0)}% — include replacement in next annual budget.`);
    }

    const filterPct = fd.life_remaining_pct || 100;
    const weeks = fd.weeks_to_replacement || 999;
    const costLow = fd.replacement_cost_low || 0;
    const costHigh = fd.replacement_cost_high || 0;
    if (filterPct < 15) {
      actions.push(`Filter replacement imminent — order now ($${costLow}–$${costHigh} per unit).`);
    } else if (filterPct < 30) {
      actions.push(`Filter replacement in ~${weeks} weeks ($${costLow}–$${costHigh} per unit).`);
    }

    if (!sl.met && sl.compliance_pct != null) {
      actions.push(`Service level breach — ${sl.compliance_pct.toFixed(1)}% vs ${sl.target_pct}% target. Review air quality events.`);
    }

    return actions;
  }

  function _buildTechActions(state) {
    const actions = [];
    if (state.filter_status?.replacement_due) {
      actions.push('Replace filter — life threshold exceeded');
    }
    if (state.filter_change_pending_reset) {
      actions.push('Press filter reset button behind front panel to complete filter change record');
    }
    if (state.filter_status?.no_anchor_on_record) {
      actions.push('No confirmed filter change on record — log via filter change form below');
    }
    return actions;
  }

  // ---------------------------------------------------------------------------
  // Utilities
  // ---------------------------------------------------------------------------

  function _pm25Color(val) {
    if (val == null) return 'var(--text-primary)';
    if (val < 5)  return 'var(--c-baseline-hi)';
    if (val < 12) return 'var(--c-baseline-lo)';
    if (val < 35) return 'var(--c-event)';
    if (val < 55) return 'var(--c-degraded)';
    return 'var(--c-critical)';
  }

  function _setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  }

  function _esc(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  return { init };

})();

// Boot after DOM ready
document.addEventListener('DOMContentLoaded', () => {
  AirTwinUI.init();
});