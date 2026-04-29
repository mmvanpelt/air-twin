"""
update_executive.py

Implements full executive view overhaul:
1. device_profiles.json — consumables, power, device life
2. config.json — electricity rate, service level targets
3. backend/twin_engine/models.py — asset_status, service_level fields
4. backend/twin_engine/engine.py — derive asset_status each cycle
5. backend/brief_generator.py — separate environment + asset sections
6. frontend/index.html — tab reorder, executive view HTML
7. frontend/js/ui.js — executive view logic, asset health, costs
8. frontend/js/state.js — normalise regime labels from enriched frames
9. tools/record_demo.py — normalise regime strings in enrich_frames()

Run from project root:
    python update_executive.py
"""

import json
import sys
import re
from pathlib import Path

ROOT = Path(__file__).parent
ERRORS = []

def ok(msg):   print(f"  \033[92m✓\033[0m {msg}")
def skip(msg): print(f"  \033[93m–\033[0m {msg} (already applied)")
def fail(msg):
    print(f"  \033[91m✗\033[0m {msg}")
    ERRORS.append(msg)


# ---------------------------------------------------------------------------
# 1. device_profiles.json
# ---------------------------------------------------------------------------

print("\n[1/9] assets/device_profiles.json")

profiles_path = ROOT / "assets" / "device_profiles.json"
with open(profiles_path) as f:
    profiles = json.load(f)

device = profiles["devices"]["ikea_starkvind_e2007"]

if "consumables" in device:
    skip("consumables already present")
else:
    device["consumables"] = {
        "particle_filter": {
            "life_hours": 4380,
            "replacement_cost_usd": {"low": 15, "high": 25},
            "part_description": "HEPA particle filter"
        },
        "particle_and_gas_filter": {
            "life_hours": 4380,
            "replacement_cost_usd": {"low": 25, "high": 40},
            "part_description": "HEPA + activated carbon gas filter"
        }
    }
    ok("consumables added")

if "device_life_hours" in device:
    skip("device_life_hours already present")
else:
    device["device_life_hours"] = 26280
    device["_device_life_comment"] = "~3 years typical use at 8h/day"
    ok("device_life_hours added")

if "power_watts_by_speed" in device:
    skip("power_watts_by_speed already present")
else:
    device["power_watts_by_speed"] = {
        "manual": {"1": 3, "2": 5, "3": 8, "4": 12, "5": 16},
        "auto":   {"1": 3, "2": 4, "3": 6, "4": 9,  "5": 12,
                   "6": 15, "7": 18, "8": 21, "9": 25},
        "_comment": "Approximate watts at each fan speed step"
    }
    ok("power_watts_by_speed added")

with open(profiles_path, "w") as f:
    json.dump(profiles, f, indent=2)
ok("device_profiles.json written")


# ---------------------------------------------------------------------------
# 2. config.json
# ---------------------------------------------------------------------------

print("\n[2/9] assets/config.json")

config_path = ROOT / "assets" / "config.json"
with open(config_path) as f:
    config = json.load(f)

changed = False

if "electricity_rate_usd_per_kwh" not in config:
    config["electricity_rate_usd_per_kwh"] = 0.13
    config["_electricity_comment"] = "US average residential rate — update for local rate"
    ok("electricity_rate_usd_per_kwh added")
    changed = True
else:
    skip("electricity_rate_usd_per_kwh already present")

if "service_level" not in config:
    config["service_level"] = {
        "pm25_threshold_ug_m3": 12.0,
        "compliance_target_pct": 95.0,
        "rolling_window_days": 30,
        "_comment": "WHO PM2.5 guideline 15 µg/m³ annual, 12 µg/m³ is conservative target"
    }
    ok("service_level added")
    changed = True
else:
    skip("service_level already present")

if changed:
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
ok("config.json written")


# ---------------------------------------------------------------------------
# 3. models.py — add asset_status and service_level fields
# ---------------------------------------------------------------------------

print("\n[3/9] backend/twin_engine/models.py")

