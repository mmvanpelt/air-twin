"""
record_demo.py — Interactive demo recording tool for Air Twin.
v2 — enriches frames with regime, confidence, baseline from state_transitions.
"""

import json
import sqlite3
import sys
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "data" / "airtwin.db"
OUTPUT_PATH = ROOT / "frontend" / "demo" / "recording.json"

class Colors:
    BOLD = '\033[1m'; GREEN = '\033[92m'; YELLOW = '\033[93m'
    CYAN = '\033[96m'; RED = '\033[91m'; END = '\033[0m'

def bold(s): return f"{Colors.BOLD}{s}{Colors.END}"
def green(s): return f"{Colors.GREEN}{s}{Colors.END}"
def yellow(s): return f"{Colors.YELLOW}{s}{Colors.END}"
def cyan(s): return f"{Colors.CYAN}{s}{Colors.END}"
def red(s): return f"{Colors.RED}{s}{Colors.END}"

def prompt(msg, default=None):
    display = f"{msg} [{default}]: " if default is not None else f"{msg}: "
    try:
        val = input(display).strip()
        return val if val else (str(default) if default is not None else "")
    except (KeyboardInterrupt, EOFError):
        print("\nAborted."); sys.exit(0)

def confirm(msg, default=True):
    hint = "[Y/n]" if default else "[y/N]"
    val = prompt(f"{msg} {hint}").lower()
    if not val: return default
    return val in ('y', 'yes')

def bar(value, max_value, width=30, char='█', empty='░'):
    filled = int(width * value / max_value) if max_value > 0 else 0
    return char * filled + empty * (width - filled)

def get_conn():
    if not DB_PATH.exists():
        print(red(f"Database not found: {DB_PATH}")); sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ---------------------------------------------------------------------------
# NEW: Enrich frames with twin state
# ---------------------------------------------------------------------------

def fetch_twin_state_history(conn) -> list[dict]:
    """
    Fetch all state transitions to build a timeline of twin state.
    Returns list of transitions sorted by timestamp.
    """
    rows = conn.execute("""
        SELECT ts, from_regime, to_regime, reason
        FROM state_transitions
        ORDER BY ts ASC
    """).fetchall()
    return [dict(r) for r in rows]


def _regime_confidence(regime: str) -> float:
    """Approximate confidence for a regime — used for demo playback only."""
    defaults = {
        'initialising': 0.52,
        'validating':   0.55,
        'baseline':     0.87,
        'event':        0.75,
        'degraded':     0.60,
        'unknown':      0.20,
    }
    return defaults.get(regime.lower().replace('regimetype.', ''), 0.5)


def _confidence_conclusion(regime: str, confidence: float) -> str:
    """Return conclusion string for regime + confidence."""
    regime_key = regime.lower().replace('regimetype.', '')
    overrides = {
        'event':        'Temporary air quality event detected. Purifier responding.',
        'degraded':     'Air quality degraded. Investigate source.',
        'initialising': 'System establishing baseline — no action required.',
        'validating':   'Baseline re-establishing after maintenance.',
        'unknown':      'Sensor data unavailable — check connections.',
    }
    if regime_key in overrides:
        return overrides[regime_key]
    if confidence >= 0.9: return 'Air quality is good.'
    if confidence >= 0.7: return 'Air quality is good. Monitoring stable.'
    if confidence >= 0.5: return 'Air quality appears good. Baseline review recommended.'
    if confidence >= 0.3: return 'Air quality assessment uncertain. Engineer review required.'
    return 'Insufficient confidence to assess. Baseline re-establishment required.'


def _normalise_regime(raw: str) -> str:
    """Normalise regime string from Python enum repr to clean lowercase."""
    if not raw:
        return 'initialising'
    return str(raw).lower().replace('regimetype.', '').strip()


