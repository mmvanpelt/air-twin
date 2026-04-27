/**
 * demo.js — Demo playback, live WebSocket, and mode management.
 *
 * Manages three modes:
 *   demo — plays back recording.json with timeline scrubber
 *   live — connects to ws://localhost:8000/ws
 *
 * Demo is default. Live is attempted on user request.
 * Falls back to demo if live connection drops.
 */

const AirTwinMode = (() => {
  const RECORDING_PATH = 'demo/recording.json';
  const WS_URL = 'ws://localhost:8000/ws';
  const WS_TIMEOUT_MS = 3000;

  let _mode = 'demo';
  let _recording = null;
  let _frameIndex = 0;
  let _playing = true;
  let _speed = 1;
  let _playInterval = null;
  let _ws = null;
  let _sparklineData = [];

  // ── Public API ──────────────────────────────────────────────

  async function init() {
    await _loadRecording();
    _buildSparkline();
    _initTimeline();
    _initModeToggle();
    _startDemo();
    _setMode('demo');
  }

  function setSpeed(speed) {
    _speed = parseFloat(speed);
    if (_playing && _mode === 'demo') {
      _stopPlayback();
      _startPlayback();
    }
  }

  function seek(frameIndex) {
    _frameIndex = Math.max(0, Math.min(frameIndex, (_recording?.frames?.length ?? 1) - 1));
    if (_recording?.frames?.[_frameIndex]) {
      AirTwinState.update(_recording.frames[_frameIndex]);
    }
    _updateScrubber();
  }

  function togglePlay() {
    if (_mode !== 'demo') return;
    _playing = !_playing;
    const btn = document.getElementById('tl-play');
    if (_playing) {
      _startPlayback();
      if (btn) btn.textContent = '⏸';
    } else {
      _stopPlayback();
      if (btn) btn.textContent = '▶';
    }
  }

  async function switchToLive() {
    _stopPlayback();
    _setStatusConnecting();

    return new Promise((resolve) => {
      const timeout = setTimeout(() => {
        console.log('Live connection timed out — staying in demo mode');
        _setMode('demo');
        _startDemo();
        resolve(false);
      }, WS_TIMEOUT_MS);

      try {
        _ws = new WebSocket(WS_URL);

        _ws.onopen = () => {
          clearTimeout(timeout);
          _setMode('live');
          resolve(true);
        };

        _ws.onmessage = (evt) => {
          try {
            const frame = JSON.parse(evt.data);
            AirTwinState.update(frame);
          } catch (e) {
            console.warn('WS parse error:', e);
          }
        };

        _ws.onclose = () => {
          if (_mode === 'live') {
            console.log('Live connection lost — falling back to demo');
            _setMode('demo');
            _startDemo();
            _showFallbackNotice();
          }
        };

        _ws.onerror = () => {
          clearTimeout(timeout);
          _setMode('demo');
          _startDemo();
          resolve(false);
        };
      } catch (e) {
        clearTimeout(timeout);
        _setMode('demo');
        _startDemo();
        resolve(false);
      }
    });
  }

  function switchToDemo() {
    if (_ws) {
      _ws.onclose = null;
      _ws.close();
      _ws = null;
    }
    _setMode('demo');
    _startDemo();
  }

  // ── Recording loading ───────────────────────────────────────

  async function _loadRecording() {
    try {
      const resp = await fetch(RECORDING_PATH);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      _recording = await resp.json();
      console.log(`Demo recording loaded — ${_recording.total_frames} frames, ${(_recording.duration_seconds/60).toFixed(1)} min`);
    } catch (e) {
      console.error('Failed to load recording:', e);
      _recording = { frames: [], total_frames: 0, duration_seconds: 0 };
    }
  }

  // ── Playback ────────────────────────────────────────────────

  function _startDemo() {
    if (_mode !== 'demo') return;
    _playing = true;
    const btn = document.getElementById('tl-play');
    if (btn) btn.textContent = '⏸';
    _startPlayback();
    const timeline = document.getElementById('timeline');
    if (timeline) timeline.classList.remove('hidden');
  }

  function _startPlayback() {
    _stopPlayback();
    if (!_recording?.frames?.length) return;

    // 1Hz base rate — speed multiplier advances multiple frames per tick
    const tickMs = Math.max(16, 1000 / _speed);
    _playInterval = setInterval(() => {
      if (!_playing || _mode !== 'demo') return;

      const frame = _recording.frames[_frameIndex];
      if (frame) AirTwinState.update(frame);

      _frameIndex++;
      if (_frameIndex >= _recording.frames.length) {
        _frameIndex = 0; // loop
      }

      _updateScrubber();
    }, tickMs);
  }

  function _stopPlayback() {
    if (_playInterval) {
      clearInterval(_playInterval);
      _playInterval = null;
    }
  }

  // ── Timeline UI ─────────────────────────────────────────────

  function _initTimeline() {
    const scrubber = document.getElementById('tl-scrubber');
    const playBtn = document.getElementById('tl-play');
    const speedSel = document.getElementById('tl-speed');

    if (scrubber) {
      scrubber.max = Math.max(0, (_recording?.frames?.length ?? 1) - 1);
      scrubber.addEventListener('input', () => {
        seek(parseInt(scrubber.value, 10));
      });
      scrubber.addEventListener('mousedown', () => {
        _stopPlayback();
      });
      scrubber.addEventListener('mouseup', () => {
        if (_playing && _mode === 'demo') _startPlayback();
      });
    }

    if (playBtn) {
      playBtn.addEventListener('click', togglePlay);
    }

    if (speedSel) {
      speedSel.addEventListener('change', () => setSpeed(speedSel.value));
    }
  }

  function _updateScrubber() {
    const scrubber = document.getElementById('tl-scrubber');
    const timeEl = document.getElementById('tl-time');
    if (!_recording?.frames?.length) return;

    const total = _recording.frames.length;
    const pct = _frameIndex / total;

    if (scrubber) scrubber.value = _frameIndex;

    if (timeEl) {
      const elapsedSec = Math.floor(_frameIndex);
      const totalSec = Math.floor(_recording.duration_seconds || total);
      timeEl.textContent = `${_fmtTime(elapsedSec)} / ${_fmtTime(totalSec)}`;
    }
  }

  function _fmtTime(seconds) {
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return `${m}:${String(s).padStart(2, '0')}`;
  }

  // ── Sparkline ───────────────────────────────────────────────

  function _buildSparkline() {
    const container = document.getElementById('tl-sparkline');
    if (!container || !_recording?.frames?.length) return;

    const frames = _recording.frames;
    const values = frames.map(f => f.value || f.pm25 || 0);
    const maxVal = Math.max(...values, 1);
    const w = 800; // SVG viewBox width
    const h = 24;
    const step = w / values.length;

    let path = `M 0 ${h}`;
    values.forEach((v, i) => {
      const x = i * step;
      const y = h - (v / maxVal) * h;
      path += ` L ${x} ${y}`;
    });
    path += ` L ${w} ${h} Z`;

    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
    svg.setAttribute('preserveAspectRatio', 'none');
    svg.style.cssText = 'width:100%;height:100%;display:block;';

    const fill = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    fill.setAttribute('d', path);
    fill.setAttribute('fill', 'rgba(234,179,8,0.15)');
    fill.setAttribute('stroke', 'rgba(234,179,8,0.5)');
    fill.setAttribute('stroke-width', '1');

    svg.appendChild(fill);
    container.appendChild(svg);
    _sparklineData = values;
  }

  // ── Mode UI ─────────────────────────────────────────────────

  function _initModeToggle() {
    const btnDemo = document.getElementById('btn-demo');
    const btnLive = document.getElementById('btn-live');

    if (btnDemo) {
      btnDemo.addEventListener('click', () => {
        if (_mode !== 'demo') switchToDemo();
      });
    }

    if (btnLive) {
      btnLive.addEventListener('click', async () => {
        if (_mode !== 'live') {
          btnLive.textContent = 'CONNECTING...';
          btnLive.disabled = true;
          const ok = await switchToLive();
          btnLive.disabled = false;
          btnLive.textContent = 'LIVE';
          if (!ok) {
            btnLive.textContent = 'UNAVAILABLE';
            setTimeout(() => { btnLive.textContent = 'LIVE'; }, 2000);
          }
        }
      });
    }
  }

  function _setMode(mode) {
    _mode = mode;
    const btnDemo = document.getElementById('btn-demo');
    const btnLive = document.getElementById('btn-live');
    const dotEl = document.getElementById('status-dot');
    const labelEl = document.getElementById('status-label');
    const timeline = document.getElementById('timeline');

    if (btnDemo) btnDemo.classList.toggle('active', mode === 'demo');
    if (btnLive) btnLive.classList.toggle('active', mode === 'live');

    if (mode === 'demo') {
      if (dotEl) { dotEl.className = 'status-dot demo'; }
      if (labelEl) labelEl.textContent = 'DEMO';
      if (timeline) timeline.classList.remove('hidden');
    } else {
      if (dotEl) { dotEl.className = 'status-dot live'; }
      if (labelEl) labelEl.textContent = 'LIVE';
      if (timeline) timeline.classList.add('hidden');
    }
  }

  function _setStatusConnecting() {
    const dotEl = document.getElementById('status-dot');
    const labelEl = document.getElementById('status-label');
    if (dotEl) dotEl.className = 'status-dot';
    if (labelEl) labelEl.textContent = 'CONNECTING';
  }

  function _showFallbackNotice() {
    const label = document.getElementById('status-label');
    if (label) {
      label.textContent = 'CONNECTION LOST — DEMO';
      setTimeout(() => { label.textContent = 'DEMO'; }, 3000);
    }
  }

  return { init, seek, togglePlay, setSpeed, switchToLive, switchToDemo };
})();

// Boot on DOM ready
document.addEventListener('DOMContentLoaded', () => {
  AirTwinMode.init();
});