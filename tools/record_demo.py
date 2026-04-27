"""
record_demo.py — Interactive demo recording tool for Air Twin.

Scans the database for interesting events, lets you select and preview
windows, compose a recording from multiple events, and writes
frontend/demo/recording.json for the GitHub Pages demo mode.

Usage:
    python tools/record_demo.py

Run from project root with venv active.
"""

import json
import sqlite3
import sys
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "data" / "airtwin.db"
OUTPUT_PATH = ROOT / "frontend" / "demo" / "recording.json"


# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------

class Colors:
    HEADER  = '\033[95m'
    BLUE    = '\033[94m'
    CYAN    = '\033[96m'
    GREEN   = '\033[92m'
    YELLOW  = '\033[93m'
    RED     = '\033[91m'
    BOLD    = '\033[1m'
    END     = '\033[0m'

def bold(s): return f"{Colors.BOLD}{s}{Colors.END}"
def green(s): return f"{Colors.GREEN}{s}{Colors.END}"
def yellow(s): return f"{Colors.YELLOW}{s}{Colors.END}"
def cyan(s): return f"{Colors.CYAN}{s}{Colors.END}"
def red(s): return f"{Colors.RED}{s}{Colors.END}"

def prompt(msg, default=None):
    """Prompt user for input with optional default."""
    if default is not None:
        display = f"{msg} [{default}]: "
    else:
        display = f"{msg}: "
    try:
        val = input(display).strip()
        return val if val else (str(default) if default is not None else "")
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        sys.exit(0)

def confirm(msg, default=True):
    """Yes/no confirmation prompt."""
    hint = "[Y/n]" if default else "[y/N]"
    val = prompt(f"{msg} {hint}").lower()
    if not val:
        return default
    return val in ('y', 'yes')

def bar(value, max_value, width=30, char='█', empty='░'):
    """Simple ASCII progress bar."""
    filled = int(width * value / max_value) if max_value > 0 else 0
    return char * filled + empty * (width - filled)


# ---------------------------------------------------------------------------
# Database queries
# ---------------------------------------------------------------------------

def get_conn():
    if not DB_PATH.exists():
        print(red(f"Database not found: {DB_PATH}"))
        print("Run the backend first to create the database.")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_readings(conn, start_ts: str, end_ts: str) -> list[dict]:
    """Fetch all readings between two ISO8601 timestamps."""
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
    """Fetch regime transitions in a time window."""
    rows = conn.execute("""
        SELECT ts, from_regime, to_regime, reason, duration_sec
        FROM state_transitions
        WHERE ts >= ? AND ts <= ?
        ORDER BY ts ASC
    """, (start_ts, end_ts)).fetchall()
    return [dict(r) for r in rows]


def detect_spike_windows(conn) -> list[dict]:
    """
    Scan database for PM2.5 ramp-up events.
    A spike window is defined as:
      - PM2.5 rises from baseline by > 5 µg/m³
      - Rise happens within 5 minutes
    Returns list of candidate windows with metadata.
    """
    print(f"  Scanning {DB_PATH.name} for spike events...", end='', flush=True)

    # Sample at 10-second intervals for performance
    rows = conn.execute("""
        SELECT ts, value, rolling_mean, fan_speed, fan_mode, purifier_on
        FROM raw_readings
        WHERE is_warmup = 0
        ORDER BY ts ASC
    """).fetchall()

    if not rows:
        print(red(" no readings found"))
        return []

    print(f" {len(rows):,} readings")

    windows = []
    i = 0
    while i < len(rows) - 60:
        row = rows[i]
        current = row['value'] or 0
        mean = row['rolling_mean'] or current

        # Look for significant rise
        if current > mean + 5.0 and current > 2.0:
            # Found start of spike — trace back to find ramp-up start
            ramp_start_idx = max(0, i - 180)  # up to 3 min before
            for j in range(i, ramp_start_idx, -1):
                if (rows[j]['value'] or 0) < mean + 1.0:
                    ramp_start_idx = j
                    break

            # Find peak
            peak_val = current
            peak_idx = i
            end_idx = min(i + 3600, len(rows) - 1)  # look up to 60 min ahead
            for k in range(i, end_idx):
                v = rows[k]['value'] or 0
                if v > peak_val:
                    peak_val = v
                    peak_idx = k
                if k > i + 60 and v < mean + 2.0:
                    end_idx = k
                    break

            # Get purifier response info
            max_fan_speed = max(
                (r['fan_speed'] or 0) for r in rows[i:end_idx]
            )
            fan_modes = set(
                r['fan_mode'] for r in rows[i:end_idx]
                if r['fan_mode']
            )

            start_ts = rows[ramp_start_idx]['ts']
            end_ts = rows[end_idx]['ts']
            peak_ts = rows[peak_idx]['ts']

            # Parse timestamps for display
            try:
                start_dt = datetime.fromisoformat(start_ts.replace('Z', '+00:00'))
                end_dt = datetime.fromisoformat(end_ts.replace('Z', '+00:00'))
                duration_min = (end_dt - start_dt).total_seconds() / 60
            except Exception:
                duration_min = 0

            baseline_val = round(mean, 1)
            rise = round(peak_val - baseline_val, 1)

            windows.append({
                "type": "spike",
                "start_ts": start_ts,
                "end_ts": end_ts,
                "peak_ts": peak_ts,
                "ramp_start_idx": ramp_start_idx,
                "peak_idx": peak_idx,
                "end_idx": end_idx,
                "baseline_val": baseline_val,
                "peak_val": round(peak_val, 1),
                "rise": rise,
                "duration_min": round(duration_min, 1),
                "max_fan_speed": max_fan_speed,
                "fan_modes": list(fan_modes),
                "frame_count": end_idx - ramp_start_idx,
            })

            # Skip ahead past this event
            i = end_idx + 1
        else:
            i += 1

    return windows