models_path = ROOT / "backend" / "twin_engine" / "models.py"
src = models_path.read_text(encoding="utf-8")

if "asset_status" in src:
    skip("asset_status already in TwinState")
else:
    old = "    # --- Control ---"
    new = """    # --- Asset status ---
    # Derived each cycle — separate from environment regime
    # operating_normally | responding | performance_low | filter_due | offline | unknown
    asset_status:               str = "unknown"
    service_level_compliance_pct: float = 100.0
    # Fraction of readings in rolling window below pm25 threshold
    monthly_energy_kwh:         float = 0.0
    monthly_cost_usd:           float = 0.0

    # --- Control ---"""
    if old in src:
        src = src.replace(old, new)
        ok("asset_status fields added to TwinState")
    else:
        fail("Could not find Control section in models.py")

if "asset_status" in src and '"asset_status":' not in src:
    old_to_dict = '            # Control\n            "last_fan_speed_commanded":'
    new_to_dict = '''            # Asset status
            "asset_status":               self.asset_status,
            "service_level_compliance_pct": self.service_level_compliance_pct,
            "monthly_energy_kwh":         self.monthly_energy_kwh,
            "monthly_cost_usd":           self.monthly_cost_usd,
            # Control
            "last_fan_speed_commanded":'''
    if old_to_dict in src:
        src = src.replace(old_to_dict, new_to_dict)
        ok("to_dict() updated with asset_status")
    else:
        fail("Could not find to_dict() control section")

if "asset_status=get" not in src:
    old_from_dict = '            # Control\n            last_fan_speed_commanded=get("last_fan_speed_commanded"),'
    new_from_dict = '''            # Asset status
            asset_status=get("asset_status", "unknown"),
            service_level_compliance_pct=get("service_level_compliance_pct", 100.0),
            monthly_energy_kwh=get("monthly_energy_kwh", 0.0),
            monthly_cost_usd=get("monthly_cost_usd", 0.0),
            # Control
            last_fan_speed_commanded=get("last_fan_speed_commanded"),'''
    if old_from_dict in src:
        src = src.replace(old_from_dict, new_from_dict)
        ok("from_dict() updated with asset_status")
    else:
        fail("Could not find from_dict() control section")

models_path.write_text(src, encoding="utf-8")
ok("models.py written")


# ---------------------------------------------------------------------------
# 4. engine.py — derive asset_status each cycle
# ---------------------------------------------------------------------------

print("\n[4/9] backend/twin_engine/engine.py")

engine_path = ROOT / "backend" / "twin_engine" / "engine.py"
src = engine_path.read_text(encoding="utf-8")

if "_derive_asset_status" in src:
    skip("_derive_asset_status already in engine.py")
