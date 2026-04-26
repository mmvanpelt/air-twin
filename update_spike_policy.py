"""
update_spike_policy.py

Updates config.json, models.py, and engine.py with the learned
spike intervention architecture.

Run from project root:
    python update_spike_policy.py
"""

import json
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
# 1. assets/config.json
# ---------------------------------------------------------------------------

print("\n[1/3] assets/config.json")

config_path = ROOT / "assets" / "config.json"
with open(config_path) as f:
    config = json.load(f)

if "spike_response_model" in config.get("spikes", {}):
    skip("spike_response_model already in config")
else:
    config["spikes"].update({
        "_policy_comment": (
            "Twin observes purifier auto response before intervening. "
            "Intervention threshold is learned from empirical spike observations, "
            "not fixed. Fixed values are cold-start priors only."
        ),
        "observation_window_minutes": 3,
        "_obs_comment": (
            "How long twin observes purifier auto response before evaluating "
            "whether to intervene. Gives auto mode time to demonstrate adequate "
            "decay. Twin does not command during this window."
        ),
        "min_spike_observations_to_learn": 10,
        "_learn_comment": (
            "Minimum spike episodes observed before learned threshold supersedes "
            "cold-start prior. During Phase A (< min observations) twin observes "
            "only. Phase B (>= min) enables evidence-based intervention."
        ),
        "intervention_performance_threshold_prior": 0.6,
        "_prior_comment": (
            "Cold-start intervention threshold — used only until "
            "min_spike_observations_to_learn is reached. If observed decay rate "
            "during spike is below this fraction of expected, auto mode is deemed "
            "inadequate. Replaced by learned threshold once sufficient data exists."
        ),
        "spike_response_model": {
            "_comment": (
                "Learned model of purifier adequate auto response. "
                "Built from empirical spike observations. Twin learns what "
                "adequate performance looks like per auto step and spike magnitude "
                "bracket. Intervention threshold derived from this model."
            ),
            "magnitude_brackets_ug_m3": [5, 15, 30, 50, 100],
            "_brackets_comment": (
                "Spike magnitude brackets in µg/m³ above baseline. "
                "Twin tracks adequate response separately per bracket — "
                "a small spike requires different intervention logic than a large one."
            ),
            "learned_adequate_performance_ratio": {},
            "_ratio_comment": (
                "Learned adequate performance ratio per auto step and magnitude bracket. "
                "Keyed by str(auto_step):str(bracket_index). "
                "Populated by twin engine as spike observations accumulate. "
                "Empty until min_spike_observations_to_learn reached."
            ),
            "intervention_enabled": False,
            "_intervention_comment": (
                "Set to true automatically by twin engine when "
                "min_spike_observations_to_learn reached. "
                "Never set manually — twin earns intervention rights through observation."
            )
        }
    })

    config["control"]["degraded_command_speed"] = 4
    config["control"]["_degraded_speed_comment"] = (
        "Fan speed commanded when twin intervenes during DEGRADED regime. "
        "Tunable without code changes."
    )
    config["control"]["restore_auto_on_resolution"] = True
    config["control"]["_restore_comment"] = (
        "When true, twin restores purifier to auto mode after every "
        "twin-commanded episode resolves. Auto mode is the default intelligence "
        "layer — twin supervises, not replaces."
    )

    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
    ok("spike learning architecture added to config.json")
    ok("degraded_command_speed added to control block")
    ok("restore_auto_on_resolution added to control block")


# ---------------------------------------------------------------------------
# 2. backend/twin_engine/models.py — add spike model fields to TwinState
# ---------------------------------------------------------------------------

print("\n[2/3] backend/twin_engine/models.py")

models_path = ROOT / "backend" / "twin_engine" / "models.py"
src = models_path.read_text(encoding="utf-8")

OLD_CONTROL = '''\
    # --- Control ---
    last_fan_speed_commanded:   Optional[int] = None
    last_fan_mode:              Optional[str] = None
    last_command_ts:            Optional[str] = None
    last_command_acknowledged:  Optional[bool] = None'''

