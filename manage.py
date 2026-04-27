"""
manage.py — Air Twin project management CLI.

Single entry point for all project maintenance, updates, and validation.
Run from project root with venv active.

Usage:
    python manage.py <command> [options]

Commands:
    apply-update <name>     Apply a named update to the codebase
    list-updates            List all available updates and their status
    validate                Run all validation checks
    db-status               Show database table row counts
    record-demo             Launch interactive demo recording tool
    version                 Show current version and applied updates
    cleanup-scripts         Remove one-off scripts from project root

Updates:
    auto-mode               Auto mode dual-scale fan speed architecture
    imports                 Fix twin_engine import paths to backend.twin_engine
    spike-policy            Learned spike intervention policy
    event-regime            EVENT regime for transient air quality events
"""

import sys
import json
import argparse
import subprocess
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).parent
VERSION_FILE = ROOT / "data" / ".version.json"


# ---------------------------------------------------------------------------
# Version tracking
# ---------------------------------------------------------------------------

def load_version() -> dict:
    if VERSION_FILE.exists():
        with open(VERSION_FILE) as f:
            return json.load(f)
    return {"version": "0.1.0", "applied_updates": [], "last_updated": None}


def save_version(data: dict) -> None:
    VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(VERSION_FILE, "w") as f:
        json.dump(data, f, indent=2)


def mark_applied(update_name: str) -> None:
    data = load_version()
    if update_name not in data["applied_updates"]:
        data["applied_updates"].append(update_name)
        data["last_updated"] = datetime.now(timezone.utc).isoformat()
        save_version(data)


def is_applied(update_name: str) -> bool:
    return update_name in load_version().get("applied_updates", [])


# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------

def ok(msg):   print(f"  \033[92m✓\033[0m {msg}")
def skip(msg): print(f"  \033[93m–\033[0m {msg} (already applied)")
def fail(msg): print(f"  \033[91m✗\033[0m {msg}")
def info(msg): print(f"  \033[94m→\033[0m {msg}")
def bold(msg): return f"\033[1m{msg}\033[0m"


# ---------------------------------------------------------------------------
# Updates registry
# ---------------------------------------------------------------------------

UPDATES = {
    "auto-mode": {
        "description": "Auto mode dual-scale fan speed architecture (1-9 internal scale)",
        "files": [
            "assets/device_profiles.json",
            "backend/twin_engine/models.py",
            "backend/twin_engine/loader.py",
        ],
    },
    "imports": {
        "description": "Fix twin_engine import paths to backend.twin_engine",
        "files": ["backend/twin_engine/*.py", "backend/brief_generator.py"],
    },
    "spike-policy": {
        "description": "Learned spike intervention — observe first, evidence-based",
        "files": [
            "assets/config.json",
            "backend/twin_engine/models.py",
            "backend/twin_engine/engine.py",
        ],
    },
    "event-regime": {
        "description": "EVENT regime for transient air quality events (cooking etc)",
        "files": [
            "backend/twin_engine/models.py",
            "backend/twin_engine/regime.py",
            "backend/twin_engine/confidence.py",
            "backend/brief_generator.py",
        ],
    },
}

# Scripts in project root that get cleaned up after absorption
ONE_OFF_SCRIPTS = [
    "apply_auto_mode_updates.py",
    "fix_imports.py",
    "fix_import.py",
    "update_spike_policy.py",
    "add_event_regime.py",
]


# ---------------------------------------------------------------------------
# Update implementations
# ---------------------------------------------------------------------------