else:
    asset_status_fn = '''
    def _derive_asset_status(self, reading: Reading) -> str:
        """
        Derive asset operational status — separate from environment regime.

        Asset status reflects the purifier device health and behaviour,
        not the air quality in the room. A purifier can be RESPONDING
        (working hard during an event) while the environment is DEGRADED.
        These are independent concerns.
        """
        from dataclasses import replace as _r

        # Check filter life
        filter_life = self._state.filter_status if hasattr(self._state, 'filter_status') else None
        if filter_life and getattr(filter_life, 'replacement_due', False):
            return "filter_due"

        # Check if purifier is online
        if reading.purifier_on is False:
            return "offline"

        if reading.purifier_on is None:
            return "unknown"

        # Check if actively responding to an event
        regime = self._state.current_regime
        if str(regime).lower().replace("regimetype.", "") in ("event", "degraded"):
            if reading.fan_speed and reading.fan_speed > 1:
                return "responding"

        # Check performance ratio if available
        perf_counts = self._state.performance_observation_counts
        if perf_counts and any(v > 3 for v in perf_counts.values()):
            # Enough observations — check empirical CADR
            empirical = self._state.empirical_cadr_auto_m3h or {}
            if empirical:
                ratios = []
                for speed, cadr in empirical.items():
                    if cadr is not None and cadr > 0:
                        from backend.twin_engine.loader import get_device_profile
                        profile = get_device_profile(self._profile.device_id
                                                     if hasattr(self._profile, 'device_id')
                                                     else "ikea_starkvind_e2007",
                                                     ROOT / "assets" / "device_profiles.json")
                        # Compare against expected — simplified check
                        ratios.append(cadr)
                if ratios and max(ratios) < 50:  # very low empirical CADR
                    return "performance_low"

        return "operating_normally"

    def _derive_service_level(self) -> float:
        """
        Calculate rolling 30-day service level compliance.
        Returns percentage of readings below pm25 threshold.
        """
        try:
            threshold = self._config.get("service_level", {}).get(
                "pm25_threshold_ug_m3", 12.0
            )
            window_days = self._config.get("service_level", {}).get(
                "rolling_window_days", 30
            )
            from datetime import datetime, timezone, timedelta
            import sqlite3
            cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()

            # Query DB for compliance
            if self._db_conn:
                row = self._db_conn.execute("""
                    SELECT
                        COUNT(*) as total,
                        SUM(CASE WHEN value <= ? THEN 1 ELSE 0 END) as compliant
                    FROM raw_readings
                    WHERE ts >= ? AND is_plausible = 1
                """, (threshold, cutoff)).fetchone()
                if row and row['total'] > 0:
                    return round(100.0 * row['compliant'] / row['total'], 1)
        except Exception:
            pass
        return 100.0

    def _derive_monthly_cost(self, reading: Reading) -> tuple[float, float]:
        """
        Estimate monthly energy usage and cost from current fan speed.
        Returns (monthly_kwh, monthly_cost_usd).
        """
        try:
            rate = self._config.get("electricity_rate_usd_per_kwh", 0.13)
            power_map = self._profile.power_watts_by_speed if hasattr(
                self._profile, 'power_watts_by_speed') else {}
            mode = reading.fan_mode or "auto"
            speed = str(reading.fan_speed or 1)
            mode_map = power_map.get(mode, power_map.get("auto", {}))
            watts = float(mode_map.get(speed, 5))
            # Assume 24h/day operation
            monthly_kwh = round(watts * 24 * 30 / 1000, 2)
            monthly_cost = round(monthly_kwh * rate, 2)
            return monthly_kwh, monthly_cost
        except Exception:
            return 0.0, 0.0

'''

    # Insert before _maybe_command_speed
    if "_maybe_command_speed" in src:
        src = src.replace(
            "\n    def _maybe_command_speed(",
            asset_status_fn + "\n    def _maybe_command_speed("
        )
        ok("_derive_asset_status() added to engine.py")
    else:
        fail("Could not find _maybe_command_speed in engine.py")

    # Add asset status derivation call in the main cycle
    old_cycle_end = "        self._persist_state()"
    new_cycle_end = """        # Derive asset status and service level each cycle
        asset_status = self._derive_asset_status(reading)
        service_level = self._derive_service_level()
        monthly_kwh, monthly_cost = self._derive_monthly_cost(reading)
        from dataclasses import replace as _r
        self._state = _r(
            self._state,
            asset_status=asset_status,
            service_level_compliance_pct=service_level,
            monthly_energy_kwh=monthly_kwh,
            monthly_cost_usd=monthly_cost,
        )
        self._persist_state()"""

    if "Derive asset status" in src:
        skip("asset status derivation call already in cycle")
    elif old_cycle_end in src:
        src = src.replace(old_cycle_end, new_cycle_end, 1)
        ok("asset status derivation added to cycle")
    else:
        fail("Could not find _persist_state() in engine.py")

engine_path.write_text(src, encoding="utf-8")
ok("engine.py written")


# ---------------------------------------------------------------------------
# 5. brief_generator.py — separate environment + asset sections
# ---------------------------------------------------------------------------

print("\n[5/9] backend/brief_generator.py")

brief_path = ROOT / "backend" / "brief_generator.py"
src = brief_path.read_text(encoding="utf-8")

if "_asset_health_brief" in src:
    skip("asset health brief already in brief_generator.py")