def detect_transition_windows(conn) -> list[dict]:
    """Find regime transition events."""
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
            start_dt = dt - timedelta(minutes=2)
            end_dt = dt + timedelta(minutes=10)

            windows.append({
                "type": "transition",
                "start_ts": start_dt.isoformat(),
                "end_ts": end_dt.isoformat(),
                "transition_ts": ts,
                "from_regime": row['from_regime'],
                "to_regime": row['to_regime'],
                "reason": row['reason'],
                "pm25_at_transition": round(row['pm25_at_transition'] or 0, 1),
                "duration_sec": row['duration_sec'],
            })
        except Exception:
            continue

    return windows


def detect_normal_windows(conn) -> list[dict]:
    """Find stable baseline operation windows for context."""
    rows = conn.execute("""
        SELECT ts, value, rolling_mean, rolling_std
        FROM raw_readings
        WHERE is_plausible = 1
          AND plausibility_reason = 'ok'
          AND rolling_std < 1.0
          AND value < 2.0
        ORDER BY ts DESC
        LIMIT 600
    """).fetchall()

    if len(rows) < 300:
        return []

    # Use the most recent 5-minute stable window
    end_ts = rows[0]['ts']
    start_ts = rows[299]['ts']
    mean_val = sum(r['value'] or 0 for r in rows[:300]) / 300

    return [{
        "type": "normal",
        "start_ts": start_ts,
        "end_ts": end_ts,
        "mean_val": round(mean_val, 2),
        "duration_min": 5.0,
        "description": "Stable baseline operation",
    }]


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