def apply_auto_mode(force: bool = False) -> bool:
    """Auto mode dual-scale fan speed architecture."""
    if is_applied("auto-mode") and not force:
        skip("auto-mode already applied")
        return True

    errors = []

    # device_profiles.json
    profiles_path = ROOT / "assets" / "device_profiles.json"
    try:
        with open(profiles_path) as f:
            profile = json.load(f)
        device = profile["devices"]["ikea_starkvind_e2007"]
        fan = device.get("fan_speeds", {})
        if "manual" not in fan:
            device["fan_speeds"] = {
                "manual": {"min": 1, "max": 5, "valid": [1, 2, 3, 4, 5]},
                "auto": {
                    "min": 1, "max": 9,
                    "valid": [1, 2, 3, 4, 5, 6, 7, 8, 9],
                    "_comment": "Auto mode 1-9 internal scale — separate from manual 1-5",
                },
            }
            perf = device.get("performance_model", {})
            perf["auto_mode_cadr_source"] = "empirical_only"
            device["performance_model"] = perf
            with open(profiles_path, "w") as f:
                json.dump(profile, f, indent=2)
            ok("device_profiles.json — fan_speeds split into manual/auto")
        else:
            skip("device_profiles.json fan_speeds already updated")
    except Exception as e:
        fail(f"device_profiles.json: {e}")
        errors.append(str(e))

    # models.py — DeviceProfile and TwinState
    models_path = ROOT / "backend" / "twin_engine" / "models.py"
    src = models_path.read_text(encoding="utf-8")
    changed = False

    if "auto_fan_speeds_valid" not in src:
        old = (
            "    fan_speeds_valid:       list[int]\n"
            "    fan_speed_min:          int\n"
            "    fan_speed_max:          int\n"
            "\n"
            "    # cadr[filter_config][str(fan_speed)] → CadrEntry"
        )
        new = (
            "    # Manual mode fan speeds — 1-5 for Starkvind\n"
            "    fan_speeds_valid:       list[int]\n"
            "    fan_speed_min:          int\n"
            "    fan_speed_max:          int\n"
            "\n"
            "    # Auto mode fan speeds — separate scale, separate empirical curve\n"
            "    auto_fan_speeds_valid:  list[int]\n"
            "    auto_fan_speed_min:     int\n"
            "    auto_fan_speed_max:     int\n"
            "    auto_mode_cadr_source:  str\n"
            "\n"
            "    # cadr[filter_config][str(fan_speed)] → CadrEntry"
        )
        if old in src:
            src = src.replace(old, new)
            changed = True
            ok("models.py — DeviceProfile auto fields added")
        else:
            fail("models.py DeviceProfile section not found")
            errors.append("DeviceProfile auto fields")
    else:
        skip("models.py DeviceProfile auto fields already present")

    if "empirical_cadr_auto_m3h" not in src:
        old = '    empirical_cadr_m3h:         dict = field(default_factory=dict)\n    # { "2": float|None, "3": float|None, "4": float|None }\n    performance_observation_counts: dict = field(default_factory=dict)\n    # { "1": int, "2": int, "3": int, "4": int, "5": int }'
        new = (
            '    empirical_cadr_m3h:         dict = field(default_factory=dict)\n'
            '    empirical_cadr_auto_m3h:    dict = field(default_factory=dict)\n'
            '    performance_observation_counts: dict = field(default_factory=dict)'
        )
        if old in src:
            src = src.replace(old, new)
            changed = True
            ok("models.py — TwinState empirical_cadr_auto_m3h added")
        else:
            skip("models.py TwinState auto fields — manual check may be needed")

    if changed:
        models_path.write_text(src, encoding="utf-8")

    mark_applied("auto-mode")
    return len(errors) == 0


def apply_imports(force: bool = False) -> bool:
    """Fix twin_engine import paths."""
    if is_applied("imports") and not force:
        skip("imports already applied")
        return True

    files = list((ROOT / "backend" / "twin_engine").glob("*.py"))
    files.append(ROOT / "backend" / "brief_generator.py")
    files.append(ROOT / "backend" / "main.py")

    total = 0
    for path in files:
        if not path.exists():
            continue
        src = path.read_text(encoding="utf-8")
        new_src = src.replace("from twin_engine.", "from backend.twin_engine.")
        new_src = new_src.replace(
            "from twin_engine import baseline, confidence, events, filter as filter_mod",
            "from backend.twin_engine import baseline, confidence, events, filter as filter_mod"
        )
        new_src = new_src.replace(
            "from twin_engine import loader, performance, regime",
            "from backend.twin_engine import loader, performance, regime"
        )
        if new_src != src:
            path.write_text(new_src, encoding="utf-8")
            count = src.count("from twin_engine")
            total += count
            ok(f"{path.name} — {count} import(s) updated")

    if total == 0:
        skip("all imports already correct")
    else:
        ok(f"Total: {total} import(s) updated")

    mark_applied("imports")
    return True