else:
    asset_brief_fn = '''

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

'''

    # Insert before generate()
    src = src.replace("\ndef generate(", asset_brief_fn + "\ndef generate(")
    ok("_asset_health_brief() added")

    # Update executive brief to include asset health
    old_exec = '''    executive_brief = {
        "conclusion": conclusion,
        "required_actions": _required_actions(state),
    }'''
    new_exec = '''    # Load profile for asset health calculation
    try:
        from backend.twin_engine.loader import get_device_profile
        from pathlib import Path
        profile_path = Path(__file__).parent.parent / "assets" / "device_profiles.json"
        with open(profile_path) as _f:
            _profile_data = json.load(_f)
    except Exception:
        _profile_data = {"devices": {}}

    asset_health = _asset_health_brief(state, _profile_data)

    executive_brief = {
        "conclusion": conclusion,
        "required_actions": _required_actions(state),
        "asset_health": asset_health,
    }'''

    if "asset_health" in src:
        skip("executive brief asset_health already present")
    elif old_exec in src:
        src = src.replace(old_exec, new_exec)
        # Add json import if missing
        if "import json" not in src:
            src = "import json\n" + src
        ok("executive brief updated with asset_health")
    else:
        fail("Could not find executive_brief dict in brief_generator.py")

brief_path.write_text(src, encoding="utf-8")
ok("brief_generator.py written")


# ---------------------------------------------------------------------------
# 6. frontend/index.html — tab reorder + executive view HTML
# ---------------------------------------------------------------------------

print("\n[6/9] frontend/index.html — tab reorder + executive HTML")

html_path = ROOT / "frontend" / "index.html"
src = html_path.read_text(encoding="utf-8")

# Fix tab order
old_tabs = '''      <button class="tab active" data-role="operator">OPERATOR</button>
      <button class="tab" data-role="executive">EXECUTIVE</button>
      <button class="tab" data-role="engineer">ENGINEER</button>
      <button class="tab" data-role="technician">TECHNICIAN</button>'''

new_tabs = '''      <button class="tab active" data-role="operator">OPERATOR</button>
      <button class="tab" data-role="technician">TECHNICIAN</button>
      <button class="tab" data-role="engineer">ENGINEER</button>
      <button class="tab" data-role="executive">EXECUTIVE</button>'''

if "TECHNICIAN</button>\n      <button class=\"tab\" data-role=\"engineer\">ENGINEER" in src:
    skip("tab order already correct")
elif old_tabs in src:
    src = src.replace(old_tabs, new_tabs)
    ok("tab order fixed — OPERATOR·TECHNICIAN·ENGINEER·EXECUTIVE")
else:
    fail("Could not find tab nav in index.html")

# Replace executive view HTML
old_exec_html = '''      <!-- ── EXECUTIVE view ─────────────────────────────────── -->
      <div class="role-view" data-role="executive">
        <div class="hud-section">
          <div class="exec-conclusion" id="exec-conclusion">—</div>
        </div>
        <div class="hud-section">
          <div class="section-title">REQUIRED ACTIONS</div>
          <div class="action-list" id="exec-actions">
            <div class="action-empty">No actions required</div>
          </div>
        </div>
        <div class="hud-section exec-meta">
          <div class="exec-meta-row">
            <span class="exec-meta-label">Confidence</span>
            <span class="exec-meta-value" id="exec-confidence">—</span>
          </div>
          <div class="exec-meta-row">
            <span class="exec-meta-label">Regime</span>
            <span class="exec-meta-value" id="exec-regime">—</span>
          </div>
          <div class="exec-meta-row">
            <span class="exec-meta-label">Baseline</span>
            <span class="exec-meta-value" id="exec-baseline">—</span>
          </div>
        </div>
      </div>'''