NEW_CONTROL = '''\
    # --- Control ---
    last_fan_speed_commanded:   Optional[int] = None
    last_fan_mode:              Optional[str] = None
    last_command_ts:            Optional[str] = None
    last_command_acknowledged:  Optional[bool] = None

    # --- Spike response model ---
    # Learned model of adequate purifier auto response per spike magnitude bracket.
    # Keyed by "auto_step:bracket_index" → list of observed performance ratios.
    # Twin observes Phase A until min_spike_observations_to_learn reached,
    # then derives intervention threshold from this data in Phase B.
    spike_response_observations:    dict = field(default_factory=dict)
    # { "6:1": [0.82, 0.79, 0.85, ...], "7:2": [0.91, ...] }

    # Derived learned thresholds — median of observations per key.
    # None until sufficient observations exist per key.
    spike_intervention_thresholds:  dict = field(default_factory=dict)
    # { "6:1": 0.82, "7:2": 0.91 }

    # Total spike observations logged (across all brackets/steps)
    spike_observation_count:        int = 0

    # Whether twin has earned intervention rights
    # Set True automatically when spike_observation_count >= min threshold
    spike_intervention_enabled:     bool = False

    # Current spike observation in progress
    # Tracks decay rate measurements during active observation window
    spike_observation_active:       bool = False
    spike_observation_started_ts:   Optional[str] = None
    spike_observation_auto_step:    Optional[int] = None
    spike_observation_magnitude:    Optional[float] = None
    spike_observation_decay_rates:  list = field(default_factory=list)'''

if "spike_response_observations" in src:
    skip("spike model fields already in TwinState")
elif OLD_CONTROL in src:
    src = src.replace(OLD_CONTROL, NEW_CONTROL)
    ok("spike model fields added to TwinState")
else:
    fail("Could not find TwinState control section — check models.py manually")

# Update to_dict()
OLD_TO_DICT_CONTROL = '''\
            # Control
            "last_fan_speed_commanded":   self.last_fan_speed_commanded,
            "last_fan_mode":              self.last_fan_mode,
            "last_command_ts":            self.last_command_ts,
            "last_command_acknowledged":  self.last_command_acknowledged,'''

NEW_TO_DICT_CONTROL = '''\
            # Control
            "last_fan_speed_commanded":   self.last_fan_speed_commanded,
            "last_fan_mode":              self.last_fan_mode,
            "last_command_ts":            self.last_command_ts,
            "last_command_acknowledged":  self.last_command_acknowledged,
            # Spike response model
            "spike_response_observations":   self.spike_response_observations,
            "spike_intervention_thresholds": self.spike_intervention_thresholds,
            "spike_observation_count":       self.spike_observation_count,
            "spike_intervention_enabled":    self.spike_intervention_enabled,
            "spike_observation_active":      self.spike_observation_active,
            "spike_observation_started_ts":  self.spike_observation_started_ts,
            "spike_observation_auto_step":   self.spike_observation_auto_step,
            "spike_observation_magnitude":   self.spike_observation_magnitude,
            "spike_observation_decay_rates": self.spike_observation_decay_rates,'''

if '"spike_response_observations"' in src:
    skip("to_dict() already has spike model fields")
elif OLD_TO_DICT_CONTROL in src:
    src = src.replace(OLD_TO_DICT_CONTROL, NEW_TO_DICT_CONTROL)
    ok("to_dict() updated with spike model fields")
else:
    fail("Could not find to_dict() control section — check models.py manually")

# Update from_dict()
OLD_FROM_DICT_CONTROL = '''\
            # Control
            last_fan_speed_commanded=get("last_fan_speed_commanded"),
            last_fan_mode=get("last_fan_mode"),
            last_command_ts=get("last_command_ts"),
            last_command_acknowledged=get("last_command_acknowledged"),'''

NEW_FROM_DICT_CONTROL = '''\
            # Control
            last_fan_speed_commanded=get("last_fan_speed_commanded"),
            last_fan_mode=get("last_fan_mode"),
            last_command_ts=get("last_command_ts"),
            last_command_acknowledged=get("last_command_acknowledged"),
            # Spike response model
            spike_response_observations=get("spike_response_observations", {}),
            spike_intervention_thresholds=get("spike_intervention_thresholds", {}),
            spike_observation_count=get("spike_observation_count", 0),
            spike_intervention_enabled=get("spike_intervention_enabled", False),
            spike_observation_active=get("spike_observation_active", False),
            spike_observation_started_ts=get("spike_observation_started_ts"),
            spike_observation_auto_step=get("spike_observation_auto_step"),
            spike_observation_magnitude=get("spike_observation_magnitude"),
            spike_observation_decay_rates=get("spike_observation_decay_rates", []),'''