def apply_spike_policy(force: bool = False) -> bool:
    """Learned spike intervention policy."""
    if is_applied("spike-policy") and not force:
        skip("spike-policy already applied")
        return True

    config_path = ROOT / "assets" / "config.json"
    try:
        with open(config_path) as f:
            config = json.load(f)

        spikes = config.get("spikes", {})
        if "spike_response_model" not in spikes:
            spikes.update({
                "observation_window_minutes": 3,
                "min_spike_observations_to_learn": 10,
                "intervention_performance_threshold_prior": 0.6,
                "spike_response_model": {
                    "magnitude_brackets_ug_m3": [5, 15, 30, 50, 100],
                    "learned_adequate_performance_ratio": {},
                    "intervention_enabled": False,
                },
            })
            config["spikes"] = spikes
            config["control"]["degraded_command_speed"] = 4
            config["control"]["restore_auto_on_resolution"] = True
            with open(config_path, "w") as f:
                json.dump(config, f, indent=2)
            ok("config.json — spike learning architecture added")
        else:
            skip("config.json spike_response_model already present")
    except Exception as e:
        fail(f"config.json: {e}")
        return False

    mark_applied("spike-policy")
    return True


def apply_event_regime(force: bool = False) -> bool:
    """EVENT regime for transient air quality events."""
    if is_applied("event-regime") and not force:
        skip("event-regime already applied")
        return True

    errors = []

    # models.py — add EVENT to RegimeType
    models_path = ROOT / "backend" / "twin_engine" / "models.py"
    src = models_path.read_text(encoding="utf-8")
    if "EVENT" not in src:
        old = '    BASELINE     = "baseline"\n    DEGRADED     = "degraded"'
        new = '    BASELINE     = "baseline"\n    EVENT        = "event"\n    DEGRADED     = "degraded"'
        if old in src:
            src = src.replace(old, new)
            models_path.write_text(src, encoding="utf-8")
            ok("models.py — RegimeType.EVENT added")
        else:
            fail("models.py RegimeType not found")
            errors.append("RegimeType.EVENT")
    else:
        skip("models.py RegimeType.EVENT already present")

    # regime.py — add _evaluate_event and update evaluate()
    regime_path = ROOT / "backend" / "twin_engine" / "regime.py"
    src = regime_path.read_text(encoding="utf-8")

    if "_evaluate_event" not in src:
        # Update is_operational
        src = src.replace(
            "    return state.current_regime in (\n"
            "        RegimeType.BASELINE,\n"
            "        RegimeType.DEGRADED,\n"
            "    )",
            "    return state.current_regime in (\n"
            "        RegimeType.BASELINE,\n"
            "        RegimeType.EVENT,\n"
            "        RegimeType.DEGRADED,\n"
            "    )"
        )

        # Update evaluate() to handle EVENT
        src = src.replace(
            "    # --- BASELINE → DEGRADED on sustained exceedance ---\n"
            "    if current == RegimeType.BASELINE:",
            "    # --- BASELINE → EVENT on exceedance ---\n"
            "    if current == RegimeType.BASELINE:"
        )

        # Add EVENT evaluation after BASELINE block
        src = src.replace(
            "    # --- DEGRADED → BASELINE on sustained recovery ---",
            "    # --- EVENT → BASELINE (resolved) or DEGRADED (sustained) ---\n"
            "    if current == RegimeType.EVENT:\n"
            "        return _evaluate_event(\n"
            "            state, reading, asset_id,\n"
            "            deviation_from_locked,\n"
            "            degraded_entry_std_multiplier,\n"
            "            degraded_entry_duration_minutes,\n"
            "        )\n\n"
            "    # --- DEGRADED → BASELINE on sustained recovery ---"
        )

        # Update baseline transition to go to EVENT
        src = src.replace(
            "            return _transition_if_needed(\n"
            "                state, RegimeType.DEGRADED, asset_id,\n"
            "                reason=f\"Rolling mean exceeded baseline_locked + \"\n"
            "                       f\"{degraded_entry_std_multiplier}×std for \"\n"
            "                       f\"{degraded_entry_duration_minutes} minutes \"\n"
            "                       f\"(deviation={deviation_from_locked:.2f} std)\"\n"
            "            )",
            "            return _transition_if_needed(\n"
            "                state, RegimeType.EVENT, asset_id,\n"
            "                reason=f\"PM2.5 exceeded baseline_locked + \"\n"
            "                       f\"{degraded_entry_std_multiplier}×std — \"\n"
            "                       f\"monitoring for resolution \"\n"
            "                       f\"(deviation={deviation_from_locked:.2f} std)\"\n"
            "            )"
        )

        # Add _evaluate_event function
        event_fn = '''

def _evaluate_event(
    state: TwinState,
    reading: Reading,
    asset_id: str,
    deviation_from_locked: Optional[float],
    degraded_entry_std_multiplier: float,
    degraded_entry_duration_minutes: float,
) -> tuple[TwinState, Optional[RegimeTransition]]:
    """
    Evaluate EVENT regime — transient air quality event in progress.
    Resolves to BASELINE if PM2.5 returns to range within window.
    Escalates to DEGRADED if elevation is sustained.
    """
    if asset_id not in _exceedance_minutes:
        _exceedance_minutes[asset_id] = 0.0

    if deviation_from_locked is None:
        state = _update_duration(state)
        return state, None

    if deviation_from_locked > degraded_entry_std_multiplier:
        _exceedance_minutes[asset_id] += 1.0 / 60.0
        if _exceedance_minutes[asset_id] >= degraded_entry_duration_minutes:
            reset_trackers(asset_id)
            return _transition_if_needed(
                state, RegimeType.DEGRADED, asset_id,
                reason=f"EVENT not resolved within {degraded_entry_duration_minutes} min "
                       f"— escalating to DEGRADED"
            )
    else:
        reset_trackers(asset_id)
        return _transition_if_needed(
            state, RegimeType.BASELINE, asset_id,
            reason="Transient event resolved — PM2.5 returned to baseline range"
        )

    state = _update_duration(state)
    return state, None

'''
        # Insert before _evaluate_degraded
        src = src.replace(
            "\ndef _evaluate_degraded(",
            event_fn + "\ndef _evaluate_degraded("
        )

        regime_path.write_text(src, encoding="utf-8")
        ok("regime.py — _evaluate_event() added, transitions updated")
    else:
        skip("regime.py _evaluate_event() already present")

    # confidence.py — add REGIME_CONCLUSIONS and update confidence_conclusion
    confidence_path = ROOT / "backend" / "twin_engine" / "confidence.py"
    src = confidence_path.read_text(encoding="utf-8")

    if "REGIME_CONCLUSIONS" not in src:
        regime_conclusions = '''

REGIME_CONCLUSIONS = {
    "event":        "Temporary air quality event detected. Purifier responding.",
    "degraded":     "Air quality degraded. Investigate source.",
    "initialising": "System establishing baseline — no action required.",
    "validating":   "Baseline re-establishing after maintenance.",
    "unknown":      "Sensor data unavailable — check connections.",
}
'''
        src = src.replace(
            "\ndef confidence_conclusion(",
            regime_conclusions + "\ndef confidence_conclusion("
        )

        src = src.replace(
            'def confidence_conclusion(confidence: float) -> str:\n'
            '    """\n'
            '    Return the tiered conclusion string for a given confidence score.\n'
            '    Used by brief_generator.py to produce the executive brief conclusion.\n'
            '    """\n'
            '    for low, high, conclusion in CONFIDENCE_TIERS:\n'
            '        if low <= confidence <= high:\n'
            '            return conclusion\n'
            '    return CONFIDENCE_TIERS[-1][2]',
            'def confidence_conclusion(confidence: float, regime: str = "baseline") -> str:\n'
            '    """\n'
            '    Return tiered conclusion for confidence score and regime.\n'
            '    Regime-specific conclusions take precedence for non-baseline regimes.\n'
            '    """\n'
            '    regime_key = regime.lower().replace("regimetype.", "").strip()\n'
            '    if regime_key in REGIME_CONCLUSIONS:\n'
            '        return REGIME_CONCLUSIONS[regime_key]\n'
            '    for low, high, conclusion in CONFIDENCE_TIERS:\n'
            '        if low <= confidence <= high:\n'
            '            return conclusion\n'
            '    return CONFIDENCE_TIERS[-1][2]'
        )
        confidence_path.write_text(src, encoding="utf-8")
        ok("confidence.py — REGIME_CONCLUSIONS and regime-aware conclusion added")
    else:
        skip("confidence.py REGIME_CONCLUSIONS already present")

    # brief_generator.py
    brief_path = ROOT / "backend" / "brief_generator.py"
    src = brief_path.read_text(encoding="utf-8")

    if "regime_str" not in src:
        src = src.replace(
            "    conclusion = confidence_conclusion(state.confidence)",
            "    regime_str = str(state.current_regime).lower().replace(\"regimetype.\", \"\")\n"
            "    conclusion = confidence_conclusion(state.confidence, regime=regime_str)"
        )
        ok("brief_generator.py — regime passed to confidence_conclusion")

    if "RegimeType.EVENT" not in src:
        src = src.replace(
            "    if state.current_regime == RegimeType.DEGRADED:\n"
            "        actions.append(\"Air quality degraded — investigate source and increase ventilation\")",
            "    if state.current_regime == RegimeType.DEGRADED:\n"
            "        actions.append(\"Air quality degraded — investigate source and increase ventilation\")\n\n"
            "    if state.current_regime == RegimeType.EVENT:\n"
            "        actions.append(\n"
            "            \"Temporary air quality event in progress — purifier responding. \"\n"
            "            \"If source is known (cooking, candle), no action required. \"\n"
            "            \"If source unknown, investigate.\"\n"
            "        )"
        )
        ok("brief_generator.py — EVENT required action added")

    brief_path.write_text(src, encoding="utf-8")

    if not errors:
        mark_applied("event-regime")
    return len(errors) == 0