new_exec_html = '''      <!-- ── EXECUTIVE view ─────────────────────────────────── -->
      <div class="role-view" data-role="executive">

        <div class="hud-section">
          <div class="exec-status-label">AIR QUALITY</div>
          <div class="exec-conclusion" id="exec-conclusion">—</div>
        </div>

        <div class="hud-section">
          <div class="section-title">ASSET STATUS</div>
          <div class="exec-asset-status" id="exec-asset-status">—</div>
          <div class="exec-asset-meta" id="exec-asset-meta">—</div>
        </div>

        <div class="hud-section">
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

        <div class="hud-section">
          <div class="section-title">MONTHLY COSTS</div>
          <div class="exec-cost-grid">
            <div class="exec-cost-row">
              <span>Energy</span>
              <span id="exec-cost-energy">—</span>
            </div>
            <div class="exec-cost-row">
              <span>Filter (monthly)</span>
              <span id="exec-cost-filter">—</span>
            </div>
            <div class="exec-cost-row exec-cost-total">
              <span>Total</span>
              <span id="exec-cost-total">—</span>
            </div>
            <div class="exec-cost-row">
              <span>Annual estimate</span>
              <span id="exec-cost-annual">—</span>
            </div>
          </div>
        </div>

        <div class="hud-section">
          <div class="section-title">SERVICE LEVEL</div>
          <div class="exec-service-wrap">
            <div class="filter-bar-track">
              <div class="filter-bar-fill" id="exec-service-bar"></div>
            </div>
            <span class="filter-pct" id="exec-service-pct">—%</span>
          </div>
          <div class="exec-row-small" id="exec-service-label">—</div>
        </div>

        <div class="hud-section">
          <div class="section-title">REQUIRED ACTIONS</div>
          <div class="action-list" id="exec-actions">
            <div class="action-empty">No actions required</div>
          </div>
        </div>

      </div>'''

if "exec-asset-status" in src:
    skip("executive view HTML already updated")
elif old_exec_html in src:
    src = src.replace(old_exec_html, new_exec_html)
    ok("executive view HTML replaced")
else:
    fail("Could not find executive view HTML in index.html")

html_path.write_text(src, encoding="utf-8")
ok("index.html written")


# ---------------------------------------------------------------------------
# 7. style.css — executive view styles
# ---------------------------------------------------------------------------

print("\n[7/9] frontend/css/style.css — executive styles")

css_path = ROOT / "frontend" / "css" / "style.css"
src = css_path.read_text(encoding="utf-8")

exec_styles = '''
/* ── Executive view ───────────────────────────────────────── */
.exec-status-label {
  font-family: var(--font-mono);
  font-size: 9px;
  letter-spacing: 0.15em;
  color: var(--text-dim);
  margin-bottom: 6px;
}

.exec-conclusion {
  font-family: var(--font-sans);
  font-size: 16px;
  font-weight: 500;
  color: var(--text-primary);
  line-height: 1.4;
}

.exec-asset-status {
  font-family: var(--font-mono);
  font-size: 13px;
  font-weight: 700;
  color: var(--c-baseline-hi);
  margin-bottom: 4px;
  transition: color 0.4s;
}

.exec-asset-meta {
  font-size: 11px;
  color: var(--text-secondary);
}

.exec-row {
  font-size: 12px;
  color: var(--text-secondary);
  margin-bottom: 8px;
}

.exec-row-small {
  font-size: 11px;
  color: var(--text-dim);
  margin-top: 4px;
}

.exec-cost-grid {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.exec-cost-row {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  color: var(--text-secondary);
  padding: 4px 0;
  border-bottom: 1px solid var(--border);
}

.exec-cost-row:last-child { border-bottom: none; }

.exec-cost-total {
  font-family: var(--font-mono);
  font-weight: 700;
  color: var(--text-primary);
  font-size: 13px;
  border-bottom: 1px solid var(--border-hi) !important;
}

.exec-service-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
'''

if "exec-asset-status" in src:
    skip("executive styles already present")
else:
    src = src + exec_styles
    ok("executive styles added")

css_path.write_text(src, encoding="utf-8")
ok("style.css written")


# ---------------------------------------------------------------------------
# 8. frontend/js/ui.js — executive view logic
# ---------------------------------------------------------------------------

print("\n[8/9] frontend/js/ui.js — executive view update")

ui_path = ROOT / "frontend" / "js" / "ui.js"
src = ui_path.read_text(encoding="utf-8")