def enrich_frames(frames: list[dict], transitions: list[dict]) -> list[dict]:
    """
    Enrich raw sensor frames with twin state fields:
      - regime
      - confidence
      - confidence_conclusion
      - baseline_locked
      - baseline_std

    Uses state_transitions to determine which regime was active at each
    frame timestamp. Carries baseline values forward from the point they
    were locked.

    Args:
        frames:      Raw sensor rows from raw_readings
        transitions: All state transitions from state_transitions table

    Returns:
        Enriched frames with twin state fields added
    """
    if not transitions:
        # No transition history — mark everything as initialising
        for f in frames:
            f['regime'] = 'initialising'
            f['confidence'] = 0.52
            f['confidence_conclusion'] = _confidence_conclusion('initialising', 0.52)
            f['baseline_locked'] = None
            f['baseline_std'] = None
        return frames

    # Build regime timeline — list of (ts, regime) sorted by time
    regime_timeline = []
    current_regime = 'initialising'
    for t in transitions:
        ts = t['ts']
        to_regime = (t['to_regime'] or 'initialising').lower().replace('regimetype.', '')
        regime_timeline.append((ts, to_regime))
        current_regime = to_regime

    # Extract baseline lock info from transitions
    baseline_locked = None
    baseline_std = 0.78  # from your system — will be overridden if found in reason
    for t in transitions:
        reason = t.get('reason') or ''
        to_regime = (t['to_regime'] or '').lower().replace('regimetype.', '')
        if to_regime == 'baseline' and 'locked' in reason.lower():
            # Extract value from reason string e.g. "Baseline locked at 0.2 µg/m³..."
            import re
            match = re.search(r'locked at ([\d.]+)', reason)
            if match:
                baseline_locked = float(match.group(1))

    def get_regime_at(ts: str) -> str:
        """Find which regime was active at a given timestamp."""
        active = 'initialising'
        for timeline_ts, regime in regime_timeline:
            if timeline_ts <= ts:
                active = regime
            else:
                break
        return active

    # Enrich each frame
    enriched = []
    for frame in frames:
        ts = frame.get('ts', '')
        regime = get_regime_at(ts)
        confidence = _regime_confidence(regime)

        # During event regime, modulate confidence by PM2.5 level
        pm25 = frame.get('value') or 0
        if regime == 'event' and pm25 > 20:
            confidence = max(0.6, confidence - (pm25 / 200))

        enriched_frame = {
            **frame,
            'regime': _normalise_regime(regime),
            'confidence': round(confidence, 3),
            'confidence_conclusion': _confidence_conclusion(regime, confidence),
            'baseline_locked': baseline_locked,
            'baseline_std': baseline_std if baseline_locked else None,
        }
        enriched.append(enriched_frame)

    return enriched


# ---------------------------------------------------------------------------
# Existing functions (unchanged)
# ---------------------------------------------------------------------------

def fetch_readings(conn, start_ts: str, end_ts: str) -> list[dict]:
    rows = conn.execute("""
        SELECT ts, value, rolling_mean, rolling_std, trend_slope,
               purifier_on, fan_speed, fan_mode,
               filter_age, device_age, pm25_internal,
               is_plausible, plausibility_reason, is_warmup
        FROM raw_readings
        WHERE ts >= ? AND ts <= ?
        ORDER BY ts ASC
    """, (start_ts, end_ts)).fetchall()
    return [dict(r) for r in rows]


def fetch_transitions(conn, start_ts: str, end_ts: str) -> list[dict]:
    rows = conn.execute("""
        SELECT ts, from_regime, to_regime, reason, duration_sec
        FROM state_transitions
        WHERE ts >= ? AND ts <= ?
        ORDER BY ts ASC
    """, (start_ts, end_ts)).fetchall()
    return [dict(r) for r in rows]


