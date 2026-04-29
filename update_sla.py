"""
update_sla.py

Moves service level agreement from config.json to asset_registry.json
where it belongs — SLA is deployment-specific, not device-agnostic.

Changes:
1. asset_registry.json — add SLA to starkvind_01 commissioning data
2. config.json — demote service_level to defaults only
3. engine.py — _derive_service_level() reads SLA from asset registry
4. frontend/js/ui.js — update service level label text

Run from project root:
    python update_sla.py
"""

import json
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
# 1. asset_registry.json — add SLA to starkvind_01
# ---------------------------------------------------------------------------

print("\n[1/4] data/asset_registry.json — add SLA")

registry_path = ROOT / "data" / "asset_registry.json"
with open(registry_path, encoding="utf-8") as f:
    registry = json.load(f)

asset = registry.get("assets", {}).get("starkvind_01", {})

if "service_level_agreement" in asset:
    skip("SLA already in starkvind_01")
else:
    asset["service_level_agreement"] = {
        "pm25_threshold_ug_m3": 12.0,
        "compliance_target_pct": 95.0,
        "rolling_window_days": 30,
        "description": "Maintain PM2.5 below WHO guideline for 95% of operating hours",
        "commissioned_by": "technician",
        "review_date": None,
        "_comment": "SLA defined at commissioning — deployment specific, not device generic"
    }
    registry["assets"]["starkvind_01"] = asset

    with open(registry_path, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2)
    ok("SLA added to starkvind_01 in asset_registry.json")


# ---------------------------------------------------------------------------
# 2. config.json — demote service_level to defaults
# ---------------------------------------------------------------------------

print("\n[2/4] assets/config.json — demote to defaults")

config_path = ROOT / "assets" / "config.json"
with open(config_path, encoding="utf-8") as f:
    config = json.load(f)

if "service_level_defaults" in config:
    skip("service_level_defaults already in config.json")
else:
    # Rename service_level to service_level_defaults
    if "service_level" in config:
        config["service_level_defaults"] = config.pop("service_level")
        config["service_level_defaults"]["_comment"] = (
            "System defaults — used only when no SLA defined in asset_registry.json. "
            "SLA should be defined at commissioning time per asset."
        )
        ok("service_level renamed to service_level_defaults")
    else:
        config["service_level_defaults"] = {
            "pm25_threshold_ug_m3": 12.0,
            "compliance_target_pct": 95.0,
            "rolling_window_days": 30,
            "_comment": "System defaults — override per asset in asset_registry.json"
        }
        ok("service_level_defaults added")

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    ok("config.json written")


# ---------------------------------------------------------------------------
# 3. engine.py — read SLA from asset registry
# ---------------------------------------------------------------------------

print("\n[3/4] backend/twin_engine/engine.py — SLA from registry")

engine_path = ROOT / "backend" / "twin_engine" / "engine.py"
src = engine_path.read_text(encoding="utf-8")

if "service_level_agreement" in src:
    skip("engine already reads SLA from registry")
else:
    old_service_level = '''    def _derive_service_level(self) -> float:
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
            )'''

    new_service_level = '''    def _derive_service_level(self) -> float:
        """
        Calculate rolling 30-day service level compliance.
        Reads SLA from asset_registry.json if defined at commissioning.
        Falls back to config.json service_level_defaults.
        Returns percentage of readings below pm25 threshold.
        """
        try:
            # Read SLA from asset registry (deployment-specific)
            from backend.twin_engine.loader import get_asset
            asset = get_asset(self._asset_id,
                              Path(__file__).parent.parent.parent / "data" / "asset_registry.json")
            sla = getattr(asset, "service_level_agreement", None) or {}
            if not sla:
                # Try loading directly from registry dict
                import json as _json
                registry_path = Path(__file__).parent.parent.parent / "data" / "asset_registry.json"
                with open(registry_path, encoding="utf-8") as _f:
                    registry = _json.load(_f)
                sla = registry.get("assets", {}).get(
                    self._asset_id, {}
                ).get("service_level_agreement", {})

            # Fall back to config defaults if no SLA defined
            defaults = self._config.get("service_level_defaults",
                       self._config.get("service_level", {}))

            threshold = sla.get("pm25_threshold_ug_m3",
                        defaults.get("pm25_threshold_ug_m3", 12.0))
            window_days = sla.get("rolling_window_days",
                         defaults.get("rolling_window_days", 30))'''

    if old_service_level in src:
        src = src.replace(old_service_level, new_service_level)
        ok("_derive_service_level() reads SLA from asset registry")
        engine_path.write_text(src, encoding="utf-8")
    else:
        fail("Could not find _derive_service_level() — check engine.py manually")


# ---------------------------------------------------------------------------
# 4. frontend/js/ui.js — update service level label
# ---------------------------------------------------------------------------

print("\n[4/4] frontend/js/ui.js — service level label")

ui_path = ROOT / "frontend" / "js" / "ui.js"
src = ui_path.read_text(encoding="utf-8")

# Find and update the service level label text
old_label_v1 = "slLabel.textContent = sl.met ?"

if "service level agreements for air purification" in src:
    # Already has the new text from previous change — update to include SLA source
    old_label = (
        "`Asset meeting service level agreements for air purification "
        "(${sl.compliance_pct.toFixed(1)}% vs ${sl.target_pct}% target)`"
    )
    new_label = (
        "`Air quality SLA met — ${sl.compliance_pct.toFixed(1)}% compliance "
        "vs ${sl.target_pct}% target (30-day rolling)`"
    )
    breach_old = (
        "`⚠ Service level breach — ${sl.compliance_pct.toFixed(1)}% vs "
        "${sl.target_pct}% target. Investigate air quality events.`"
    )
    breach_new = (
        "`⚠ Air quality SLA breach — ${sl.compliance_pct.toFixed(1)}% compliance "
        "vs ${sl.target_pct}% target. Review events and investigate source.`"
    )
    if old_label in src:
        src = src.replace(old_label, new_label)
        src = src.replace(breach_old, breach_new)
        ui_path.write_text(src, encoding="utf-8")
        ok("Service level label updated to SLA language")
    else:
        skip("Service level label already updated")
elif old_label_v1 in src:
    # Original text — full replacement
    old_sl = (
        "if (slLabel) slLabel.textContent = sl.met ?\n"
        "          `Service level met (target ${sl.target_pct}%)` :\n"
        "          `⚠ Below target ${sl.target_pct}% — investigate`;"
    )
    new_sl = (
        "if (slLabel) slLabel.textContent = sl.met ?\n"
        "          `Air quality SLA met — ${sl.compliance_pct.toFixed(1)}% compliance "
        "vs ${sl.target_pct}% target (30-day rolling)` :\n"
        "          `⚠ Air quality SLA breach — ${sl.compliance_pct.toFixed(1)}% "
        "vs ${sl.target_pct}% target. Review events.`;"
    )
    if old_sl in src:
        src = src.replace(old_sl, new_sl)
        ui_path.write_text(src, encoding="utf-8")
        ok("Service level label updated")
    else:
        fail("Could not find service level label in ui.js")
else:
    skip("Service level label already correct")


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
    print("\nRestart backend and refresh frontend to see changes")
    print("SLA is now defined in data/asset_registry.json")
    print("Service level label reads: 'Air quality SLA met'")