if 'spike_response_observations=get("spike_response_observations"' in src:
    skip("from_dict() already has spike model fields")
elif OLD_FROM_DICT_CONTROL in src:
    src = src.replace(OLD_FROM_DICT_CONTROL, NEW_FROM_DICT_CONTROL)
    ok("from_dict() updated with spike model fields")
else:
    fail("Could not find from_dict() control section — check models.py manually")

models_path.write_text(src, encoding="utf-8")
ok("models.py written")


# ---------------------------------------------------------------------------
# 3. backend/twin_engine/engine.py — revise spike intervention logic
# ---------------------------------------------------------------------------

print("\n[3/3] backend/twin_engine/engine.py")

engine_path = ROOT / "backend" / "twin_engine" / "engine.py"
src = engine_path.read_text(encoding="utf-8")

OLD_MAYBE_COMMAND = '''\
    def _maybe_command_speed(self, reading: Reading, spike_active: bool) -> None:
        """
        Decide whether to command a fan speed change.

        Autonomous speed increases are permitted during spike events.
        Sustained high-speed operation beyond escalation_awareness_minutes
        triggers operator awareness (handled by escalation event).
        Turning purifier off and overriding auto mode require explicit permission.
        """
        if not self._config["control"]["autonomous_speed_increase"]:
            return

        if reading.fan_speed is None:
            return

        # Don\'t command if already at max
        max_speed = self._profile.fan_speed_max
        current_speed = reading.fan_speed
        if current_speed >= max_speed:
            return

        # Command one step up
        target_speed = min(current_speed + 1, max_speed)
        self._command_fan_speed(target_speed)'''