UPDATE_FUNCTIONS = {
    "auto-mode":    apply_auto_mode,
    "imports":      apply_imports,
    "spike-policy": apply_spike_policy,
    "event-regime": apply_event_regime,
}


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_apply_update(args):
    name = args.name
    force = getattr(args, 'force', False)

    if name not in UPDATE_FUNCTIONS:
        print(f"Unknown update: {name}")
        print(f"Available: {', '.join(UPDATE_FUNCTIONS.keys())}")
        sys.exit(1)

    print(f"\n{bold(f'Applying update: {name}')}")
    print(f"  {UPDATES[name]['description']}\n")

    success = UPDATE_FUNCTIONS[name](force=force)

    print()
    if success:
        print(f"\033[92m{bold('Done.')}\033[0m")
    else:
        print(f"\033[91m{bold('Completed with errors — check output above.')}\033[0m")
        sys.exit(1)


def cmd_list_updates(args):
    print(f"\n{bold('Available updates:')}\n")
    version_data = load_version()
    applied = version_data.get("applied_updates", [])

    for name, info_data in UPDATES.items():
        status = "\033[92m✓ applied\033[0m" if name in applied else "\033[93m○ pending\033[0m"
        print(f"  {status}  {bold(name)}")
        print(f"           {info_data['description']}")
        print()


