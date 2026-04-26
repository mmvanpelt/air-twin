"""
apply_auto_mode_updates.py

One-shot script that applies the auto mode dual-scale architecture changes
to all three files:
  - assets/device_profiles.json
  - backend/twin_engine/models.py
  - backend/twin_engine/loader.py

Run from the project root:
  python3 apply_auto_mode_updates.py

Prints a summary of every change made. Safe to re-run — checks current
state before applying each change and skips if already applied.
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
ERRORS = []


def ok(msg): print(f"  ✓ {msg}")
def skip(msg): print(f"  – {msg} (already applied)")
def fail(msg):
    print(f"  ✗ {msg}")
    ERRORS.append(msg)


# ---------------------------------------------------------------------------
# 1. assets/device_profiles.json
# ---------------------------------------------------------------------------

print("\n[1/3] assets/device_profiles.json")

path = ROOT / "assets" / "device_profiles.json"
with open(path) as f:
    profile = json.load(f)

device = profile["devices"]["ikea_starkvind_e2007"]

# Update fan_speeds
fan = device.get("fan_speeds", {})
if "manual" in fan:
    skip("fan_speeds already has manual/auto sub-blocks")
else:
    device["fan_speeds"] = {
        "manual": {
            "min": 1,
            "max": 5,
            "valid": [1, 2, 3, 4, 5]
        },
        "auto": {
            "min": 1,
            "max": 9,
            "valid": [1, 2, 3, 4, 5, 6, 7, 8, 9],
            "_comment": (
                "Auto mode uses a finer 1-9 internal scale distinct from "
                "manual 1-5. Steps are not mapped to manual equivalents — "
                "twin builds separate empirical performance curves per auto step."
            )
        }
    }
    ok("fan_speeds split into manual/auto sub-blocks")

# Add auto_mode_cadr_source to performance_model
perf = device.get("performance_model", {})
if "auto_mode_cadr_source" in perf:
    skip("auto_mode_cadr_source already in performance_model")
else:
    perf["auto_mode_cadr_source"] = "empirical_only"
    perf["_auto_mode_comment"] = (
        "No manufacturer CADR exists for auto mode steps 1-9. "
        "Performance assessment in auto mode withheld until sufficient "
        "empirical observations exist per step. Manual mode uses "
        "manufacturer CADR anchors from day one."
    )
    device["performance_model"] = perf
    ok("auto_mode_cadr_source added to performance_model")

with open(path, "w") as f:
    json.dump(profile, f, indent=2)
ok("device_profiles.json written")


# ---------------------------------------------------------------------------
# 2. backend/twin_engine/models.py
# ---------------------------------------------------------------------------

print("\n[2/3] backend/twin_engine/models.py")

path = ROOT / "backend" / "twin_engine" / "models.py"
src = path.read_text(encoding="utf-8")

# --- Update DeviceProfile dataclass ---
OLD_DEVICE_PROFILE_FANS = '''\
    fan_speeds_valid:       list[int]
    fan_speed_min:          int
    fan_speed_max:          int

    cadr:'''

NEW_DEVICE_PROFILE_FANS = '''\
    # Manual mode fan speeds — 1-5 for Starkvind
    fan_speeds_valid:       list[int]
    fan_speed_min:          int
    fan_speed_max:          int

    # Auto mode fan speeds — separate scale, separate empirical curve
    # 1-9 for Starkvind. Empty list if device has no distinct auto scale.
    auto_fan_speeds_valid:  list[int]
    auto_fan_speed_min:     int
    auto_fan_speed_max:     int
    auto_mode_cadr_source:  str   # "empirical_only" | "manufacturer"

    cadr:'''

if "auto_fan_speeds_valid" in src:
    skip("DeviceProfile already has auto_fan_speeds fields")
elif OLD_DEVICE_PROFILE_FANS in src:
    src = src.replace(OLD_DEVICE_PROFILE_FANS, NEW_DEVICE_PROFILE_FANS)
    ok("DeviceProfile updated with auto_fan_speeds fields")
else:
    fail("Could not find DeviceProfile fan_speeds section to update — check models.py manually")

# --- Update TwinState performance fields ---
OLD_PERF_FIELDS = '''\
    # --- Performance ---
    room_efficiency_factor:     float = 1.0
    # Empirical CADR estimates for speeds 2-4, keyed by str(fan_speed).
    # None until min_observations threshold met. Supersede interpolated
    # priors from device profile once promoted.
    empirical_cadr_m3h:         dict = field(default_factory=dict)
    # { "2": float|None, "3": float|None, "4": float|None }
    performance_observation_counts: dict = field(default_factory=dict)
    # { "1": int, "2": int, "3": int, "4": int, "5": int }'''

NEW_PERF_FIELDS = '''\
    # --- Performance ---
    room_efficiency_factor:     float = 1.0

    # Empirical CADR estimates for manual mode speeds 2-4 (m³/h).
    # Keyed by str(fan_speed). Supersede interpolated priors from device
    # profile once empirical_cadr_min_observations threshold is met.
    # Speeds 1 and 5 always use manufacturer anchors — never updated here.
    empirical_cadr_m3h:         dict = field(default_factory=dict)
    # { "2": float, "3": float, "4": float }

    # Empirical performance curves for auto mode steps 1-9 (m³/h).
    # Keyed by str(auto_step). Built entirely from observations —
    # no manufacturer anchors exist for auto mode steps.
    empirical_cadr_auto_m3h:    dict = field(default_factory=dict)
    # { "1": float, "2": float, ..., "9": float }

    # Observation counts per speed — manual and auto tracked separately
    performance_observation_counts:      dict = field(default_factory=dict)
    # { "manual": {"1": int, ..., "5": int}, "auto": {"1": int, ..., "9": int} }'''

if "empirical_cadr_auto_m3h" in src:
    skip("TwinState already has empirical_cadr_auto_m3h field")
elif OLD_PERF_FIELDS in src:
    src = src.replace(OLD_PERF_FIELDS, NEW_PERF_FIELDS)
    ok("TwinState performance fields updated")
else:
    fail("Could not find TwinState performance fields to update — check models.py manually")

# --- Update to_dict() ---
OLD_TO_DICT_PERF = '''\
            "room_efficiency_factor":          self.room_efficiency_factor,
            "empirical_cadr_m3h":              self.empirical_cadr_m3h,
            "performance_observation_counts":  self.performance_observation_counts,'''

NEW_TO_DICT_PERF = '''\
            "room_efficiency_factor":          self.room_efficiency_factor,
            "empirical_cadr_m3h":              self.empirical_cadr_m3h,
            "empirical_cadr_auto_m3h":         self.empirical_cadr_auto_m3h,
            "performance_observation_counts":  self.performance_observation_counts,'''

if '"empirical_cadr_auto_m3h":         self.empirical_cadr_auto_m3h' in src:
    skip("to_dict() already has empirical_cadr_auto_m3h")
elif OLD_TO_DICT_PERF in src:
    src = src.replace(OLD_TO_DICT_PERF, NEW_TO_DICT_PERF)
    ok("to_dict() updated with empirical_cadr_auto_m3h")
else:
    fail("Could not find to_dict() performance section — check models.py manually")

# --- Update from_dict() ---
OLD_FROM_DICT_PERF = '''\
            room_efficiency_factor=get("room_efficiency_factor", 1.0),
            empirical_cadr_m3h=get("empirical_cadr_m3h", {}),
            performance_observation_counts=get("performance_observation_counts", {}),'''

NEW_FROM_DICT_PERF = '''\
            room_efficiency_factor=get("room_efficiency_factor", 1.0),
            empirical_cadr_m3h=get("empirical_cadr_m3h", {}),
            empirical_cadr_auto_m3h=get("empirical_cadr_auto_m3h", {}),
            performance_observation_counts=get("performance_observation_counts", {}),'''

if 'empirical_cadr_auto_m3h=get("empirical_cadr_auto_m3h"' in src:
    skip("from_dict() already has empirical_cadr_auto_m3h")
elif OLD_FROM_DICT_PERF in src:
    src = src.replace(OLD_FROM_DICT_PERF, NEW_FROM_DICT_PERF)
    ok("from_dict() updated with empirical_cadr_auto_m3h")
else:
    fail("Could not find from_dict() performance section — check models.py manually")

path.write_text(src, encoding="utf-8")
ok("models.py written")


# ---------------------------------------------------------------------------
# 3. backend/twin_engine/loader.py
# ---------------------------------------------------------------------------

print("\n[3/3] backend/twin_engine/loader.py")

path = ROOT / "backend" / "twin_engine" / "loader.py"
src = path.read_text(encoding="utf-8")

OLD_LOADER_FAN = '''\
    fan = d.get("fan_speeds", {})
    control = d.get("control", {})
    perf = d.get("performance_model", {})

    profile = DeviceProfile(
        model_id=model_id,
        manufacturer=d["manufacturer"],
        model=d["model"],
        device_type=d["type"],
        sku=d["sku"],
        zigbee_friendly_name=d["zigbee_friendly_name"],
        fan_speeds_valid=fan.get("valid", [1, 2, 3, 4, 5]),
        fan_speed_min=fan.get("min", 1),
        fan_speed_max=fan.get("max", 5),
        cadr=cadr,'''

NEW_LOADER_FAN = '''\
    fan = d.get("fan_speeds", {})
    manual_fan = fan.get("manual", fan)  # fall back to flat structure for older profiles
    auto_fan = fan.get("auto", {})
    control = d.get("control", {})
    perf = d.get("performance_model", {})

    profile = DeviceProfile(
        model_id=model_id,
        manufacturer=d["manufacturer"],
        model=d["model"],
        device_type=d["type"],
        sku=d["sku"],
        zigbee_friendly_name=d["zigbee_friendly_name"],
        fan_speeds_valid=manual_fan.get("valid", [1, 2, 3, 4, 5]),
        fan_speed_min=manual_fan.get("min", 1),
        fan_speed_max=manual_fan.get("max", 5),
        auto_fan_speeds_valid=auto_fan.get("valid", []),
        auto_fan_speed_min=auto_fan.get("min", 1),
        auto_fan_speed_max=auto_fan.get("max", 9),
        auto_mode_cadr_source=perf.get("auto_mode_cadr_source", "empirical_only"),
        cadr=cadr,'''

if "auto_fan_speeds_valid" in src:
    skip("loader.py already has auto_fan_speeds_valid")
elif OLD_LOADER_FAN in src:
    src = src.replace(OLD_LOADER_FAN, NEW_LOADER_FAN)
    ok("loader.py updated with auto mode fan speed parsing")
else:
    fail("Could not find loader.py fan_speeds section — check loader.py manually")

path.write_text(src, encoding="utf-8")
ok("loader.py written")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

print("\n" + "="*50)
if ERRORS:
    print(f"COMPLETED WITH {len(ERRORS)} ERROR(S) — manual fixes required:")
    for e in ERRORS:
        print(f"  ✗ {e}")
    sys.exit(1)
else:
    print("ALL CHANGES APPLIED SUCCESSFULLY")
    print("\nRun validation test:")
    print("  cd backend")
    print('  python3 -c "')
    print("  import sys; sys.path.insert(0, '.')")
    print("  import twin_engine.loader as loader")
    print("  loader.configure('../assets/device_profiles.json', '../data/asset_registry.json')")
    print("  profile = loader.get_device_profile('ikea_starkvind_e2007')")
    print("  print(f'Manual speeds: {profile.fan_speeds_valid}')")
    print("  print(f'Auto speeds: {profile.auto_fan_speeds_valid}')")
    print("  print(f'Auto CADR source: {profile.auto_mode_cadr_source}')")
    print("  from twin_engine.models import TwinState")
    print("  state = TwinState()")
    print("  state2 = TwinState.from_dict(state.to_dict())")
    print("  assert state2.empirical_cadr_auto_m3h == {}")
    print("  print('All checks pass')")
    print('  "')