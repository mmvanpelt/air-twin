"""
loader.py — Device profile and asset registry loader for the Air Twin twin engine.

This module is the sole interface between the twin engine and the backing store
for device profiles and asset registry. All other modules call get_device_profile()
and get_asset() — they never read JSON files directly.

Backing store is currently flat JSON files. To swap to a database or API later,
only this module changes. All engine logic remains untouched.

Loader functions are intentionally stateless — they read from disk on each call.
Caching is the responsibility of engine.py if needed. This keeps the loader
simple and testable.
"""

import json
import logging
import math
from pathlib import Path
from typing import Optional

from backend.twin_engine.models import (
    Asset,
    AssetFilterState,
    AssetPlacement,
    CadrEntry,
    DeviceProfile,
    FilterProfile,
    FilterType,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Backing store paths — set once by engine.py on startup via configure()
# ---------------------------------------------------------------------------

_device_profiles_path: Optional[Path] = None
_asset_registry_path: Optional[Path] = None


def configure(device_profiles_path: str, asset_registry_path: str) -> None:
    """
    Configure loader paths. Must be called by engine.py before any
    get_device_profile() or get_asset() calls.

    Args:
        device_profiles_path: Path to assets/device_profiles.json
        asset_registry_path:  Path to data/asset_registry.json
    """
    global _device_profiles_path, _asset_registry_path
    _device_profiles_path = Path(device_profiles_path)
    _asset_registry_path  = Path(asset_registry_path)
    logger.info(f"Loader configured — profiles: {_device_profiles_path}, registry: {_asset_registry_path}")


# ---------------------------------------------------------------------------
# Internal — raw JSON reads
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> dict:
    """
    Read and parse a JSON file. Raises clear errors on missing or malformed files.
    Comments (keys starting with '_') are silently ignored by the parsers below —
    they are never returned to callers.
    """
    if not path.exists():
        raise FileNotFoundError(f"Required file not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Malformed JSON in {path}: {e}")


def _strip_comments(d: dict) -> dict:
    """
    Recursively remove comment keys (keys starting with '_') from a dict.
    Allows JSON files to carry inline documentation without affecting parsing.
    """
    result = {}
    for k, v in d.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict):
            result[k] = _strip_comments(v)
        else:
            result[k] = v
    return result


# ---------------------------------------------------------------------------
# CADR interpolation
# ---------------------------------------------------------------------------

def _interpolate_cadr_power_law(speed: int, anchors: dict[str, CadrEntry]) -> float:
    """
    Interpolate CADR for an unpublished intermediate fan speed using a power-law
    curve anchored to the published min and max values.

    Fan motor airflow scales approximately with RPM. Power scales with RPM^3.
    Linear interpolation is not physically correct for this device class.

    The exponent is derived by fitting the power law to the two anchor points:
        cadr = a * speed^exponent
    Solving for exponent:
        exponent = log(cadr_max / cadr_min) / log(speed_max / speed_min)

    Args:
        speed:   Fan speed to interpolate (2, 3, or 4)
        anchors: Dict of CadrEntry keyed by str(fan_speed), must contain "1" and "5"

    Returns:
        Interpolated CADR in m³/h
    """
    cadr_min = anchors["1"].value
    cadr_max = anchors["5"].value

    if cadr_min is None or cadr_max is None:
        raise ValueError("Cannot interpolate — anchor values for speeds 1 and 5 are required")

    speed_min = 1
    speed_max = 5

    exponent = math.log(cadr_max / cadr_min) / math.log(speed_max / speed_min)
    a = cadr_min / (speed_min ** exponent)
    interpolated = a * (speed ** exponent)

    logger.debug(f"Power-law interpolation speed {speed}: {interpolated:.1f} m³/h "
                 f"(exponent={exponent:.3f}, anchors={cadr_min}/{cadr_max})")
    return round(interpolated, 1)