def cmd_validate(args):
    print(f"\n{bold('Running validation checks...')}\n")
    errors = []

    # Check twin_engine imports
    for path in (ROOT / "backend" / "twin_engine").glob("*.py"):
        src = path.read_text(encoding="utf-8")
        if "from twin_engine." in src or "from twin_engine import" in src:
            fail(f"{path.name} — bare twin_engine imports found")
            errors.append(path.name)
        else:
            ok(f"{path.name} — imports clean")

    # Check config files exist
    for config in ["assets/config.json", "assets/device_profiles.json",
                   "data/asset_registry.json"]:
        path = ROOT / config
        if path.exists():
            ok(f"{config} exists")
        else:
            fail(f"{config} missing")
            errors.append(config)

    # Check RegimeType.EVENT
    try:
        sys.path.insert(0, str(ROOT))
        from backend.twin_engine.models import RegimeType
        assert RegimeType.EVENT == "event"
        ok("RegimeType.EVENT = 'event'")
    except Exception as e:
        fail(f"RegimeType.EVENT: {e}")
        errors.append("RegimeType.EVENT")

    # Check confidence_conclusion accepts regime
    try:
        from backend.twin_engine.confidence import confidence_conclusion
        result = confidence_conclusion(0.9, regime="event")
        assert "event" in result.lower() or "temporary" in result.lower()
        ok(f"confidence_conclusion(regime='event') = '{result}'")
    except Exception as e:
        fail(f"confidence_conclusion: {e}")
        errors.append("confidence_conclusion")

    print()
    if errors:
        print(f"\033[91m{bold(f'Validation failed — {len(errors)} error(s)')}\033[0m")
        sys.exit(1)
    else:
        print(f"\033[92m{bold('All checks pass')}\033[0m")