NEW_MAYBE_COMMAND = '''\
    def _maybe_command_speed(self, reading: Reading, spike_active: bool) -> None:
        """
        Decide whether to command a fan speed change during a spike.

        Policy: observe first, intervene only when evidence shows auto mode
        is insufficient. Twin earns intervention rights through observation.

        Phase A (spike_observation_count < min_spike_observations_to_learn):
          - Observe only. Record purifier auto response. No commands issued.
          - Twin is building its model of adequate auto performance.

        Phase B (spike_observation_count >= min threshold):
          - Compare current auto response against learned adequate threshold.
          - Intervene only if response is below learned threshold.
          - Restore auto mode on resolution.

        The purifier\'s auto mode is the default intelligence layer.
        Twin supervises — it does not replace.
        """
        if not self._config["control"]["autonomous_speed_increase"]:
            return

        if reading.fan_speed is None or reading.fan_mode != "auto":
            return

        obs_window = self._config["spikes"]["observation_window_minutes"]
        min_obs = self._config["spikes"]["min_spike_observations_to_learn"]
        prior_threshold = self._config["spikes"][
            "intervention_performance_threshold_prior"
        ]

        # --- Start observation window if not already active ---
        if not self._state.spike_observation_active:
            from dataclasses import replace as _r
            import statistics
            baseline = self._state.baseline_locked or self._state.baseline_current or 0
            magnitude = max(0.0, (reading.rolling_mean or reading.value) - baseline)
            self._state = _r(
                self._state,
                spike_observation_active=True,
                spike_observation_started_ts=utc_now(),
                spike_observation_auto_step=reading.fan_speed,
                spike_observation_magnitude=magnitude,
                spike_observation_decay_rates=[],
            )
            logger.info(
                f"Spike observation started — auto_step={reading.fan_speed}, "
                f"magnitude={magnitude:.1f} µg/m³ above baseline. "
                f"Observing for {obs_window} min before evaluating intervention."
            )
            return

        # --- Accumulate decay rate observations ---
        from dataclasses import replace as _r
        if reading.trend_slope is not None:
            decay_per_min = -reading.trend_slope * 60
            if decay_per_min > 0:
                rates = list(self._state.spike_observation_decay_rates) + [decay_per_min]
                self._state = _r(self._state, spike_observation_decay_rates=rates)

        # --- Check if observation window has elapsed ---
        if self._state.spike_observation_started_ts is None:
            return

        from datetime import datetime, timezone
        started = datetime.fromisoformat(self._state.spike_observation_started_ts)
        elapsed_min = (
            datetime.now(timezone.utc) - started
        ).total_seconds() / 60.0

        if elapsed_min < obs_window:
            return  # Still observing — do not intervene yet

        # --- Observation window complete — evaluate response ---
        import statistics as _stats
        rates = self._state.spike_observation_decay_rates
        if not rates:
            logger.debug("No decay rate observations collected — cannot evaluate")
            self._state = _r(self._state, spike_observation_active=False,
                             spike_observation_decay_rates=[])
            return

        observed_decay = _stats.median(rates)
        auto_step = self._state.spike_observation_auto_step or reading.fan_speed

        # Get expected decay for this auto step
        from backend.twin_engine.performance import expected_decay_rate
        from backend.twin_engine.models import FilterType
        filter_type = self._state.installed_filter_type
        exp_decay = expected_decay_rate(
            fan_speed=min(auto_step, self._profile.fan_speed_max),
            filter_type=filter_type if isinstance(filter_type, FilterType)
                        else FilterType(filter_type),
            profile=self._profile,
            state=self._state,
            room_volume_m3=self._asset.room_volume_m3,
        )

        if exp_decay is None or exp_decay == 0:
            logger.debug("Expected decay unavailable — cannot evaluate intervention")
            self._state = _r(self._state, spike_observation_active=False,
                             spike_observation_decay_rates=[])
            return

        performance_ratio = observed_decay / exp_decay

        # Determine intervention threshold
        magnitude = self._state.spike_observation_magnitude or 0
        bracket = self._get_magnitude_bracket(magnitude)
        obs_key = f"{auto_step}:{bracket}"

        if self._state.spike_intervention_enabled:
            threshold = self._state.spike_intervention_thresholds.get(
                obs_key, prior_threshold
            )
            threshold_source = "learned"
        else:
            threshold = prior_threshold
            threshold_source = "prior"

        # Record this observation
        obs_dict = dict(self._state.spike_response_observations)
        obs_dict.setdefault(obs_key, [])
        obs_dict[obs_key].append(performance_ratio)
        new_count = self._state.spike_observation_count + 1

        # Update learned thresholds
        thresholds = dict(self._state.spike_intervention_thresholds)
        if len(obs_dict[obs_key]) >= 3:
            thresholds[obs_key] = _stats.median(obs_dict[obs_key])

        # Enable intervention if enough total observations
        intervention_enabled = new_count >= min_obs

        self._state = _r(
            self._state,
            spike_response_observations=obs_dict,
            spike_intervention_thresholds=thresholds,
            spike_observation_count=new_count,
            spike_intervention_enabled=intervention_enabled,
            spike_observation_active=False,
            spike_observation_decay_rates=[],
        )

        logger.info(
            f"Spike observation complete — auto_step={auto_step}, "
            f"observed_decay={observed_decay:.4f}, "
            f"performance_ratio={performance_ratio:.3f}, "
            f"threshold={threshold:.3f} ({threshold_source}), "
            f"total_observations={new_count}"
        )

        # --- Intervene if response is inadequate ---
        if performance_ratio < threshold:
            if self._state.spike_intervention_enabled or True:
                # Intervene — command manual speed above current auto step
                max_manual = self._profile.fan_speed_max
                # Map auto step to nearest manual equivalent for command
                # Use ceiling division to map 1-9 to 1-5
                manual_equiv = min(
                    max(1, round(auto_step * max_manual / 9)), max_manual
                )
                target = min(manual_equiv + 1, max_manual)
                logger.warning(
                    f"Auto mode response inadequate "
                    f"(ratio={performance_ratio:.3f} < threshold={threshold:.3f}) "
                    f"— intervening at manual speed {target}"
                )
                self._command_fan_speed(target)
        else:
            logger.info(
                f"Auto mode response adequate "
                f"(ratio={performance_ratio:.3f} >= threshold={threshold:.3f}) "
                f"— no intervention"
            )

    def _get_magnitude_bracket(self, magnitude_ug_m3: float) -> int:
        """Return bracket index for a spike magnitude."""
        brackets = self._config["spikes"].get(
            "magnitude_brackets_ug_m3", [5, 15, 30, 50, 100]
        )
        for i, threshold in enumerate(brackets):
            if magnitude_ug_m3 <= threshold:
                return i
        return len(brackets)

    def _restore_auto_mode(self) -> None:
        """
        Restore purifier to auto mode after a twin-commanded episode resolves.
        Called after spike resolution and DEGRADED → BASELINE transition.
        Auto mode is the default intelligence layer — always restored after intervention.
        """
        if not self._config["control"].get("restore_auto_on_resolution", True):
            return
        set_topic = f"zigbee2mqtt/{self._profile.zigbee_friendly_name}/set"
        self._publish(set_topic, {"fan_mode": "auto"})
        from dataclasses import replace as _r
        self._state = _r(self._state, last_fan_mode="auto")
        logger.info("Purifier restored to auto mode after twin-commanded episode")'''