def _parse_cadr_config(cadr_raw: dict, interpolation_policy: str) -> dict[str, dict[str, CadrEntry]]:
    """
    Parse the cadr block from device_profiles.json into a typed dict.
    Applies interpolation policy to null intermediate speed values.

    Returns:
        { filter_config: { str(fan_speed): CadrEntry } }
    """
    result = {}
    skip_keys = {"interpolation_policy"}

    for filter_config, speeds in cadr_raw.items():
        if filter_config.startswith("_") or filter_config in skip_keys:
            continue

        entries: dict[str, CadrEntry] = {}
        for speed_str, entry_raw in speeds.items():
            if speed_str.startswith("_"):
                continue
            entries[speed_str] = CadrEntry(
                value=entry_raw.get("value"),
                source=entry_raw.get("source", "unknown"),
                source_detail=entry_raw.get("source_detail", ""),
            )

        # Apply interpolation to null intermediate speeds
        if interpolation_policy == "power_law":
            for speed in [2, 3, 4]:
                speed_str = str(speed)
                if speed_str in entries and entries[speed_str].value is None:
                    try:
                        interpolated = _interpolate_cadr_power_law(speed, entries)
                        entries[speed_str] = CadrEntry(
                            value=interpolated,
                            source="interpolated",
                            source_detail=f"Power-law interpolation between speed 1 "
                                          f"({entries['1'].value} m³/h) and speed 5 "
                                          f"({entries['5'].value} m³/h). "
                                          f"Not published by manufacturer.",
                        )
                    except Exception as e:
                        logger.warning(f"Interpolation failed for speed {speed} "
                                       f"in {filter_config}: {e}")

        result[filter_config] = entries

    return result


# ---------------------------------------------------------------------------
# Public — device profile loader
# ---------------------------------------------------------------------------

def get_device_profile(model_id: str) -> DeviceProfile:
    """
    Load and parse a device profile by model ID.

    Reads from the configured device_profiles.json. Comment keys are stripped.
    CADR interpolation is applied per the profile's interpolation_policy.
    Returns a fully typed DeviceProfile dataclass — callers never see raw JSON.

    Args:
        model_id: Key in device_profiles.json "devices" dict,
                  e.g. "ikea_starkvind_e2007"

    Returns:
        DeviceProfile dataclass

    Raises:
        FileNotFoundError: If device_profiles.json does not exist
        KeyError:          If model_id is not found in the profiles file
        ValueError:        If the file is malformed
    """
    if _device_profiles_path is None:
        raise RuntimeError("Loader not configured — call loader.configure() first")

    raw = _read_json(_device_profiles_path)
    devices = raw.get("devices", {})

    if model_id not in devices:
        available = [k for k in devices if not k.startswith("_")]
        raise KeyError(f"Device profile '{model_id}' not found. "
                       f"Available profiles: {available}")

    d = _strip_comments(devices[model_id])

    interpolation_policy = d.get("cadr", {}).get("interpolation_policy", "none")
    cadr = _parse_cadr_config(d.get("cadr", {}), interpolation_policy)

    # Parse filter types
    filter_types: dict[str, FilterProfile] = {}
    for ft_key, ft_raw in d.get("filter_types", {}).items():
        if ft_key.startswith("_"):
            continue
        filter_types[ft_key] = FilterProfile(
            label=ft_raw["label"],
            description=ft_raw["description"],
            filter_life_hours=ft_raw["filter_life_hours"],
            filter_life_source=ft_raw["filter_life_source"],
            cadr_profile=ft_raw["cadr_profile"],
        )

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
        cadr=cadr,
        interpolation_policy=interpolation_policy,
        filter_types=filter_types,
        default_filter_type=d.get("default_filter_type", "particle_only"),
        performance_ratio_degradation_threshold=perf.get(
            "performance_ratio_degradation_threshold", 0.70),
        empirical_cadr_min_observations=perf.get("empirical_cadr_min_observations", 10),
        clogged_filter_physics=perf.get("clogged_filter_physics", "fixed_rpm"),
        command_acknowledgement_timeout_s=control.get(
            "command_acknowledgement_timeout_s", 10),
        valid_fan_modes=control.get("valid_fan_modes", ["auto", "manual"]),
    )

    logger.info(f"Loaded device profile: {model_id} ({profile.manufacturer} {profile.model})")
    return profile


# ---------------------------------------------------------------------------
# Public — asset registry loader
# ---------------------------------------------------------------------------