def cmd_db_status(args):
    import sqlite3
    db_path = ROOT / "data" / "airtwin.db"
    if not db_path.exists():
        print(f"\033[91mDatabase not found: {db_path}\033[0m")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    print(f"\n{bold('Database status:')} {db_path}\n")

    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()

    for (table,) in tables:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        bar_width = min(30, count // 100)
        bar = "█" * bar_width
        print(f"  {table:<30} {count:>8,} rows  {bar}")

    conn.close()
    print()


def cmd_record_demo(args):
    demo_script = ROOT / "tools" / "record_demo.py"
    if not demo_script.exists():
        print(f"\033[91mrecord_demo.py not found at {demo_script}\033[0m")
        sys.exit(1)
    subprocess.run([sys.executable, str(demo_script)])


def cmd_version(args):
    data = load_version()
    print(f"\n{bold('Air Twin')}")
    print(f"  Version:      {data.get('version', 'unknown')}")
    print(f"  Last updated: {data.get('last_updated', 'never')}")
    applied = data.get("applied_updates", [])
    pending = [k for k in UPDATES if k not in applied]
    print(f"  Applied:      {', '.join(applied) if applied else 'none'}")
    print(f"  Pending:      {', '.join(pending) if pending else 'none'}")
    print()


def cmd_cleanup_scripts(args):
    print(f"\n{bold('Cleaning up one-off scripts from project root...')}\n")

    removed = []
    for script in ONE_OFF_SCRIPTS:
        path = ROOT / script
        if path.exists():
            if not getattr(args, 'dry_run', False):
                path.unlink()
                removed.append(script)
                ok(f"Removed {script}")
            else:
                info(f"Would remove {script} (dry run)")
        else:
            skip(f"{script} not found")

    if removed and not getattr(args, 'dry_run', False):
        print()
        commit = input("  Commit removal? [Y/n]: ").strip().lower()
        if commit in ('', 'y', 'yes'):
            subprocess.run(["git", "add", "-A"], cwd=ROOT)
            subprocess.run(
                ["git", "commit", "-m", "Remove one-off scripts — absorbed into manage.py"],
                cwd=ROOT
            )
            subprocess.run(["git", "push"], cwd=ROOT)
            ok("Committed and pushed")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    if sys.platform == 'win32':
        import os
        os.system('color')

    parser = argparse.ArgumentParser(
        prog="manage.py",
        description="Air Twin project management CLI",
    )
    subparsers = parser.add_subparsers(dest="command")

    # apply-update
    p_apply = subparsers.add_parser("apply-update", help="Apply a named update")
    p_apply.add_argument("name", choices=list(UPDATE_FUNCTIONS.keys()))
    p_apply.add_argument("--force", action="store_true",
                         help="Re-apply even if already marked applied")

    # list-updates
    subparsers.add_parser("list-updates", help="List all updates and their status")

    # validate
    subparsers.add_parser("validate", help="Run all validation checks")

    # db-status
    subparsers.add_parser("db-status", help="Show database row counts")

    # record-demo
    subparsers.add_parser("record-demo", help="Launch interactive demo recording tool")

    # version
    subparsers.add_parser("version", help="Show version and applied updates")

    # cleanup-scripts
    p_clean = subparsers.add_parser("cleanup-scripts",
                                     help="Remove one-off scripts from project root")
    p_clean.add_argument("--dry-run", action="store_true",
                          help="Show what would be removed without removing")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    commands = {
        "apply-update":    cmd_apply_update,
        "list-updates":    cmd_list_updates,
        "validate":        cmd_validate,
        "db-status":       cmd_db_status,
        "record-demo":     cmd_record_demo,
        "version":         cmd_version,
        "cleanup-scripts": cmd_cleanup_scripts,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()