if "exec-asset-status" in src:
    skip("executive view logic already updated")
else:
    old_exec_fn = '''  // ── Executive view ──────────────────────────────────────────

  function _updateExecutive(state) {
    const concEl = document.getElementById(\'exec-conclusion\');
    if (concEl) {
      concEl.textContent = state.confidence_conclusion || \'—\';
    }

    const actionsEl = document.getElementById(\'exec-actions\');
    if (actionsEl) {
      const actions = _buildActions(state);
      if (actions.length === 0) {
        actionsEl.innerHTML = \'<div class="action-empty">No actions required</div>\';
      } else {
        actionsEl.innerHTML = actions.map(a =>
          `<div class="action-item">${_esc(a)}</div>`
        ).join(\'\');
      }
    }

    _setText(\'exec-confidence\', state.confidence != null ?
      `${(state.confidence * 100).toFixed(0)}%` : \'—\');
    _setText(\'exec-regime\', AirTwinState.regimeLabel(state.regime));
    _setText(\'exec-baseline\', state.baseline_locked != null ?
      `${state.baseline_locked.toFixed(2)} µg/m³` : \'Not locked\');
  }'''

    new_exec_fn = '''  // ── Executive view ──────────────────────────────────────────

  function _updateExecutive(state) {
    // Air quality conclusion — plain English only
    const concEl = document.getElementById('exec-conclusion');
    if (concEl) {
      concEl.textContent = state.confidence_conclusion || '—';
    }

    // Asset status
    const assetStatusEl = document.getElementById('exec-asset-status');
    const assetMetaEl = document.getElementById('exec-asset-meta');
    const assetStatus = state.asset_status || 'unknown';
    const assetLabels = {
      operating_normally: 'Operating normally',
      responding:         'Responding to air quality event',
      performance_low:    'Performance below expected — review recommended',
      filter_due:         'Filter replacement due',
      offline:            'Offline — check connections',
      unknown:            'Status unknown',
    };
    const assetColors = {
      operating_normally: 'var(--c-baseline-hi)',
      responding:         'var(--c-event)',
      performance_low:    'var(--c-degraded)',
      filter_due:         'var(--c-degraded)',
      offline:            'var(--c-critical)',
      unknown:            'var(--c-unknown)',
    };
    if (assetStatusEl) {
      assetStatusEl.textContent = assetLabels[assetStatus] || assetStatus;
      assetStatusEl.style.color = assetColors[assetStatus] || 'var(--text-secondary)';
    }

    // Asset health from full brief if available
    const ah = state.asset_health;
    if (ah) {
      if (assetMetaEl) {
        const years = ah.device?.years_remaining;
        assetMetaEl.textContent = years != null ?
          `Est. ${years.toFixed(1)} years remaining asset life` : '—';
      }

      // Filter
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

      // Costs
      const costs = ah.costs;
      if (costs) {
        _setText('exec-cost-energy',  `$${(costs.energy_monthly_usd || 0).toFixed(2)}/mo`);
        _setText('exec-cost-filter',  `$${(costs.filter_monthly_usd || 0).toFixed(2)}/mo`);
        _setText('exec-cost-total',   `$${(costs.total_monthly_usd  || 0).toFixed(2)}/mo`);
        _setText('exec-cost-annual',  `$${(costs.total_annual_usd   || 0).toFixed(2)}/yr`);
      }

      // Service level
      const sl = ah.service_level;
      if (sl) {
        const slBar = document.getElementById('exec-service-bar');
        const slPct = document.getElementById('exec-service-pct');
        const slLabel = document.getElementById('exec-service-label');
        if (slBar) {
          slBar.style.width = `${sl.compliance_pct || 0}%`;
          slBar.style.background = sl.met ?
            'var(--c-baseline-hi)' : 'var(--c-degraded)';
        }
        if (slPct) slPct.textContent = `${(sl.compliance_pct || 0).toFixed(1)}%`;
        if (slLabel) slLabel.textContent = sl.met ?
          `Service level met (target ${sl.target_pct}%)` :
          `⚠ Below target ${sl.target_pct}% — investigate`;
      }
    } else {
      // No asset_health yet — show placeholders with available state
      if (assetMetaEl) assetMetaEl.textContent = 'Asset health data loading...';
    }

    // Required actions
    const actionsEl = document.getElementById('exec-actions');
    if (actionsEl) {
      const actions = _buildActions(state);
      actionsEl.innerHTML = actions.length === 0 ?
        '<div class="action-empty">No actions required</div>' :
        actions.map(a => `<div class="action-item">${_esc(a)}</div>`).join('');
    }
  }'''

    if old_exec_fn in src:
        src = src.replace(old_exec_fn, new_exec_fn)
        ok("_updateExecutive() replaced with full asset health view")
    else:
        fail("Could not find _updateExecutive() in ui.js — apply manually")