def get_asset(asset_id: str) -> Asset:
    """
    Load and parse an asset registry entry by asset ID.

    Reads from the configured asset_registry.json. Returns a fully typed
    Asset dataclass — callers never see raw JSON.

    Args:
        asset_id: Key in asset_registry.json "assets" dict,
                  e.g. "starkvind_01"

    Returns:
        Asset dataclass

    Raises:
        FileNotFoundError: If asset_registry.json does not exist
        KeyError:          If asset_id is not found in the registry
        ValueError:        If the file is malformed
    """
    if _asset_registry_path is None:
        raise RuntimeError("Loader not configured — call loader.configure() first")

    raw = _read_json(_asset_registry_path)
    assets = raw.get("assets", {})

    if asset_id not in assets:
        available = [k for k in assets if not k.startswith("_")]
        raise KeyError(f"Asset '{asset_id}' not found. "
                       f"Available assets: {available}")

    a = _strip_comments(assets[asset_id])

    placement_raw = a.get("placement", {})
    placement = AssetPlacement(
        section=placement_raw.get("section"),
        position_m=placement_raw.get("position_m"),
        blender_object_name=placement_raw.get("blender_object_name"),
        position_source=placement_raw.get("position_source", "unknown"),
    )

    filter_raw = a.get("filter", {})
    installed_type_str = filter_raw.get("installed_type", "particle_only")
    last_type_str = filter_raw.get("last_logged_filter_type")

    filter_state = AssetFilterState(
        installed_type=FilterType(installed_type_str),
        filter_change_device_age_anchor=filter_raw.get("filter_change_device_age_anchor"),
        last_logged_filter_type=FilterType(last_type_str) if last_type_str else None,
    )

    room_raw = a.get("room", {})

    asset = Asset(
        asset_id=asset_id,
        device_profile_id=a["device_profile"],
        sku=a["sku"],
        zigbee_ieee=a["zigbee_ieee"],
        zigbee_friendly_name=a["zigbee_friendly_name"],
        room_volume_m3=room_raw["volume_m3"],
        room_shape=room_raw.get("shape", "unknown"),
        placement=placement,
        filter_state=filter_state,
        commissioned_at=a.get("commissioned_at"),
        notes=a.get("notes"),
    )

    logger.info(f"Loaded asset: {asset_id} "
                f"(profile: {asset.device_profile_id}, "
                f"volume: {asset.room_volume_m3} m³)")
    return asset


# ---------------------------------------------------------------------------
# Public — list helpers (for registration API, engineer view)
# ---------------------------------------------------------------------------

def list_device_profiles() -> list[str]:
    """Return all model IDs in the device profiles file."""
    if _device_profiles_path is None:
        raise RuntimeError("Loader not configured — call loader.configure() first")
    raw = _read_json(_device_profiles_path)
    return [k for k in raw.get("devices", {}) if not k.startswith("_")]


def list_assets() -> list[str]:
    """Return all asset IDs in the asset registry."""
    if _asset_registry_path is None:
        raise RuntimeError("Loader not configured — call loader.configure() first")
    raw = _read_json(_asset_registry_path)
    return [k for k in raw.get("assets", {}) if not k.startswith("_")]


def update_asset_field(asset_id: str, field_path: list[str], value) -> None:
    """
    Update a single field in the asset registry JSON file in place.
    Used by engine.py to write commissioned_at on first run, and by
    the Blender export pipeline to write position_m.

    Args:
        asset_id:   Asset key in the registry
        field_path: List of nested keys, e.g. ["placement", "position_m"]
        value:      New value to write

    Example:
        update_asset_field("starkvind_01", ["commissioned_at"], "2026-04-25T...")
        update_asset_field("starkvind_01", ["placement", "position_m"], [1.2, 0.0, 3.4])
    """
    if _asset_registry_path is None:
        raise RuntimeError("Loader not configured — call loader.configure() first")

    raw = _read_json(_asset_registry_path)

    if asset_id not in raw.get("assets", {}):
        raise KeyError(f"Asset '{asset_id}' not found in registry")

    # Navigate to the parent of the target field
    target = raw["assets"][asset_id]
    for key in field_path[:-1]:
        target = target[key]
    target[field_path[-1]] = value

    with open(_asset_registry_path, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2)

    logger.info(f"Updated asset registry: {asset_id} {'.'.join(field_path)} = {value}")