def detect_spike_windows(conn) -> list[dict]:
    print(f"  Scanning {DB_PATH.name} for spike events...", end='', flush=True)
    rows = conn.execute("""
        SELECT ts, value, rolling_mean, fan_speed, fan_mode, purifier_on
        FROM raw_readings WHERE is_warmup = 0 ORDER BY ts ASC
    """).fetchall()
    if not rows:
        print(red(" no readings found")); return []
    print(f" {len(rows):,} readings")

    windows = []
    i = 0
    while i < len(rows) - 60:
        row = rows[i]
        current = row['value'] or 0
        mean = row['rolling_mean'] or current
        if current > mean + 5.0 and current > 2.0:
            ramp_start_idx = max(0, i - 180)
            for j in range(i, ramp_start_idx, -1):
                if (rows[j]['value'] or 0) < mean + 1.0:
                    ramp_start_idx = j; break
            peak_val = current; peak_idx = i
            end_idx = min(i + 3600, len(rows) - 1)
            for k in range(i, end_idx):
                v = rows[k]['value'] or 0
                if v > peak_val: peak_val = v; peak_idx = k
                if k > i + 60 and v < mean + 2.0: end_idx = k; break
            max_fan_speed = max((r['fan_speed'] or 0) for r in rows[i:end_idx])
            fan_modes = set(r['fan_mode'] for r in rows[i:end_idx] if r['fan_mode'])
            start_ts = rows[ramp_start_idx]['ts']
            end_ts = rows[end_idx]['ts']
            peak_ts = rows[peak_idx]['ts']
            try:
                start_dt = datetime.fromisoformat(start_ts.replace('Z', '+00:00'))
                end_dt = datetime.fromisoformat(end_ts.replace('Z', '+00:00'))
                duration_min = (end_dt - start_dt).total_seconds() / 60
            except: duration_min = 0
            windows.append({
                "type": "spike", "start_ts": start_ts, "end_ts": end_ts,
                "peak_ts": peak_ts, "baseline_val": round(mean, 1),
                "peak_val": round(peak_val, 1), "rise": round(peak_val - mean, 1),
                "duration_min": round(duration_min, 1), "max_fan_speed": max_fan_speed,
                "fan_modes": list(fan_modes), "frame_count": end_idx - ramp_start_idx,
            })
            i = end_idx + 1
        else:
            i += 1
    return windows


def detect_transition_windows(conn) -> list[dict]:
    rows = conn.execute("""
        SELECT st.ts, st.from_regime, st.to_regime, st.reason, st.duration_sec,
               r.value as pm25_at_transition
        FROM state_transitions st
        LEFT JOIN raw_readings r ON r.ts = (
            SELECT ts FROM raw_readings WHERE ts <= st.ts ORDER BY ts DESC LIMIT 1
        )
        ORDER BY st.ts ASC
    """).fetchall()
    windows = []
    for row in rows:
        try:
            ts = row['ts']
            dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            windows.append({
                "type": "transition",
                "start_ts": (dt - timedelta(minutes=2)).isoformat(),
                "end_ts": (dt + timedelta(minutes=10)).isoformat(),
                "transition_ts": ts,
                "from_regime": row['from_regime'],
                "to_regime": row['to_regime'],
                "reason": row['reason'],
                "pm25_at_transition": round(row['pm25_at_transition'] or 0, 1),
            })
        except: continue
    return windows


def detect_normal_windows(conn) -> list[dict]:
    rows = conn.execute("""
        SELECT ts, value, rolling_mean, rolling_std FROM raw_readings
        WHERE is_plausible = 1 AND plausibility_reason = 'ok'
          AND rolling_std < 1.0 AND value < 2.0
        ORDER BY ts DESC LIMIT 600
    """).fetchall()
    if len(rows) < 300: return []
    mean_val = sum(r['value'] or 0 for r in rows[:300]) / 300
    return [{"type": "normal", "start_ts": rows[299]['ts'], "end_ts": rows[0]['ts'],
             "mean_val": round(mean_val, 2), "duration_min": 5.0}]


