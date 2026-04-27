/**
 * ui.js — HUD panels, role tabs, and all DOM updates.
 *
 * Subscribes to AirTwinState and updates every panel reactively.
 * No direct state writes — reads only.
 */

const AirTwinUI = (() => {

  function init() {
    _initRoleTabs();
    AirTwinState.on('update', _onStateUpdate);
    AirTwinState.on('regime-change', _onRegimeChange);
    _initMaintenanceForm();
  }

  // ── Role tab switching ──────────────────────────────────────

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

  // ── Main state update handler ───────────────────────────────

  function _onStateUpdate(state) {
    _updateHeader(state);
    _updateRegimeBadge(state);
    _updateOperator(state);
    _updateExecutive(state);
    _updateEngineer(state);
    _updateTechnician(state);
  }

  function _onRegimeChange({ from, to }) {
    // Brief flash on regime badge
    const badge = document.getElementById('regime-badge');
    if (badge) {
      badge.style.transform = 'scale(1.08)';
      setTimeout(() => { badge.style.transform = ''; }, 300);
    }
  }

  // ── Header ──────────────────────────────────────────────────

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

  // ── Regime badge ────────────────────────────────────────────

  function _updateRegimeBadge(state) {
    const badge = document.getElementById('regime-badge');
    if (!badge) return;
    const cls = AirTwinState.regimeClass(state.regime);
    badge.className = `regime-badge ${cls}`;
    badge.textContent = AirTwinState.regimeLabel(state.regime);
  }

  // ── Operator view ───────────────────────────────────────────

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
        const mode = state.fan_mode === 'auto' ? 'A' : 'M';
        fanEl.textContent = `${state.fan_speed}${mode}`;
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
      const color = AirTwinState.regimeColor(state.regime, state.confidence);
      concEl.style.borderLeftColor = color;
    }

    // Filter
    const fs = state.filter_status;
    if (fs) {
      const barEl = document.getElementById('op-filter-bar');
      const pctEl = document.getElementById('op-filter-pct');
      const metaEl = document.getElementById('op-filter-meta');

      if (barEl && fs.life_percent != null) {
        const pct = Math.min(100, fs.life_percent);
        barEl.style.width = `${pct}%`;
        barEl.style.background = pct > 85 ? 'var(--c-degraded)' :
                                  pct > 65 ? 'var(--c-event)' :
                                  'var(--c-baseline-hi)';
      }
      if (pctEl) {
        pctEl.textContent = fs.life_percent != null ? `${fs.life_percent.toFixed(0)}%` : '—%';
      }
      if (metaEl) {
        const parts = [];
        if (fs.installed_type) parts.push(fs.installed_type.replace('_', ' '));
        if (fs.source === 'device_counter_no_anchor') parts.push('no anchor');
        if (fs.pending_reset) parts.push('reset pending');
        metaEl.textContent = parts.join(' · ') || '—';
      }
    }

    // Alerts — for now show regime-based alerts
    const alertsEl = document.getElementById('op-alerts');
    if (alertsEl) {
      const alerts = _buildAlerts(state);
      if (alerts.length === 0) {
        alertsEl.innerHTML = '<div class="alert-empty">No active alerts</div>';
      } else {
        alertsEl.innerHTML = alerts.map(a =>
          `<div class="alert-item">${_esc(a)}</div>`
        ).join('');
      }
    }
  }

  // ── Executive view ──────────────────────────────────────────

  function _updateExecutive(state) {
    const concEl = document.getElementById('exec-conclusion');
    if (concEl) {
      concEl.textContent = state.confidence_conclusion || '—';
    }

    const actionsEl = document.getElementById('exec-actions');
    if (actionsEl) {
      const actions = _buildActions(state);
      if (actions.length === 0) {
        actionsEl.innerHTML = '<div class="action-empty">No actions required</div>';
      } else {
        actionsEl.innerHTML = actions.map(a =>
          `<div class="action-item">${_esc(a)}</div>`
        ).join('');
      }
    }

    _setText('exec-confidence', state.confidence != null ?
      `${(state.confidence * 100).toFixed(0)}%` : '—');
    _setText('exec-regime', AirTwinState.regimeLabel(state.regime));
    _setText('exec-baseline', state.baseline_locked != null ?
      `${state.baseline_locked.toFixed(2)} µg/m³` : 'Not locked');
  }

  // ── Engineer view ───────────────────────────────────────────

  function _updateEngineer(state) {
    // Confidence arc
    const arcEl = document.getElementById('eng-arc');
    const valEl = document.getElementById('eng-confidence-val');
    if (arcEl && state.confidence != null) {
      const pct = Math.max(0, Math.min(1, state.confidence));
      const arcLen = 126; // total arc length (approx)
      const filled = pct * arcLen;
      arcEl.style.strokeDasharray = `${filled} ${arcLen - filled + 1}`;
      const color = AirTwinState.regimeColor(state.regime, state.confidence);
      arcEl.style.stroke = color;
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

      if (factors.length === 0) {
        factorsEl.innerHTML = '<div style="color:var(--text-dim);font-size:11px">No factors yet</div>';
      } else {
        factorsEl.innerHTML = factors.map(f => {
          const sign = f.delta >= 0 ? '+' : '';
          const cls = f.delta >= 0 ? 'pos' : 'neg';
          const label = f.key.replace(/_/g, ' ');
          return `<div class="factor-row" title="${_esc(f.reason)}">
            <span class="factor-name">${_esc(label)}</span>
            <span class="factor-delta ${cls}">${sign}${f.delta.toFixed(3)}</span>
          </div>`;
        }).join('');
      }
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

    const fs = state.filter_status;
    if (fs) {
      _setText('eng-filter-age', fs.age_hours != null ?
        `${fs.age_hours.toFixed(0)}h (${fs.source?.replace(/_/g, ' ')})` : '—');
      _setText('eng-filter-life', fs.life_percent != null ?
        `${fs.life_percent.toFixed(0)}%` : '—%');
    }
  }

  // ── Technician view ─────────────────────────────────────────

  function _updateTechnician(state) {
    const actions = _buildTechActions(state);
    const actionsEl = document.getElementById('tech-actions');
    if (actionsEl) {
      if (actions.length === 0) {
        actionsEl.innerHTML = '<div class="action-empty">No pending actions</div>';
      } else {
        actionsEl.innerHTML = actions.map(a =>
          `<div class="action-item">${_esc(a)}</div>`
        ).join('');
      }
    }

    _setText('tech-commissioned', state.commissioned_at ?
      new Date(state.commissioned_at).toLocaleDateString() : '—');
    _setText('tech-filter-type',
      (state.installed_filter_type || '—').replace(/_/g, ' '));

    const fs = state.filter_status;
    if (fs) {
      _setText('tech-filter-age', fs.age_hours != null ?
        `${fs.age_hours.toFixed(0)}h` : '—');
    }

    // Device age from raw state
    if (state.last_known_filter_age != null) {
      const hours = (state.last_known_filter_age / 60).toFixed(0);
      _setText('tech-device-age', `${hours}h`);
    } else {
      _setText('tech-device-age', '—');
    }

    _setText('tech-pending-reset',
      state.filter_change_pending_reset ? '⚠ YES' : 'No');

    const pendingEl = document.getElementById('tech-pending-reset');
    if (pendingEl) {
      pendingEl.style.color = state.filter_change_pending_reset ?
        'var(--c-event)' : 'var(--text-secondary)';
    }
  }

  // ── Maintenance form ────────────────────────────────────────

  function _initMaintenanceForm() {
    const btn = document.getElementById('tech-log-change');
    if (!btn) return;

    btn.addEventListener('click', async () => {
      const filterType = document.getElementById('tech-filter-select')?.value || 'particle_only';
      const resultEl = document.getElementById('tech-maint-result');

      btn.disabled = true;
      btn.textContent = 'LOGGING...';

      try {
        const resp = await fetch('http://localhost:8000/maintenance', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            device_age_minutes: 0, // will be populated from current state
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

  // ── Derived data helpers ────────────────────────────────────

  function _buildAlerts(state) {
    const alerts = [];
    if (state.regime === 'degraded') {
      alerts.push('Air quality degraded — investigate source');
    }
    if (state.regime === 'event') {
      alerts.push('Temporary air quality event in progress');
    }
    if (state.filter_status?.replacement_due) {
      alerts.push('Filter replacement due');
    }
    if (state.filter_status?.pending_reset) {
      alerts.push('Filter reset button not pressed after change');
    }
    if (state.confidence < 0.3) {
      alerts.push('Twin confidence critically low — engineer review required');
    }
    return alerts;
  }

  function _buildActions(state) {
    const actions = [];
    if (state.regime === 'degraded') {
      actions.push('Air quality degraded — investigate source and increase ventilation');
    }
    if (state.regime === 'event') {
      actions.push('Temporary event in progress — purifier responding. Monitor for resolution.');
    }
    if (state.regime === 'initialising' || state.regime === 'validating') {
      actions.push('System establishing baseline — no action required');
    }
    if (state.regime === 'unknown') {
      actions.push('Sensor data unavailable — check Pi and sensor connections');
    }
    if (state.confidence < 0.3) {
      actions.push('Twin confidence critically low — engineer review required before relying on assessment');
    } else if (state.confidence < 0.5) {
      actions.push('Twin confidence low — engineer review recommended');
    }
    if (state.filter_status?.replacement_due) {
      actions.push('Replace filter — manufacturer service interval exceeded');
    }
    if (state.filter_change_pending_reset) {
      actions.push('Press filter reset button behind front panel');
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
    if (state.filter_status?.no_anchor) {
      actions.push('No confirmed filter change on record — log via filter change form');
    }
    return actions;
  }

  function _pm25Color(val) {
    if (val == null) return 'var(--text-primary)';
    if (val < 5)   return 'var(--c-baseline-hi)';
    if (val < 12)  return 'var(--c-baseline-lo)';
    if (val < 35)  return 'var(--c-event)';
    if (val < 55)  return 'var(--c-degraded)';
    return 'var(--c-critical)';
  }

  // ── Utilities ───────────────────────────────────────────────

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