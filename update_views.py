"""
update_views.py

1. Executive view — remove filter life, keep cost/service/status only
2. Technician view — add filter life %, weeks to replacement, cost
3. Engineer view — add asset life, monthly cost with basis
4. scene.js — raycaster tooltip on asset hover

Run from project root:
    python update_views.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent
ERRORS = []

def ok(msg):   print(f"  \033[92m✓\033[0m {msg}")
def skip(msg): print(f"  \033[93m–\033[0m {msg} (already applied)")
def fail(msg):
    print(f"  \033[91m✗\033[0m {msg}")
    ERRORS.append(msg)


# ---------------------------------------------------------------------------
# 1. frontend/index.html — restructure executive, technician, engineer HTML
# ---------------------------------------------------------------------------

print("\n[1/3] frontend/index.html — role view HTML updates")

html_path = ROOT / "frontend" / "index.html"
src = html_path.read_text(encoding="utf-8")

# --- Executive view — remove filter and device life sections ---
old_exec_filter = '''        <div class="hud-section">
          <div class="section-title">FILTER</div>
          <div class="exec-row">
            <span id="exec-filter-type">—</span>
          </div>
          <div class="filter-bar-wrap">
            <div class="filter-bar-track">
              <div class="filter-bar-fill" id="exec-filter-bar"></div>
            </div>
            <span class="filter-pct" id="exec-filter-pct">—%</span>
          </div>
          <div class="exec-row-small" id="exec-filter-weeks">—</div>
          <div class="exec-row-small" id="exec-filter-cost">—</div>
        </div>

        <div class="hud-section">
          <div class="section-title">ASSET LIFE</div>
          <div class="filter-bar-wrap">
            <div class="filter-bar-track">
              <div class="filter-bar-fill" id="exec-device-bar" style="background:var(--c-initialising)"></div>
            </div>
            <span class="filter-pct" id="exec-device-pct">—%</span>
          </div>
          <div class="exec-row-small" id="exec-device-years">—</div>
        </div>

        <div class="hud-section">'''

new_exec_filter = '''        <div class="hud-section">'''

if "exec-filter-bar" in src and old_exec_filter in src:
    src = src.replace(old_exec_filter, new_exec_filter)
    ok("Executive view — filter and device life sections removed")
elif "exec-filter-bar" not in src:
    skip("Executive filter section already removed")
else:
    fail("Could not find executive filter section in index.html")

# --- Technician view — add filter life and asset life ---
old_tech_filter = '''        <div class="hud-section">
          <div class="section-title">DEVICE STATUS</div>
          <div class="device-grid">
            <div class="bl-row"><span>Commissioned</span><span id="tech-commissioned">—</span></div>
            <div class="bl-row"><span>Filter type</span><span id="tech-filter-type">—</span></div>
            <div class="bl-row"><span>Filter age</span><span id="tech-filter-age">—</span></div>
            <div class="bl-row"><span>Device age</span><span id="tech-device-age">—</span></div>
            <div class="bl-row"><span>Pending reset</span><span id="tech-pending-reset">—</span></div>
          </div>
        </div>'''

new_tech_filter = '''        <div class="hud-section">
          <div class="section-title">FILTER STATUS</div>
          <div class="filter-bar-wrap">
            <div class="filter-bar-track">
              <div class="filter-bar-fill" id="tech-filter-bar"></div>
            </div>
            <span class="filter-pct" id="tech-filter-pct">—%</span>
          </div>
          <div class="exec-row-small" id="tech-filter-weeks">—</div>
          <div class="exec-row-small" id="tech-filter-cost">—</div>
        </div>

        <div class="hud-section">
          <div class="section-title">DEVICE STATUS</div>
          <div class="device-grid">
            <div class="bl-row"><span>Commissioned</span><span id="tech-commissioned">—</span></div>
            <div class="bl-row"><span>Filter type</span><span id="tech-filter-type">—</span></div>
            <div class="bl-row"><span>Filter age</span><span id="tech-filter-age">—</span></div>
            <div class="bl-row"><span>Device age</span><span id="tech-device-age">—</span></div>
            <div class="bl-row"><span>Pending reset</span><span id="tech-pending-reset">—</span></div>
          </div>
        </div>'''

if "tech-filter-bar" in src:
    skip("Technician filter section already added")
elif old_tech_filter in src:
    src = src.replace(old_tech_filter, new_tech_filter)
    ok("Technician view — filter life section added")
else:
    fail("Could not find technician device status section")

# --- Engineer view — add asset life and cost sections ---
old_eng_perf = '''        <div class="hud-section">
          <div class="section-title">PERFORMANCE</div>
          <div class="perf-grid">
            <div class="bl-row"><span>Room REF</span><span id="eng-ref">—</span></div>
            <div class="bl-row"><span>Filter age</span><span id="eng-filter-age">—</span></div>
            <div class="bl-row"><span>Filter life</span><span id="eng-filter-life">—%</span></div>
          </div>
        </div>'''

new_eng_perf = '''        <div class="hud-section">
          <div class="section-title">PERFORMANCE</div>
          <div class="perf-grid">
            <div class="bl-row"><span>Room REF</span><span id="eng-ref">—</span></div>
            <div class="bl-row"><span>Filter age</span><span id="eng-filter-age">—</span></div>
            <div class="bl-row"><span>Filter life</span><span id="eng-filter-life">—%</span></div>
            <div class="bl-row"><span>Asset status</span><span id="eng-asset-status">—</span></div>
            <div class="bl-row"><span>Service level</span><span id="eng-service-level">—</span></div>
          </div>
        </div>

        <div class="hud-section">
          <div class="section-title">ASSET LIFE</div>
          <div class="perf-grid">
            <div class="bl-row"><span>Life remaining</span><span id="eng-device-life-pct">—%</span></div>
            <div class="bl-row"><span>Est. years left</span><span id="eng-device-years">—</span></div>
          </div>
        </div>

        <div class="hud-section">
          <div class="section-title">OPERATING COSTS</div>
          <div class="perf-grid">
            <div class="bl-row"><span>Energy/month</span><span id="eng-cost-energy">—</span></div>
            <div class="bl-row"><span>Filter/month</span><span id="eng-cost-filter">—</span></div>
            <div class="bl-row"><span>Total/month</span><span id="eng-cost-total">—</span></div>
            <div class="bl-row"><span>kWh/month</span><span id="eng-cost-kwh">—</span></div>
          </div>
        </div>'''

if "eng-device-life-pct" in src:
    skip("Engineer asset life and cost sections already added")
elif old_eng_perf in src:
    src = src.replace(old_eng_perf, new_eng_perf)
    ok("Engineer view — asset life and cost sections added")
else:
    fail("Could not find engineer performance section")

# --- Add tooltip overlay div before closing body ---
if 'id="asset-tooltip"' in src:
    skip("Asset tooltip div already present")
else:
    src = src.replace(
        "  <!-- ── Scripts",
        '''  <!-- ── Asset tooltip ─────────────────────────────────── -->
  <div id="asset-tooltip" class="asset-tooltip hidden">
    <div class="tooltip-title" id="tooltip-title">—</div>
    <div class="tooltip-row" id="tooltip-status">—</div>
    <div class="tooltip-row" id="tooltip-pm25">—</div>
    <div class="tooltip-row" id="tooltip-fan">—</div>
  </div>

  <!-- ── Scripts'''
    )
    ok("Asset tooltip div added")

html_path.write_text(src, encoding="utf-8")
ok("index.html written")


# ---------------------------------------------------------------------------
# 2. style.css — tooltip styles
# ---------------------------------------------------------------------------

print("\n[2/3] frontend/css/style.css — tooltip styles")

css_path = ROOT / "frontend" / "css" / "style.css"
src = css_path.read_text(encoding="utf-8")

tooltip_styles = '''
/* ── Asset hover tooltip ──────────────────────────────────── */
.asset-tooltip {
  position: fixed;
  background: var(--bg-panel);
  border: 1px solid var(--border-hi);
  border-radius: 4px;
  padding: 10px 12px;
  pointer-events: none;
  z-index: 200;
  min-width: 160px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.4);
  transition: opacity 0.15s;
}
.asset-tooltip.hidden { opacity: 0; }

.tooltip-title {
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 700;
  color: var(--text-primary);
  letter-spacing: 0.08em;
  margin-bottom: 6px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--border);
}

.tooltip-row {
  font-size: 11px;
  color: var(--text-secondary);
  padding: 2px 0;
  font-family: var(--font-mono);
}
'''

if "asset-tooltip" in src:
    skip("Tooltip styles already present")
else:
    src += tooltip_styles
    ok("Tooltip styles added")

css_path.write_text(src, encoding="utf-8")
ok("style.css written")


# ---------------------------------------------------------------------------
# 3. ui.js — technician filter life + engineer asset life/cost + exec cleanup
# ---------------------------------------------------------------------------

print("\n[3/3] frontend/js/ui.js + scene.js — view logic and tooltip")

ui_path = ROOT / "frontend" / "js" / "ui.js"
src = ui_path.read_text(encoding="utf-8")

# --- Update executive view to remove filter/device refs ---
if "exec-filter-bar" in src:
    # Remove filter and device bar update code from _updateExecutive
    old_filter_block = '''      // Filter
      const ft = ah.filter;
      if (ft) {
        _setText('exec-filter-type', ft.type || '—');
        const barEl = document.getElementById('exec-filter-bar');
        const pctEl = document.getElementById('exec-filter-pct');
        if (barEl) {
          const pct = ft.life_remaining_pct || 0;
          barEl.style.width = `${pct}%`;
          barEl.style.background = pct < 15 ? 'var(--c-degraded)' :
                                    pct < 30 ? 'var(--c-event)' :
                                    'var(--c-baseline-hi)';
        }
        if (pctEl) pctEl.textContent = `${ft.life_remaining_pct || 0}%`;
        _setText('exec-filter-weeks',
          ft.weeks_to_replacement != null ?
          `Est. replacement in ${ft.weeks_to_replacement} weeks` : '—');
        _setText('exec-filter-cost',
          ft.replacement_cost_low != null ?
          `Replacement cost: $${ft.replacement_cost_low}–${ft.replacement_cost_high}` : '—');
      }

      // Device life
      const dev = ah.device;
      if (dev) {
        const devBar = document.getElementById('exec-device-bar');
        const devPct = document.getElementById('exec-device-pct');
        if (devBar) devBar.style.width = `${dev.life_remaining_pct || 0}%`;
        if (devPct) devPct.textContent = `${dev.life_remaining_pct || 0}%`;
        _setText('exec-device-years',
          dev.years_remaining != null ?
          `Est. ${dev.years_remaining.toFixed(1)} years remaining` : '—');
      }

      // Costs'''

    new_filter_block = '''      // Costs'''

    if old_filter_block in src:
        src = src.replace(old_filter_block, new_filter_block)
        ok("Executive view — filter/device refs removed from ui.js")
    else:
        skip("Executive filter block already cleaned")
else:
    skip("exec-filter-bar not in ui.js")

# --- Add technician filter life update ---
old_tech_fn = '''  function _updateTechnician(state) {
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
  }'''

new_tech_fn = '''  function _updateTechnician(state) {
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

    // Filter life bar — from asset_health if available
    const ah = state.asset_health;
    if (ah && ah.filter) {
      const ft = ah.filter;
      const barEl = document.getElementById('tech-filter-bar');
      const pctEl = document.getElementById('tech-filter-pct');
      if (barEl) {
        const pct = ft.life_remaining_pct || 0;
        barEl.style.width = `${pct}%`;
        barEl.style.background = pct < 15 ? 'var(--c-degraded)' :
                                  pct < 30 ? 'var(--c-event)' :
                                  'var(--c-baseline-hi)';
      }
      if (pctEl) pctEl.textContent = `${ft.life_remaining_pct || 0}%`;
      _setText('tech-filter-weeks',
        ft.weeks_to_replacement != null ?
        `Est. replacement in ${ft.weeks_to_replacement} weeks` : '—');
      _setText('tech-filter-cost',
        ft.replacement_cost_low != null ?
        `Replacement cost: $${ft.replacement_cost_low}–${ft.replacement_cost_high}` : '—');
    } else {
      // Fallback from raw filter_status
      const fs = state.filter_status;
      if (fs) {
        const barEl = document.getElementById('tech-filter-bar');
        const pctEl = document.getElementById('tech-filter-pct');
        if (barEl && fs.life_percent != null) {
          barEl.style.width = `${fs.life_percent}%`;
          barEl.style.background = fs.life_percent < 15 ? 'var(--c-degraded)' :
                                    fs.life_percent < 30 ? 'var(--c-event)' :
                                    'var(--c-baseline-hi)';
        }
        if (pctEl) pctEl.textContent = `${(fs.life_percent || 0).toFixed(0)}%`;
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
  }'''

if "tech-filter-bar" in src:
    skip("Technician filter life already in ui.js")
elif old_tech_fn in src:
    src = src.replace(old_tech_fn, new_tech_fn)
    ok("Technician filter life added to ui.js")
else:
    fail("Could not find _updateTechnician() in ui.js")

# --- Add engineer asset life and cost ---
old_eng_end = '''    _setText('eng-ref', state.room_efficiency_factor != null ?
      state.room_efficiency_factor.toFixed(3) : '—');

    const fs = state.filter_status;
    if (fs) {
      _setText('eng-filter-age', fs.age_hours != null ?
        `${fs.age_hours.toFixed(0)}h (${fs.source?.replace(/_/g, ' ')})` : '—');
      _setText('eng-filter-life', fs.life_percent != null ?
        `${fs.life_percent.toFixed(0)}%` : '—%');
    }
  }'''

new_eng_end = '''    _setText('eng-ref', state.room_efficiency_factor != null ?
      state.room_efficiency_factor.toFixed(3) : '—');

    const fs = state.filter_status;
    if (fs) {
      _setText('eng-filter-age', fs.age_hours != null ?
        `${fs.age_hours.toFixed(0)}h (${fs.source?.replace(/_/g, ' ')})` : '—');
      _setText('eng-filter-life', fs.life_percent != null ?
        `${fs.life_percent.toFixed(0)}%` : '—%');
    }

    // Asset status and service level
    _setText('eng-asset-status', state.asset_status ?
      state.asset_status.replace(/_/g, ' ') : '—');
    _setText('eng-service-level', state.service_level_compliance_pct != null ?
      `${state.service_level_compliance_pct.toFixed(1)}% (30-day)` : '—');

    // Asset life and costs from asset_health
    const ah = state.asset_health;
    if (ah) {
      if (ah.device) {
        _setText('eng-device-life-pct', `${ah.device.life_remaining_pct || 0}%`);
        _setText('eng-device-years', ah.device.years_remaining != null ?
          `${ah.device.years_remaining.toFixed(1)} years` : '—');
      }
      if (ah.costs) {
        _setText('eng-cost-energy',  `$${(ah.costs.energy_monthly_usd || 0).toFixed(2)}`);
        _setText('eng-cost-filter',  `$${(ah.costs.filter_monthly_usd || 0).toFixed(2)}`);
        _setText('eng-cost-total',   `$${(ah.costs.total_monthly_usd  || 0).toFixed(2)}`);
        _setText('eng-cost-kwh',     `${(state.monthly_energy_kwh || 0).toFixed(2)} kWh`);
      }
    }
  }'''

if "eng-device-life-pct" in src:
    skip("Engineer asset life already in ui.js")
elif old_eng_end in src:
    src = src.replace(old_eng_end, new_eng_end)
    ok("Engineer asset life and cost added to ui.js")
else:
    fail("Could not find engineer section end in ui.js")

ui_path.write_text(src, encoding="utf-8")
ok("ui.js written")

# --- scene.js — add raycaster tooltip ---
scene_path = ROOT / "frontend" / "js" / "scene.js"
scene_src = scene_path.read_text(encoding="utf-8")

if "_raycaster" in scene_src or "asset-tooltip" in scene_src:
    skip("Raycaster tooltip already in scene.js")
else:
    tooltip_code = '''
  // ── Asset hover tooltip ─────────────────────────────────────

  let _raycaster = null;
  let _mouse = new THREE.Vector2();
  let _hoveredAsset = null;
  let _tooltipAssets = []; // meshes with asset metadata

  function _initTooltip(canvas) {
    _raycaster = new THREE.Raycaster();

    canvas.addEventListener('mousemove', (e) => {
      const rect = canvas.getBoundingClientRect();
      _mouse.x = ((e.clientX - rect.left) / rect.width)  * 2 - 1;
      _mouse.y = -((e.clientY - rect.top)  / rect.height) * 2 + 1;
      _updateTooltip(e.clientX, e.clientY);
    });

    canvas.addEventListener('mouseleave', () => {
      _hideTooltip();
    });
  }

  function _registerAssetMesh(mesh, assetId, assetType) {
    mesh.traverse(child => {
      if (child.isMesh) {
        child.userData.assetId = assetId;
        child.userData.assetType = assetType;
        _tooltipAssets.push(child);
      }
    });
  }

  function _updateTooltip(mouseX, mouseY) {
    if (!_raycaster || !_camera || _tooltipAssets.length === 0) return;

    _raycaster.setFromCamera(_mouse, _camera);
    const intersects = _raycaster.intersectObjects(_tooltipAssets, false);

    const tooltip = document.getElementById('asset-tooltip');
    if (!tooltip) return;

    if (intersects.length > 0) {
      const hit = intersects[0].object;
      const assetId = hit.userData.assetId;
      const assetType = hit.userData.assetType;

      if (assetId !== _hoveredAsset) {
        _hoveredAsset = assetId;
        const state = AirTwinState.get();

        // Build tooltip content
        const titleEl = document.getElementById('tooltip-title');
        const statusEl = document.getElementById('tooltip-status');
        const pm25El = document.getElementById('tooltip-pm25');
        const fanEl = document.getElementById('tooltip-fan');

        if (titleEl) {
          const label = assetType === 'sensor' ? 'SDS011 · ' : 'STARKVIND · ';
          titleEl.textContent = label + assetId;
        }

        if (statusEl) {
          const status = state.asset_status || 'unknown';
          statusEl.textContent = 'Status: ' + status.replace(/_/g, ' ');
        }

        if (pm25El) {
          pm25El.textContent = state.pm25 != null ?
            `PM2.5: ${state.pm25.toFixed(1)} µg/m³` : 'PM2.5: —';
        }

        if (fanEl) {
          if (assetType === 'purifier') {
            const mode = state.fan_mode || '—';
            const speed = state.fan_speed || '—';
            fanEl.textContent = `Fan: ${mode} · Step ${speed}`;
          } else {
            fanEl.textContent = `Sensor · ${state.regime?.toUpperCase() || '—'}`;
          }
        }
      }

      // Position tooltip near cursor
      tooltip.classList.remove('hidden');
      tooltip.style.left = `${mouseX + 16}px`;
      tooltip.style.top  = `${mouseY - 8}px`;

      // Keep tooltip on screen
      const rect = tooltip.getBoundingClientRect();
      if (rect.right > window.innerWidth - 8) {
        tooltip.style.left = `${mouseX - rect.width - 16}px`;
      }
      if (rect.bottom > window.innerHeight - 8) {
        tooltip.style.top = `${mouseY - rect.height - 8}px`;
      }

    } else {
      _hideTooltip();
    }
  }

  function _hideTooltip() {
    _hoveredAsset = null;
    const tooltip = document.getElementById('asset-tooltip');
    if (tooltip) tooltip.classList.add('hidden');
  }

'''

    # Insert tooltip code before the GLB loader section
    scene_src = scene_src.replace(
        "\n  // ── GLB loader",
        tooltip_code + "\n  // ── GLB loader"
    )
    ok("Raycaster tooltip code added to scene.js")

    # Register purifier mesh after loading
    old_purifier_add = "      _scene.add(_purifierMesh);\n    });"
    new_purifier_add = """      _scene.add(_purifierMesh);
      _registerAssetMesh(_purifierMesh, 'starkvind_01', 'purifier');
    });"""
    if old_purifier_add in scene_src:
        scene_src = scene_src.replace(old_purifier_add, new_purifier_add)
        ok("Purifier mesh registered for tooltip")
    else:
        fail("Could not find purifier mesh add in scene.js")

    # Register sensor mesh after loading
    old_sensor_add = "      _scene.add(_sensorMesh);\n    });"
    new_sensor_add = """      _scene.add(_sensorMesh);
      _registerAssetMesh(_sensorMesh, 'sds011_01', 'sensor');
    });"""
    if old_sensor_add in scene_src:
        scene_src = scene_src.replace(old_sensor_add, new_sensor_add)
        ok("Sensor mesh registered for tooltip")
    else:
        fail("Could not find sensor mesh add in scene.js")

    # Init tooltip in init() after orbit controls
    old_orbit_init = "    _initOrbit(canvas);"
    new_orbit_init = "    _initOrbit(canvas);\n    _initTooltip(canvas);"
    if "_initTooltip" in scene_src:
        skip("_initTooltip already called in init()")
    elif old_orbit_init in scene_src:
        scene_src = scene_src.replace(old_orbit_init, new_orbit_init)
        ok("_initTooltip() called in init()")
    else:
        fail("Could not find _initOrbit call in scene.js")

    scene_path.write_text(scene_src, encoding="utf-8")
    ok("scene.js written")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

print("\n" + "=" * 50)
if ERRORS:
    print(f"COMPLETED WITH {len(ERRORS)} ERROR(S):")
    for e in ERRORS: print(f"  ✗ {e}")
    sys.exit(1)
else:
    print("ALL CHANGES APPLIED SUCCESSFULLY")
    print("\nRefresh http://localhost:5500 to see changes")
    print("Hover over purifier or sensor in 3D view for tooltip")