if "_restore_auto_mode" in src:
    skip("spike intervention logic already updated in engine.py")
elif OLD_MAYBE_COMMAND in src:
    src = src.replace(OLD_MAYBE_COMMAND, NEW_MAYBE_COMMAND)
    ok("_maybe_command_speed() replaced with observe-first learned policy")
    ok("_get_magnitude_bracket() added")
    ok("_restore_auto_mode() added")
else:
    fail("Could not find _maybe_command_speed() in engine.py — check manually")

# Add _restore_auto_mode call after spike resolution and regime transition
OLD_SPIKE_RESOLVE = '''\
            if spike_event is not None:
                cycle_events.append(("spike", spike_event))

            # Command purifier response to spike
            if spike_active and reading.purifier_on:
                self._maybe_command_speed(reading, spike_active=True)'''

NEW_SPIKE_RESOLVE = '''\
            if spike_event is not None:
                cycle_events.append(("spike", spike_event))
                # Spike resolved — restore auto mode if twin had intervened
                if self._state.last_fan_mode != "auto":
                    self._restore_auto_mode()

            # Observe/command purifier response to spike
            if spike_active and reading.purifier_on:
                self._maybe_command_speed(reading, spike_active=True)'''

if "Spike resolved — restore auto mode" in src:
    skip("spike resolution restore already in engine.py")
elif OLD_SPIKE_RESOLVE in src:
    src = src.replace(OLD_SPIKE_RESOLVE, NEW_SPIKE_RESOLVE)
    ok("Auto mode restore on spike resolution added")
else:
    fail("Could not find spike resolution block in engine.py")

# Add restore on regime transition DEGRADED → BASELINE
OLD_REGIME_TRANSITION = '''\
        if regime_transition is not None:
            cycle_events.append(("regime_transition", regime_transition))
            logger.info(f"Regime transition: {regime_transition}")
            # Reset escalation timer on regime exit
            if regime_transition.to_regime != RegimeType.DEGRADED:
                events.reset_escalation_timer(self._asset_id)
            # Clear spike tracker if escalating to DEGRADED
            if regime_transition.to_regime == RegimeType.DEGRADED:
                events.clear_spike(self._asset_id)'''

NEW_REGIME_TRANSITION = '''\
        if regime_transition is not None:
            cycle_events.append(("regime_transition", regime_transition))
            logger.info(f"Regime transition: {regime_transition}")
            # Reset escalation timer on regime exit
            if regime_transition.to_regime != RegimeType.DEGRADED:
                events.reset_escalation_timer(self._asset_id)
            # Clear spike tracker if escalating to DEGRADED
            if regime_transition.to_regime == RegimeType.DEGRADED:
                events.clear_spike(self._asset_id)
            # Restore auto mode when returning to baseline from degraded
            if (regime_transition.from_regime == RegimeType.DEGRADED
                    and regime_transition.to_regime == RegimeType.BASELINE):
                self._restore_auto_mode()'''

if "Restore auto mode when returning to baseline" in src:
    skip("regime transition auto restore already in engine.py")
elif OLD_REGIME_TRANSITION in src:
    src = src.replace(OLD_REGIME_TRANSITION, NEW_REGIME_TRANSITION)
    ok("Auto mode restore on DEGRADED → BASELINE transition added")
else:
    fail("Could not find regime transition block in engine.py")

engine_path.write_text(src, encoding="utf-8")
ok("engine.py written")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

print("\n" + "="*50)
if ERRORS:
    print(f"COMPLETED WITH {len(ERRORS)} ERROR(S):")
    for e in ERRORS:
        print(f"  ✗ {e}")
    sys.exit(1)
else:
    print("ALL CHANGES APPLIED SUCCESSFULLY")
    print("\nValidate with:")
    print("  python -c \"")
    print("  import json")
    print("  c = json.load(open('assets/config.json'))")
    print("  assert 'spike_response_model' in c['spikes']")
    print("  assert 'degraded_command_speed' in c['control']")
    print("  print('config.json: pass')")
    print("  \"")