def preview_window(frames: list[dict], label: str) -> None:
    """Show a text-based preview of a recording window."""
    if not frames:
        print(yellow("  No frames to preview"))
        return

    values = [f['value'] or 0 for f in frames]
    max_val = max(values) if values else 1
    min_val = min(values) if values else 0

    print(f"\n  {bold(label)} — {len(frames)} frames")
    print(f"  PM2.5 range: {min_val:.1f} → {max_val:.1f} µg/m³")
    print()

    # Sample every N frames for display
    sample_every = max(1, len(frames) // 40)
    sampled = frames[::sample_every]

    for frame in sampled:
        val = frame['value'] or 0
        b = bar(val, max_val, width=25)
        fan = frame.get('fan_speed', '-') or '-'
        mode = (frame.get('fan_mode') or '')[:1].upper()
        ts_short = frame['ts'][11:19] if frame['ts'] else '?'
        print(f"  {ts_short}  [{b}] {val:6.1f} µg/m³  fan:{fan}{mode}")

    print()


# ---------------------------------------------------------------------------
# Compose recording
# ---------------------------------------------------------------------------

def compose_recording(windows_data: list[tuple[list[dict], str]],
                      gap_seconds: int = 5) -> dict:
    """
    Compose multiple frame lists into a single recording.
    Inserts synthetic gap frames between windows.
    """
    all_frames = []
    total_duration = 0

    for i, (frames, label) in enumerate(windows_data):
        # Add gap between windows
        if i > 0 and gap_seconds > 0 and all_frames:
            last = all_frames[-1]
            gap_frame = {
                **last,
                "ts": last['ts'],
                "_gap": True,
            }
            for _ in range(gap_seconds):
                all_frames.append(gap_frame)

        all_frames.extend(frames)
        total_duration += len(frames)

    return {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "total_frames": len(all_frames),
        "duration_seconds": len(all_frames),
        "windows": [label for _, label in windows_data],
        "frames": all_frames,
    }


# ---------------------------------------------------------------------------
# Main interactive flow
# ---------------------------------------------------------------------------

def main():
    # Windows-compatible color support
    if sys.platform == 'win32':
        os.system('color')

    print()
    print(bold("═" * 50))
    print(bold("  Air Twin Demo Recorder"))
    print(bold("═" * 50))
    print()

    conn = get_conn()

    # --- Scan for events ---
    print(bold("Scanning database for interesting events..."))
    print()

    spike_windows = detect_spike_windows(conn)
    transition_windows = detect_transition_windows(conn)
    normal_windows = detect_normal_windows(conn)

    all_candidates = []

    # Spike events
    for w in spike_windows:
        all_candidates.append(("spike", w))

    # Transition events
    for w in transition_windows:
        all_candidates.append(("transition", w))

    # Normal baseline sample
    for w in normal_windows:
        all_candidates.append(("normal", w))

    if not all_candidates:
        print(yellow("No interesting events found in database."))
        print("Run the backend and collect some data first.")
        print("Tip: do a candle test to generate a spike event.")
        sys.exit(0)

    # --- Display candidates ---
    print(bold(f"Found {len(all_candidates)} candidate window(s):"))
    print()

    for idx, (event_type, w) in enumerate(all_candidates, 1):
        num = bold(f"  [{idx}]")

        if event_type == "spike":
            ts_display = w['start_ts'][5:16].replace('T', ' ')
            rise_color = red if w['rise'] > 50 else yellow if w['rise'] > 10 else cyan
            print(f"{num} {ts_display} — {bold('PM2.5 spike')}: "
                  f"{w['baseline_val']} → {rise_color(str(w['peak_val']))} µg/m³  "
                  f"(+{w['rise']} µg/m³)")
            fan_info = f"auto steps 1→{w['max_fan_speed']}" if w['max_fan_speed'] else "purifier off"
            print(f"       Purifier: {fan_info}  |  Duration: {w['duration_min']} min  |  "
                  f"Frames: {w['frame_count']:,}")

        elif event_type == "transition":
            ts_display = w['transition_ts'][5:16].replace('T', ' ')
            print(f"{num} {ts_display} — {bold('Regime transition')}: "
                  f"{cyan(w['from_regime'] or 'none')} → {green(w['to_regime'])}")
            if w['reason']:
                reason_short = w['reason'][:60] + '...' if len(w['reason']) > 60 else w['reason']
                print(f"       {reason_short}")

        elif event_type == "normal":
            ts_display = w['start_ts'][5:16].replace('T', ' ')
            print(f"{num} {ts_display} — {bold('Normal baseline')}: "
                  f"mean {w['mean_val']} µg/m³  |  {w['duration_min']} min")

        print()

    # --- Selection ---
    print(bold("Select event(s) to include in recording"))
    print("  Enter numbers separated by commas (e.g. 1,3) or 'all'")
    selection_str = prompt("  Selection")

    if selection_str.lower() == 'all':
        selected_indices = list(range(1, len(all_candidates) + 1))
    else:
        try:
            selected_indices = [int(x.strip()) for x in selection_str.split(',')]
        except ValueError:
            print(red("Invalid selection"))
            sys.exit(1)

    selected = []
    for idx in selected_indices:
        if 1 <= idx <= len(all_candidates):
            selected.append(all_candidates[idx - 1])
        else:
            print(yellow(f"  Warning: index {idx} out of range — skipped"))

    if not selected:
        print(red("No valid events selected"))
        sys.exit(1)

    print()

    # --- Configure each window ---
    windows_data = []

    for event_type, w in selected:
        print(bold(f"─" * 40))

        if event_type == "spike":
            ts_display = w['start_ts'][5:16].replace('T', ' ')
            print(bold(f"Spike event — {ts_display}"))
            print(f"  Peak: {w['peak_val']} µg/m³  |  Rise: +{w['rise']} µg/m³")
            print()

            lead_in = int(prompt("  Seconds before ramp-up to include", default=30))
            lead_out = int(prompt("  Seconds after resolution to include", default=60))

            # Calculate adjusted timestamps
            try:
                start_dt = datetime.fromisoformat(
                    w['start_ts'].replace('Z', '+00:00')
                )
                end_dt = datetime.fromisoformat(
                    w['end_ts'].replace('Z', '+00:00')
                )
                adj_start = (start_dt - timedelta(seconds=lead_in)).isoformat()
                adj_end = (end_dt + timedelta(seconds=lead_out)).isoformat()
            except Exception:
                adj_start = w['start_ts']
                adj_end = w['end_ts']

        elif event_type == "transition":
            ts_display = w['transition_ts'][5:16].replace('T', ' ')
            print(bold(f"Regime transition — {ts_display}"))
            print(f"  {w['from_regime']} → {w['to_regime']}")
            print()

            lead_in = int(prompt("  Seconds before transition to include", default=60))
            lead_out = int(prompt("  Seconds after transition to include", default=120))

            try:
                trans_dt = datetime.fromisoformat(
                    w['transition_ts'].replace('Z', '+00:00')
                )
                adj_start = (trans_dt - timedelta(seconds=lead_in)).isoformat()
                adj_end = (trans_dt + timedelta(seconds=lead_out)).isoformat()
            except Exception:
                adj_start = w['start_ts']
                adj_end = w['end_ts']

        else:
            ts_display = w['start_ts'][5:16].replace('T', ' ')
            print(bold(f"Normal baseline — {ts_display}"))
            print()
            adj_start = w['start_ts']
            adj_end = w['end_ts']

        # Fetch frames for this window
        print(f"  Fetching frames...", end='', flush=True)
        frames = fetch_readings(conn, adj_start, adj_end)
        print(f" {len(frames):,} frames")

        if not frames:
            print(yellow("  No frames in this window — skipping"))
            continue

        # Preview
        if confirm("  Preview this window?", default=True):
            label = f"{event_type} — {ts_display}"
            preview_window(frames, label)

        if confirm("  Include this window?", default=True):
            label = f"{event_type}:{ts_display}"
            windows_data.append((frames, label))
            print(green(f"  Added — {len(frames):,} frames"))
        else:
            print(yellow("  Skipped"))

        print()

    if not windows_data:
        print(red("No windows selected — nothing to write"))
        sys.exit(0)

    # --- Compose ---
    print(bold("─" * 40))
    print(bold("Composing recording"))
    print()

    total_frames = sum(len(f) for f, _ in windows_data)
    print(f"  Windows: {len(windows_data)}")
    print(f"  Total frames: {total_frames:,}")
    print(f"  Duration: {total_frames/60:.1f} minutes")
    print()

    gap = 5
    if len(windows_data) > 1:
        gap = int(prompt("  Gap between windows (seconds)", default=5))

    recording = compose_recording(windows_data, gap_seconds=gap)

    # --- Write output ---
    print()
    output_path = OUTPUT_PATH
    custom_path = prompt(f"  Output path", default=str(output_path))
    if custom_path:
        output_path = Path(custom_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists():
        if not confirm(f"  {output_path.name} already exists — overwrite?", default=True):
            print(yellow("Aborted — file not written"))
            sys.exit(0)

    with open(output_path, 'w') as f:
        json.dump(recording, f, indent=2)

    size_kb = output_path.stat().st_size / 1024
    print(green(f"\n  Written — {recording['total_frames']:,} frames, {size_kb:.1f} KB"))
    print(f"  → {output_path}")
    print()

    # --- Commit ---
    if confirm("  Commit and push to GitHub?", default=True):
        import subprocess

        windows_desc = " + ".join(label for _, label in windows_data)
        commit_msg = f"Update demo recording — {windows_desc}"

        cmds = [
            ["git", "add", str(output_path)],
            ["git", "commit", "-m", commit_msg],
            ["git", "push"],
        ]

        for cmd in cmds:
            result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
            if result.returncode == 0:
                print(green(f"  {' '.join(cmd[:2])}: ok"))
            else:
                print(red(f"  {' '.join(cmd[:2])} failed: {result.stderr.strip()}"))
                break
    else:
        print(f"\nRun manually:")
        print(f"  git add {output_path}")
        print(f"  git commit -m 'Update demo recording'")
        print(f"  git push")

    print()
    print(bold("Done."))
    print()

    conn.close()


if __name__ == "__main__":
    main()