def preview_window(frames: list[dict], label: str) -> None:
    if not frames: print(yellow("  No frames to preview")); return
    values = [f['value'] or 0 for f in frames]
    max_val = max(values) if values else 1
    print(f"\n  {bold(label)} — {len(frames)} frames")
    print(f"  PM2.5 range: {min(values):.1f} → {max_val:.1f} µg/m³\n")
    sample_every = max(1, len(frames) // 40)
    for frame in frames[::sample_every]:
        val = frame['value'] or 0
        regime = frame.get('regime', '?')[:4].upper()
        fan = frame.get('fan_speed', '-') or '-'
        mode = (frame.get('fan_mode') or '')[:1].upper()
        ts_short = frame['ts'][11:19] if frame['ts'] else '?'
        print(f"  {ts_short}  [{bar(val, max_val, 20)}] {val:5.1f}  fan:{fan}{mode}  {regime}")
    print()


def compose_recording(windows_data, gap_seconds=5) -> dict:
    all_frames = []
    for i, (frames, label) in enumerate(windows_data):
        if i > 0 and gap_seconds > 0 and all_frames:
            gap_frame = {**all_frames[-1], "_gap": True}
            for _ in range(gap_seconds):
                all_frames.append(gap_frame)
        all_frames.extend(frames)
    return {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "total_frames": len(all_frames),
        "duration_seconds": len(all_frames),
        "windows": [label for _, label in windows_data],
        "frames": all_frames,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if sys.platform == 'win32': os.system('color')

    print(f"\n{bold('═'*50)}\n{bold('  Air Twin Demo Recorder v2')}\n{bold('═'*50)}\n")

    conn = get_conn()

    # Load full transition history for enrichment
    print(bold("Loading twin state history..."), end='', flush=True)
    all_transitions = fetch_twin_state_history(conn)
    print(f" {len(all_transitions)} transitions found")
    print()

    print(bold("Scanning database for interesting events...\n"))

    spike_windows = detect_spike_windows(conn)
    transition_windows = detect_transition_windows(conn)
    normal_windows = detect_normal_windows(conn)

    all_candidates = (
        [("spike", w) for w in spike_windows] +
        [("transition", w) for w in transition_windows] +
        [("normal", w) for w in normal_windows]
    )

    if not all_candidates:
        print(yellow("No events found.")); sys.exit(0)

    print(bold(f"Found {len(all_candidates)} candidate window(s):\n"))

    for idx, (event_type, w) in enumerate(all_candidates, 1):
        num = bold(f"  [{idx}]")
        if event_type == "spike":
            ts_display = w['start_ts'][5:16].replace('T', ' ')
            rise_color = red if w['rise'] > 50 else yellow if w['rise'] > 10 else cyan
            print(f"{num} {ts_display} — {bold('PM2.5 spike')}: "
                  f"{w['baseline_val']} → {rise_color(str(w['peak_val']))} µg/m³  (+{w['rise']} µg/m³)")
            print(f"       Purifier: auto 1→{w['max_fan_speed']}  |  "
                  f"{w['duration_min']} min  |  {w['frame_count']:,} frames")
        elif event_type == "transition":
            ts_display = w['transition_ts'][5:16].replace('T', ' ')
            print(f"{num} {ts_display} — {bold('Regime transition')}: "
                  f"{cyan(w['from_regime'] or 'none')} → {green(w['to_regime'])}")
            if w['reason']:
                print(f"       {w['reason'][:70]}")
        elif event_type == "normal":
            ts_display = w['start_ts'][5:16].replace('T', ' ')
            print(f"{num} {ts_display} — {bold('Normal baseline')}: mean {w['mean_val']} µg/m³")
        print()

    print(bold("Select event(s) to include (e.g. 1,3) or 'all'"))
    selection_str = prompt("  Selection")

    if selection_str.lower() == 'all':
        selected_indices = list(range(1, len(all_candidates) + 1))
    else:
        try:
            selected_indices = [int(x.strip()) for x in selection_str.split(',')]
        except ValueError:
            print(red("Invalid selection")); sys.exit(1)

    selected = [all_candidates[i-1] for i in selected_indices
                if 1 <= i <= len(all_candidates)]
    if not selected:
        print(red("No valid events selected")); sys.exit(1)

    print()
    windows_data = []

    for event_type, w in selected:
        print(bold("─" * 40))

        if event_type == "spike":
            ts_display = w['start_ts'][5:16].replace('T', ' ')
            print(bold(f"Spike — {ts_display}  peak={w['peak_val']} µg/m³\n"))
            lead_in = int(prompt("  Seconds before ramp-up", default=60))
            lead_out = int(prompt("  Seconds after resolution", default=120))
            try:
                start_dt = datetime.fromisoformat(w['start_ts'].replace('Z', '+00:00'))
                end_dt = datetime.fromisoformat(w['end_ts'].replace('Z', '+00:00'))
                adj_start = (start_dt - timedelta(seconds=lead_in)).isoformat()
                adj_end = (end_dt + timedelta(seconds=lead_out)).isoformat()
            except:
                adj_start = w['start_ts']; adj_end = w['end_ts']

        elif event_type == "transition":
            ts_display = w['transition_ts'][5:16].replace('T', ' ')
            print(bold(f"Transition — {ts_display}  {w['from_regime']} → {w['to_regime']}\n"))
            lead_in = int(prompt("  Seconds before transition", default=60))
            lead_out = int(prompt("  Seconds after transition", default=120))
            try:
                trans_dt = datetime.fromisoformat(w['transition_ts'].replace('Z', '+00:00'))
                adj_start = (trans_dt - timedelta(seconds=lead_in)).isoformat()
                adj_end = (trans_dt + timedelta(seconds=lead_out)).isoformat()
            except:
                adj_start = w['start_ts']; adj_end = w['end_ts']

        else:
            ts_display = w['start_ts'][5:16].replace('T', ' ')
            print(bold(f"Normal baseline — {ts_display}\n"))
            adj_start = w['start_ts']; adj_end = w['end_ts']

        print(f"  Fetching frames...", end='', flush=True)
        frames = fetch_readings(conn, adj_start, adj_end)
        print(f" {len(frames):,} frames")

        if not frames:
            print(yellow("  No frames — skipping")); continue

        # ENRICH frames with twin state
        print(f"  Enriching with twin state...", end='', flush=True)
        frames = enrich_frames(frames, all_transitions)
        print(f" done")

        if confirm("  Preview?", default=True):
            preview_window(frames, f"{event_type} — {ts_display}")

        if confirm("  Include?", default=True):
            windows_data.append((frames, f"{event_type}:{ts_display}"))
            print(green(f"  Added — {len(frames):,} frames"))
        else:
            print(yellow("  Skipped"))
        print()

    if not windows_data:
        print(red("Nothing to write")); sys.exit(0)

    print(bold("─" * 40))
    print(bold("Composing recording\n"))

    total = sum(len(f) for f, _ in windows_data)
    print(f"  Windows: {len(windows_data)}")
    print(f"  Total frames: {total:,}  ({total/60:.1f} min)\n")

    gap = int(prompt("  Gap between windows (seconds)", default=5)) if len(windows_data) > 1 else 5
    recording = compose_recording(windows_data, gap_seconds=gap)

    output_path = Path(prompt("  Output path", default=str(OUTPUT_PATH)))
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists() and not confirm(f"  Overwrite {output_path.name}?"):
        print(yellow("Aborted")); sys.exit(0)

    with open(output_path, 'w') as f:
        json.dump(recording, f, indent=2)

    size_kb = output_path.stat().st_size / 1024
    print(green(f"\n  Written — {recording['total_frames']:,} frames, {size_kb:.1f} KB"))
    print(f"  → {output_path}\n")

    if confirm("  Commit and push?", default=True):
        import subprocess
        desc = " + ".join(label for _, label in windows_data)
        for cmd in [
            ["git", "add", str(output_path)],
            ["git", "commit", "-m", f"Update demo recording — {desc}"],
            ["git", "push"],
        ]:
            r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
            print(green(f"  {cmd[1]}: ok") if r.returncode == 0
                  else red(f"  {cmd[1]} failed: {r.stderr.strip()}"))

    print(f"\n{bold('Done.')}\n")
    conn.close()


if __name__ == "__main__":
    main()