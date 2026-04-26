"""
engine.py — Main twin engine for the Air Twin digital twin.

This is the sole I/O boundary for the twin engine package. It wires all
modules together into a single processing cycle, owns all file I/O and
database persistence, and exposes current twin state for FastAPI.

Architecture:
  - All other modules are pure functions. They receive TwinState and return
    updated TwinState. They never touch files or databases directly.
  - engine.py calls them in order, collects results, persists everything.
  - MQTT publish is injected as a callback from main.py — engine has no
    direct MQTT knowledge and is testable without a live broker.

Processing cycle order (per reading):
  1. filter.update()
  2. baseline.update()
  3. performance.on_purifier_active() / on_purifier_inactive()
  4. confidence.update()
  5. regime.evaluate()
  6. events.on_baseline_reading() / on_degraded_reading()
  7. events.check_uncommanded_state_change()
  8. Persist to database
  9. Write twin_state.json
  10. Notify FastAPI via state_callback
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from backend.twin_engine import baseline, confidence, events, filter as filter_mod
from backend.twin_engine import loader, performance, regime
from backend.twin_engine.models import (
    ControlSource,
    FilterType,
    Reading,
    RegimeType,
    TwinState,
    utc_now,
)

logger = logging.getLogger(__name__)


class TwinEngine:
    """
    Main twin engine. Instantiated once by main.py on startup.

    Args:
        config_path:      Path to assets/config.json
        publish_callback: fn(topic: str, payload: dict) — injected by main.py
        db_persist:       fn(reading, state, events) — injected by main.py
        state_callback:   async fn(state_dict) — called after each cycle for WebSocket
    """

    def __init__(
        self,
        config_path: str,
        publish_callback: Callable[[str, dict], None],
        db_persist: Callable,
        state_callback: Optional[Callable] = None,
    ):
        self._publish = publish_callback
        self._db_persist = db_persist
        self._state_callback = state_callback

        # Load config
        self._config = self._load_config(config_path)

        # Configure loader
        loader.configure(
            self._config["paths"]["device_profiles"],
            self._config["paths"]["asset_registry"],
        )

        # Load asset and device profile
        # For now single-asset — multi-asset support via asset_id loop in future
        self._asset_id = "starkvind_01"
        self._asset = loader.get_asset(self._asset_id)
        self._profile = loader.get_device_profile(self._asset.device_profile_id)

        logger.info(
            f"Twin engine initialised — asset={self._asset_id}, "
            f"profile={self._profile.model_id}, "
            f"room={self._asset.room_volume_m3}m³"
        )

        # Load or create TwinState
        self._state = self._load_state()

        # Cold start detection
        self._handle_cold_start()

        # Pending command tracking
        self._pending_command_ts: Optional[float] = None

        # Last reading timestamp for gap calculation
        self._last_reading_epoch: Optional[float] = None

    # ---------------------------------------------------------------------------
    # Public — main entry point called by mqtt_subscriber on each reading
    # ---------------------------------------------------------------------------

    def process_reading(self, reading: Reading) -> None:
        """
        Process one incoming reading through the full twin engine cycle.
        Called by mqtt_subscriber.py on every MQTT message merge.
        Thread-safe — all state updates are synchronous and sequential.
        """
        now_epoch = time.time()
        seconds_since_last = (
            now_epoch - self._last_reading_epoch
            if self._last_reading_epoch is not None
            else 0.0
        )
        self._last_reading_epoch = now_epoch

        # Commission on first valid reading
        if self._state.commissioned_at is None and reading.is_plausible is not False:
            from dataclasses import replace as _dc_replace
            commissioned_ts = utc_now()
            self._state = _dc_replace(self._state, commissioned_at=commissioned_ts)
            loader.update_asset_field(
                self._asset_id, ["commissioned_at"], commissioned_ts
            )
            logger.info(f"Asset commissioned at {commissioned_ts}")

        # Collect all events this cycle
        cycle_events = []
        cycle_observation = None

        # --- Step 1: Filter ---
        self._state, filter_signals, filter_alerts = filter_mod.update(
            state=self._state,
            reading=reading,
            asset_id=self._asset_id,
            unlogged_drop_threshold_minutes=self._config["filter"][
                "unlogged_change_detection_threshold_minutes"
            ],
        )
        if filter_alerts:
            for alert in filter_alerts:
                logger.warning(f"[filter] {alert}")

        # --- Step 2: Baseline ---
        if regime.regime_allows_baseline_learning(self._state):
            self._state, baseline_signals = baseline.update(
                state=self._state,
                reading=reading,
                asset_id=self._asset_id,
                min_readings_to_lock=self._config["baseline"]["min_readings_to_lock"],
                lock_variance_threshold_std=self._config["baseline"][
                    "lock_variance_threshold_std"
                ],
                ema_alpha=self._config["baseline"]["baseline_ema_alpha"],
                rate_of_change_guard_ug_m3_per_hour=self._config["baseline"][
                    "rate_of_change_guard_ug_m3_per_hour"
                ],
            )
        else:
            baseline_signals = []
            # Still update baseline_current EMA in operational regimes
            if self._state.baseline_locked is not None:
                self._state, _ = baseline.update(
                    state=self._state,
                    reading=reading,
                    asset_id=self._asset_id,
                    min_readings_to_lock=999999,  # lock never triggered
                    lock_variance_threshold_std=self._config["baseline"][
                        "lock_variance_threshold_std"
                    ],
                    ema_alpha=self._config["baseline"]["baseline_ema_alpha"],
                    rate_of_change_guard_ug_m3_per_hour=self._config["baseline"][
                        "rate_of_change_guard_ug_m3_per_hour"
                    ],
                )

        # --- Step 3: Performance ---
        twin_age_hours, age_source = filter_mod.best_filter_age_hours(
            self._state,
            reading.device_age or 0,
            reading.filter_age,
        )

        if reading.purifier_on:
            self._state, cycle_observation, perf_signals = (
                performance.on_purifier_active(
                    state=self._state,
                    reading=reading,
                    asset_id=self._asset_id,
                    profile=self._profile,
                    room_volume_m3=self._asset.room_volume_m3,
                    twin_filter_age_hours=twin_age_hours,
                    min_event_duration_minutes=self._config["performance"][
                        "min_event_duration_minutes"
                    ],
                )
            )
        else:
            self._state, cycle_observation, perf_signals = (
                performance.on_purifier_inactive(
                    state=self._state,
                    reading=reading,
                    asset_id=self._asset_id,
                    profile=self._profile,
                    room_volume_m3=self._asset.room_volume_m3,
                    min_event_duration_minutes=self._config["performance"][
                        "min_event_duration_minutes"
                    ],
                )
            )

        # --- Step 4: Confidence ---
        deviation = baseline.deviation_from_locked(self._state, reading.rolling_mean)
        season_mismatch = baseline.season_mismatch(self._state)
        reading_gap_minutes = seconds_since_last / 60.0

        self._state = confidence.update(
            state=self._state,
            reading=reading,
            signals=perf_signals,
            baseline_signals=baseline_signals,
            filter_signals=filter_signals,
            reading_gap_minutes=reading_gap_minutes,
            sensor_cross_reference_threshold=self._config["confidence"][
                "sensor_cross_reference_threshold_ug_m3"
            ],
            evidence_weights=self._config["confidence"]["evidence_weights"],
            smoothing_alpha=self._config["confidence"]["smoothing_alpha"],
            season_mismatch=season_mismatch,
        )

        # --- Step 5: Regime ---
        self._state, regime_transition = regime.evaluate(
            state=self._state,
            reading=reading,
            asset_id=self._asset_id,
            baseline_locked=self._state.baseline_locked,
            baseline_std=self._state.baseline_std,
            deviation_from_locked=deviation,
            gap_threshold_s=self._config["regimes"]["gap_threshold_s"],
            degraded_entry_std_multiplier=self._config["regimes"][
                "degraded_entry_std_multiplier"
            ],
            degraded_entry_duration_minutes=self._config["regimes"][
                "degraded_entry_duration_minutes"
            ],
            degraded_exit_duration_minutes=self._config["regimes"][
                "degraded_exit_duration_minutes"
            ],
            seconds_since_last_reading=seconds_since_last,
        )

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
                self._restore_auto_mode()

        # --- Step 6: Spike / escalation events ---
        if self._state.current_regime == RegimeType.BASELINE:
            spike_event, spike_active = events.on_baseline_reading(
                state=self._state,
                reading=reading,
                asset_id=self._asset_id,
                deviation_from_locked=deviation,
                spike_entry_std_multiplier=self._config["regimes"][
                    "degraded_entry_std_multiplier"
                ],
                spike_resolution_window_minutes=self._config["spikes"][
                    "resolution_window_minutes"
                ],
            )
            if spike_event is not None:
                cycle_events.append(("spike", spike_event))
                # Spike resolved — restore auto mode if twin had intervened
                if self._state.last_fan_mode != "auto":
                    self._restore_auto_mode()

            # Observe/command purifier response to spike
            if spike_active and reading.purifier_on:
                self._maybe_command_speed(reading, spike_active=True)

        elif self._state.current_regime == RegimeType.DEGRADED:
            escalation_event = events.on_degraded_reading(
                state=self._state,
                reading=reading,
                asset_id=self._asset_id,
                escalation_awareness_minutes=self._config["control"][
                    "escalation_awareness_minutes"
                ],
            )
            if escalation_event is not None:
                cycle_events.append(("escalation", escalation_event))
                logger.warning(
                    f"Escalation event — DEGRADED for "
                    f"{escalation_event.duration_minutes:.1f} min"
                )

        # --- Step 7: Uncommanded state change detection ---
        uncommanded = events.check_uncommanded_state_change(
            reading=reading,
            asset_id=self._asset_id,
            state=self._state,
        )
        if uncommanded is not None:
            cycle_events.append(("uncommanded_state_change", uncommanded))

        # --- Step 8: Command acknowledgement check ---
        if self._pending_command_ts is not None:
            ack = events.check_acknowledgement(
                reading=reading,
                asset_id=self._asset_id,
                ack_timeout_s=self._profile.command_acknowledgement_timeout_s,
                current_ts_epoch=now_epoch,
                command_ts_epoch=self._pending_command_ts,
            )
            if ack is not None:
                if not ack.acknowledged and not ack.retry_attempted:
                    # Retry once
                    self._command_fan_speed(
                        self._state.last_fan_speed_commanded, retry=True
                    )
                elif not ack.acknowledged and ack.retry_attempted:
                    logger.error(
                        f"Purifier unresponsive after retry — "
                        f"commanded speed {self._state.last_fan_speed_commanded}, "
                        f"reported {reading.fan_speed}"
                    )
                    self._pending_command_ts = None
                elif ack.acknowledged:
                    self._pending_command_ts = None

        # --- Step 9: Persist to database ---
        try:
            self._db_persist(
                reading=reading,
                state=self._state,
                cycle_events=cycle_events,
                observation=cycle_observation,
                filter_alerts=filter_alerts,
            )
        except Exception as e:
            logger.error(f"Database persistence error: {e}")

        # --- Step 10: Write twin_state.json ---
        self._save_state()

        # --- Step 11: Notify FastAPI ---
        if self._state_callback is not None:
            try:
                asyncio.get_event_loop().call_soon_threadsafe(
                    asyncio.ensure_future,
                    self._state_callback(self._public_state(reading)),
                )
            except Exception as e:
                logger.debug(f"State callback error (non-fatal): {e}")

    # ---------------------------------------------------------------------------
    # Public — maintenance events (called by FastAPI routes in main.py)
    # ---------------------------------------------------------------------------

    def on_filter_change(
        self,
        device_age_minutes: int,
        filter_type: str,
        actor: str,
    ) -> None:
        """
        Handle a QR-logged filter change maintenance event.
        Called by FastAPI POST /maintenance route.
        """
        self._state = filter_mod.on_qr_filter_change(
            state=self._state,
            device_age_minutes=device_age_minutes,
            filter_type=filter_type,
            asset_id=self._asset_id,
        )
        self._state = baseline.reset_for_maintenance(
            state=self._state,
            asset_id=self._asset_id,
        )
        self._state, transition = regime.enter_validating(
            state=self._state,
            asset_id=self._asset_id,
            reason=f"Filter change logged by {actor} — baseline re-learn required",
        )
        self._save_state()
        logger.info(
            f"Filter change processed — actor={actor}, "
            f"type={filter_type}, device_age={device_age_minutes}min"
        )

    def on_technician_reset(self, device_age_minutes: int, actor: str) -> None:
        """
        Handle a technician-initiated baseline reset.
        Called by FastAPI POST /maintenance/reset route.
        """
        self._state = filter_mod.on_technician_reset(
            state=self._state,
            device_age_minutes=device_age_minutes,
            asset_id=self._asset_id,
        )
        self._state = baseline.reset_for_maintenance(
            state=self._state,
            asset_id=self._asset_id,
        )
        self._state, _ = regime.enter_initialising(
            state=self._state,
            asset_id=self._asset_id,
            reason=f"Technician reset by {actor}",
        )
        self._save_state()
        logger.info(f"Technician reset processed — actor={actor}")

    # ---------------------------------------------------------------------------
    # Public — state exposure for FastAPI
    # ---------------------------------------------------------------------------

    def get_state(self) -> dict:
        """Return current twin state dict for API endpoints."""
        return self._state.to_dict()

    def get_regime_summary(self) -> dict:
        """Return regime summary for engineer view."""
        return regime.regime_summary(self._state)

    def get_confidence_factors(self) -> list:
        """Return all confidence factors sorted by delta for engineer view."""
        return confidence.all_active_factors(self._state)

    def get_confidence_conclusion(self) -> str:
        """Return tiered conclusion string for executive brief."""
        return confidence.confidence_conclusion(self._state.confidence)

    def get_dominant_negative_factor(self):
        """Return dominant negative confidence factor for engineer view."""
        return confidence.dominant_negative_factor(self._state)

    def get_filter_status(self, reading: Optional[Reading] = None) -> dict:
        """Return filter status summary."""
        filter_profile = self._profile.filter_types.get(
            self._state.installed_filter_type.value
            if hasattr(self._state.installed_filter_type, "value")
            else str(self._state.installed_filter_type),
            None,
        )
        filter_life_hours = filter_profile.filter_life_hours if filter_profile else 4380

        device_age = 0
        filter_age = None
        if reading is not None:
            device_age = reading.device_age or 0
            filter_age = reading.filter_age

        age_hours, source = filter_mod.best_filter_age_hours(
            self._state, device_age, filter_age
        )
        fraction, _ = filter_mod.filter_life_fraction(
            self._state, device_age, filter_age, filter_life_hours
        )

        return {
            "twin_filter_age_hours": age_hours,
            "filter_age_source": source,
            "filter_life_hours": filter_life_hours,
            "filter_life_fraction": fraction,
            "filter_life_percent": round(fraction * 100, 1),
            "installed_filter_type": str(self._state.installed_filter_type),
            "no_anchor_on_record": filter_mod.no_anchor_on_record(self._state),
            "filter_change_pending_reset": self._state.filter_change_pending_reset,
        }

    # ---------------------------------------------------------------------------
    # Internal — MQTT command publishing
    # ---------------------------------------------------------------------------

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

        The purifier's auto mode is the default intelligence layer.
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
        logger.info("Purifier restored to auto mode after twin-commanded episode")

    def _command_fan_speed(self, speed: int, retry: bool = False) -> None:
        """
        Publish a fan speed command to the purifier.

        If purifier is in auto mode, switches to manual first,
        commands the speed, then schedules auto mode restoration.
        Mode change is logged but does not require explicit permission.
        """
        if speed not in self._profile.fan_speeds_valid:
            logger.warning(f"Invalid fan speed commanded: {speed} — ignoring")
            return

        topic_set = self._profile.cadr  # use zigbee set topic from profile
        # Get set topic from config — Zigbee control topic
        set_topic = f"zigbee2mqtt/{self._profile.zigbee_friendly_name}/set"

        # Switch to manual if in auto mode
        if self._state.last_fan_mode == "auto":
            self._publish(set_topic, {"fan_mode": "manual"})
            logger.info(f"Switched purifier to manual mode for speed command")

        # Command the speed
        self._publish(set_topic, {"fan_speed": speed})

        # Update state
        from dataclasses import replace
        self._state = replace(
            self._state,
            last_fan_speed_commanded=speed,
            last_command_ts=utc_now(),
            last_command_acknowledged=False,
        )

        # Register for acknowledgement tracking
        self._pending_command_ts = time.time()
        events.register_command(self._asset_id, speed, utc_now())

        action = "retry" if retry else "command"
        logger.info(f"Fan speed {action}: {speed}")

    # ---------------------------------------------------------------------------
    # Internal — startup helpers
    # ---------------------------------------------------------------------------

    def _load_config(self, config_path: str) -> dict:
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Config not found: {config_path}")
        with open(path) as f:
            return json.load(f)

    def _load_state(self) -> TwinState:
        """Load TwinState from twin_state.json or create fresh on first run."""
        state_path = Path(self._config["paths"]["twin_state"])
        if state_path.exists():
            try:
                with open(state_path) as f:
                    data = json.load(f)
                state = TwinState.from_dict(data)
                logger.info(
                    f"Loaded twin state — regime={state.current_regime}, "
                    f"confidence={state.confidence:.3f}, "
                    f"baseline_locked={state.baseline_locked}"
                )
                return state
            except Exception as e:
                logger.error(
                    f"Failed to load twin_state.json ({e}) — starting fresh"
                )
        logger.info("No twin_state.json found — starting fresh")
        return TwinState()

    def _handle_cold_start(self) -> None:
        """
        Detect if downtime since last reading exceeds gap threshold.
        If so, enter UNKNOWN before accepting new readings.
        Does not resume baseline from stale state.
        """
        if self._state.last_reading_ts is None:
            return

        try:
            last_ts = datetime.fromisoformat(self._state.last_reading_ts)
            now = datetime.now(timezone.utc)
            downtime_s = (now - last_ts).total_seconds()
            gap_threshold = self._config["regimes"]["gap_threshold_s"]

            if downtime_s > gap_threshold:
                logger.warning(
                    f"Cold start — downtime={downtime_s:.0f}s > "
                    f"gap_threshold={gap_threshold}s — entering UNKNOWN"
                )
                self._state, _ = regime.enter_initialising(
                    state=self._state,
                    asset_id=self._asset_id,
                    reason=(
                        f"Cold start after {downtime_s:.0f}s downtime — "
                        f"baseline not resumed from stale state"
                    ),
                )
            else:
                logger.info(
                    f"Warm start — downtime={downtime_s:.0f}s within threshold"
                )
        except Exception as e:
            logger.warning(f"Cold start check failed ({e}) — continuing")

    def _save_state(self) -> None:
        """Write TwinState to twin_state.json atomically."""
        state_path = Path(self._config["paths"]["twin_state"])
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = state_path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w") as f:
                json.dump(self._state.to_dict(), f, indent=2)
            tmp_path.replace(state_path)
        except Exception as e:
            logger.error(f"Failed to write twin_state.json: {e}")
            if tmp_path.exists():
                tmp_path.unlink()

    def _public_state(self, reading: Reading) -> dict:
        """
        Build the state dict published to FastAPI WebSocket each cycle.
        Combines TwinState with current reading values and derived fields.
        """
        return {
            "ts": reading.ts,
            "pm25": reading.value,
            "pm25_internal": reading.pm25_internal,
            "regime": self._state.current_regime,
            "confidence": self._state.confidence,
            "confidence_conclusion": confidence.confidence_conclusion(
                self._state.confidence
            ),
            "baseline_locked": self._state.baseline_locked,
            "baseline_current": self._state.baseline_current,
            "fan_speed": reading.fan_speed,
            "fan_mode": reading.fan_mode,
            "purifier_on": reading.purifier_on,
            "filter_status": self.get_filter_status(reading),
            "confidence_factors": confidence.all_active_factors(self._state),
            "regime_summary": regime.regime_summary(self._state),
            "room_efficiency_factor": self._state.room_efficiency_factor,
            "commissioned_at": self._state.commissioned_at,
        }