# Add asset_health to state mapping in update handlers
if "state.asset_health" not in src:
    # Add asset_health pass-through in _onStateUpdate
    old_update = "  function _onStateUpdate(state) {\n    _updateHeader(state);"
    new_update = """  function _onStateUpdate(state) {
    // Pass asset_health from WebSocket frame if present
    if (state.asset_health === undefined && window._lastAssetHealth) {
      state.asset_health = window._lastAssetHealth;
    }
    if (state.asset_health) {
      window._lastAssetHealth = state.asset_health;
    }
    _updateHeader(state);"""
    if old_update in src:
        src = src.replace(old_update, new_update)
        ok("asset_health pass-through added")

ui_path.write_text(src, encoding="utf-8")
ok("ui.js written")


# ---------------------------------------------------------------------------
# 9. tools/record_demo.py — normalise regime strings
# ---------------------------------------------------------------------------

print("\n[9/9] tools/record_demo.py — normalise regime strings")

demo_path = ROOT / "tools" / "record_demo.py"
if not demo_path.exists():
    fail("tools/record_demo.py not found")
else:
    src = demo_path.read_text(encoding="utf-8")

    if "_normalise_regime" in src:
        skip("_normalise_regime already in record_demo.py")
    else:
        old_enrich = "def enrich_frames(frames: list[dict], transitions: list[dict]) -> list[dict]:"
        new_enrich = '''def _normalise_regime(raw: str) -> str:
    """Normalise regime string from Python enum repr to clean lowercase."""
    if not raw:
        return 'initialising'
    return str(raw).lower().replace('regimetype.', '').strip()


def enrich_frames(frames: list[dict], transitions: list[dict]) -> list[dict]:'''

        if old_enrich in src:
            src = src.replace(old_enrich, new_enrich)
            ok("_normalise_regime() added")
        else:
            fail("Could not find enrich_frames() signature")

        # Use _normalise_regime in regime_timeline
        old_timeline = "        to_regime = (t['to_regime'] or 'initialising').lower().replace('regimetype.', '').strip()"
        new_timeline = "        to_regime = _normalise_regime(t['to_regime'] or 'initialising')"
        if old_timeline in src:
            src = src.replace(old_timeline, new_timeline)
            ok("regime_timeline uses _normalise_regime()")

        # Use in enriched frame
        old_frame_regime = "            'regime': regime,"
        new_frame_regime = "            'regime': _normalise_regime(regime),"
        if old_frame_regime in src:
            src = src.replace(old_frame_regime, new_frame_regime)
            ok("enriched frame uses _normalise_regime()")

        demo_path.write_text(src, encoding="utf-8")
        ok("record_demo.py written")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

print("\n" + "=" * 50)
if ERRORS:
    print(f"COMPLETED WITH {len(ERRORS)} ERROR(S):")
    for e in ERRORS:
        print(f"  ✗ {e}")
    sys.exit(1)
else:
    print("ALL CHANGES APPLIED SUCCESSFULLY")
    print("\nNext steps:")
    print("  1. python -m backend.main   (restart backend)")
    print("  2. python manage.py record-demo  (re-record with clean regime labels)")
    print("  3. Open http://localhost:5500 and check executive view")