# Air Twin

A room-scale digital twin of an air quality system built from the ground up — real hardware, real sensor data, real 3D geometry, and a defensible audit trail that can explain every conclusion it makes.

Built with a Raspberry Pi 3 B+, an IKEA Starkvind air purifier (004.619.49), an SDS011 PM2.5 sensor, Python, FastAPI, and Three.js. Runs entirely on a local network with no internet dependency at runtime.

---

## What it does

Air Twin mirrors a physical room and air purifier in a live 3D scene. As the purifier runs, PM2.5 sensor readings are processed on the Raspberry Pi — qualified for plausibility, enriched with rolling statistics and trend slope — and published to a local MQTT broker. The Windows backend subscribes, persists readings to an append-only SQLite log, and drives a confidence state machine that classifies air quality trends and maintains explicit regime boundaries.

When a filter is replaced, a QR scan on an iPhone records the event as a first-class maintenance record — closing the prior performance regime, opening a new one, and resetting the confidence model. The system generates a one-page executive brief summarising financial position, asset performance, compliance, and required action — with explicit judgments, not just data.

The goal is not prediction or optimisation. The goal is trustworthy representation — a twin that can defend every conclusion it makes from raw, auditable data.

---

## Why it exists

Most air quality dashboards display data. This project models the system behind the data — including the physical asset, its maintenance history, the causal boundaries between performance regimes, and the confidence level of every conclusion drawn. The distinction matters: a PM2.5 reading after a filter change means something completely different to the same reading before one. Air Twin knows the difference.

This is an MVP proving state continuity and trustworthy representation. Prediction and autonomous control are deferred to later phases deliberately — the foundation has to be correct before anything is built on top of it.

---

## Architecture

### Hardware — all delivered

| Device | Role |
|---|---|
| Raspberry Pi 3 B+ | Intelligent edge processor · MQTT broker · sensor host |
| SDS011 sensor | PM2.5 readings · USB serial to Pi · 0–999.9 µg/m³ |
| IKEA Starkvind (004.619.49) | Air purifier · Zigbee · on/off + fan speed 1–5 · auto mode |
| Zigbee USB dongle | Bridges Starkvind to Zigbee2MQTT on Pi |
| iPhone | QR scan maintenance input · Polycam LiDAR room scan (offline, once) |
| Windows machine | FastAPI backend · Three.js frontend · SQLite · twin engine |

### Network topology

All communication is local network only. No internet required at runtime. Both Pi and Windows have static IPs.

```
SDS011 ──USB──► Pi ──pyserial──► edge processing
                                  ├── warmup discard (5 readings)
                                  ├── hardware bounds check (0–999.9 µg/m³)
                                  ├── rolling window (30 readings · mean + std)
                                  ├── dynamic plausibility check
                                  └── trend slope (linear regression)
                                        │
                                        ▼ enriched JSON
Starkvind ──Zigbee──► Pi ──Zigbee2MQTT──► Mosquitto broker :1883
                                                │
                                                │ MQTT over LAN
                                                ▼
                                       Windows machine
                                        ├── paho-mqtt subscriber
                                        ├── FastAPI :8000
                                        │     ├── REST endpoints
                                        │     ├── WebSocket → Three.js
                                        │     ├── GET  /brief     (executive)
                                        │     ├── GET  /engineer  (engineer)
                                        │     └── GET  /respond/{asset_id} (operator)
                                        ├── Twin engine
                                        │     ├── Confidence FSM (5 states)
                                        │     ├── Regime boundary logic
                                        │     ├── PM classification
                                        │     ├── Cumulative load model
                                        │     └── Brief generator
                                        ├── SQLite (append-only)
                                        │     ├── sensor_events
                                        │     ├── maintenance_events
                                        │     ├── regimes
                                        │     ├── escalation_events
                                        │     ├── state_transitions
                                        │     └── views: enriched_readings
                                        │              regime_summary
                                        │              performance_vs_spec
                                        └── twin_state.json

iPhone ──Wi-Fi──► FastAPI :8000
  ├── POST /maintenance    (filter change QR scan)
  └── POST /respond/{id}  (operator incident response)

Polycam ──offline──► Blender ──► room.glb + purifier.glb ──► frontend/assets/

Phase 3 reserved (not yet active):
  airtwin/control/fan_speed   Windows → Pi
  airtwin/control/mode        Windows → Pi
```

### Software stack

| Layer | Technology |
|---|---|
| Edge broker | Mosquitto on Raspberry Pi |
| Sensor ingestion + edge processing | pyserial + paho-mqtt (Python) |
| Purifier state | Zigbee2MQTT |
| Backend | FastAPI + Uvicorn (Python) |
| Twin engine | Pure Python — confidence FSM, regime logic, cumulative load, brief generator |
| Storage | SQLite (append-only event log + views) + twin_state.json |
| Frontend | Three.js — served locally, no CDN · operator HUD · engineer page · executive brief |
| 3D pipeline | Polycam (iPhone LiDAR) → Blender → glTF Binary (.glb) |

### Twin layers

| Layer | What it models |
|---|---|
| Geometry | LiDAR room mesh + Blender-refined purifier mesh at correct scale |
| Semantics | Asset identity · sensor binding · room association · filter state · device profile ref |
| Runtime state | PM2.5 · purifier mode · fan speed · filter age · runtime hours · confidence · regime |

---

## Operating regime model

Every sensor reading belongs to exactly one regime. Regimes never overlap. Pre- and post-maintenance data are permanently separated by a hard boundary.

### Regime states

| State | Meaning |
|---|---|
| `initialising` | First run — no reference data · all readings accepted · no plausibility check |
| `validating` | New filter installed · learning new baseline · exits after 12-hour window |
| `baseline` | Normal operation · full dynamic plausibility active · twin trusts conclusions |
| `degraded` | Anomaly detected · twin flags explicitly · does not silently continue |
| `unknown` | No readings for > configurable gap · last known state held but flagged stale |

### Confidence FSM transitions

```
initialising ──► baseline
baseline ──► validating  (filter change QR)
baseline ──► degraded    (anomaly)
validating ──► baseline  (window complete)
validating ──► degraded  (anomaly during validation)
degraded ──► validating  (filter change QR)
degraded ──► baseline    (recovery)
any ──► unknown          (reading gap exceeded)
```

---

## Edge processing — Raspberry Pi

The Pi is an intelligent edge processor, not a dumb sensor pipe. All raw data qualification happens before data reaches the network.

| Component | Function |
|---|---|
| WarmupFilter | Discards first 5 readings — SDS011 spin-up produces unreliable values |
| HardwareBoundsCheck | Rejects readings outside 0.0–999.9 µg/m³ — only fixed thresholds in system |
| RollingWindow | Circular buffer of last 30 readings · Welford algorithm · mean + std dev |
| PlausibilityChecker | Dynamic delta check relative to rolling_std · one-sided · disabled during initialising |
| TrendCalculator | Linear regression on rolling window · produces trend_slope for Pi-local prediction (Phase 2) |
| MQTTPublisher | Publishes enriched JSON — Windows receives pre-qualified data only |

### Enriched MQTT payload

```json
{
  "value": 24.3,
  "timestamp": "2026-04-11T14:32:00Z",
  "is_plausible": true,
  "delta": 1.2,
  "rolling_mean": 22.8,
  "rolling_std": 2.1,
  "trend_slope": -0.4,
  "purifier_on": true,
  "fan_speed": 3
}
```

---

## Maintenance trigger model

Filter replacement is triggered by three independent conditions — any one fires a recommendation:

| Trigger | Basis | Threshold |
|---|---|---|
| Cumulative load | PM2.5 × airflow × time · physics-based | 15% filter life remaining |
| Calendar age | Safety net for low-use filters | 180 days (IKEA recommendation) |
| Performance degradation | Purifier at max fan, PM2.5 not improving | Observational — regime-relative |

### Device profile

All device-specific constants live in `assets/device_profiles.json` — never in code. Adding a new purifier model requires only a JSON entry, no code change.

```json
{
  "ikea_starkvind": {
    "sku": "004.619.49",
    "purchase_price": 199.99,
    "fan_airflow_m3h": {"1": 55, "2": 105, "3": 163, "4": 216, "5": 270},
    "cadr_range_m3h": {"min": 55, "max": 270},
    "auto_mode_supported": true,
    "filter_types": {
      "particulate": {"sku": "304.619.43", "unit_cost": 25.00, "max_calendar_days": 180},
      "gas":         {"sku": "804.881.29", "unit_cost": 35.00, "max_calendar_days": 180}
    }
  }
}
```

---

## Escalation and operator response

### Threshold model

Thresholds cascade in three tiers — class defaults → room class → condition-derived adjustments:

```
Class defaults     system-wide baseline (config.json)
    ↓ overrides
Room class         environment type (residential_bedroom, office, medical, storage)
    ↓ adjustments
Condition-derived  time-of-day · trend severity · filter age
```

### Operator response flow

When a degraded alert fires, the operator scans a QR code posted near the purifier. Safari opens a pre-populated response form served by FastAPI. The operator selects an assessment, action, and expected resolution window. The response is recorded as a first-class event in `escalation_events`.

If the resolution window expires without PM2.5 recovering, the system re-alerts and escalates automatically.

### Command source detection (Phase 3 notation)

The Starkvind cannot distinguish Pi commands from human or device-auto adjustments. Phase 3 control relinquishment uses three combined approaches: command tracking, rapid divergence detection, and timeout relinquishment. Control topics reserved but not yet active.

---

## Role-based value

All four roles use the same underlying twin state — exposed at different abstraction levels.

| Role | What they see | What they can do |
|---|---|---|
| Maintenance technician | Filter life % · runtime hours · cumulative load · QR confirmation | Log filter replacement via QR scan |
| Operator / facilities | Room status · degraded duration · trend direction · escalation alerts | Acknowledge alerts · log investigation response |
| Engineer | Pre-joined SQLite views · regime comparison · performance vs spec · sensor drift indicators | CSV/JSON export · direct DB access · regime analysis API |
| Executive | One-page brief · financial position · asset performance · compliance · required action | Review brief · approve spend above threshold |

---

## Executive brief

Auto-generated from existing twin state. Readable in under 60 seconds. Makes judgments, not data dumps.

### Brief structure

```
OVERALL STATUS:   NORMAL ● / WATCH ● / ATTENTION ● / CRITICAL ●   Trend ↑↓→

FINANCIAL POSITION
  Annual run rate · spend this period · next expected spend · approval required

ASSET PERFORMANCE
  vs specification · PM2.5 this week vs last week · vs pre-purifier baseline

ASSET HEALTH
  Filter life % · runtime hours · confidence level · capacity estimate note

NOTABLE EXCEPTIONS
  Degraded episodes · exceedances · unresolved escalations

COMPLIANCE
  Hours within guidelines · exceedance events · operator response record

REQUIRED EXECUTIVE ACTION
  Explicit yes/no · cost if yes · approval threshold
```

### Delivery

On-demand via `GET /brief` — executive bookmarks the URL. Always reflects current twin state. Scheduled PDF export deferred to Phase 2.

---

## Security model

| Control | Implementation | Scope |
|---|---|---|
| Network boundary | FastAPI not internet-exposed · local LAN only | All traffic |
| Input validation | Pydantic schemas · enum-constrained fields · length bounds | All POST endpoints |
| Read-only DB | SQLite `mode=ro` URI for engineer queries | /engineer · /export |
| Server-side timestamps | Clients cannot backdate records | All write endpoints |
| Client IP logging | Every write logs source IP | All POST endpoints |
| Rate limiting | 10/min operator response · 5/hour exports | POST + export endpoints |
| Append-only audit trail | No UPDATE or DELETE on event tables | All event tables |
| QR shared secret | Random key in URL · stored in .env | /respond/{asset_id} |
| Asset ID validation | Requests for unknown assets return 404 | /respond · /export |
| Authentication | Deferred to Phase 2 (pre-external access) | — |
| HTTPS | Deferred to Phase 2 (pre-external access) | — |

---

## Database schema

### Tables

```
sensor_events         timestamp · pm25_value · is_plausible · purifier_on · fan_speed
                      rolling_mean · rolling_std · trend_slope · regime_id · client_ip

maintenance_events    timestamp · event_type · filter_type · filter_sku
                      prior_regime_id · new_regime_id · actor · client_ip

regimes               regime_id · state · started_at · ended_at · filter_age_days
                      filter_runtime_hours · cumulative_load · filter_life_remaining
                      readings_in_regime · end_reason · opened_by_event_id

state_transitions     transition_id · asset_id · from_state · to_state
                      transitioned_at · duration_in_prior_state_min · trigger

escalation_events     escalation_id · asset_id · triggered_at · degraded_duration_min
                      threshold_crossed · operator_response · responded_at · resolution

control_events        event_id · event_type · timestamp · commanded_value
                      actual_value · source_detected    (Phase 3)
```

### Views

```
enriched_readings     sensor_events joined with regimes + maintenance context
regime_summary        one row per regime · aggregated PM2.5 statistics · asset class
performance_vs_spec   actual PM2.5 per fan speed vs device profile rated CADR
```

---

## MQTT topics

### Active — Phase 1

| Topic | Direction | Payload |
|---|---|---|
| `airtwin/sensor/pm25` | Pi → Windows | Enriched JSON — value, plausibility, rolling stats, trend |
| `airtwin/purifier/state` | Pi → Windows | `on` / `off` — Zigbee2MQTT |
| `airtwin/purifier/fan_speed` | Pi → Windows | 1–5 — Zigbee2MQTT |
| `airtwin/purifier/mode` | Pi → Windows | `manual` / `auto` — Zigbee2MQTT |

### Reserved — Phase 3

| Topic | Direction | Purpose |
|---|---|---|
| `airtwin/control/fan_speed` | Windows → Pi | Autonomous fan speed command |
| `airtwin/control/mode` | Windows → Pi | auto / manual mode override |

---

## Project structure

```
air-twin/
├── backend/
│   ├── main.py                  # FastAPI app + WebSocket + all routes
│   ├── twin_engine.py           # Confidence FSM · regime logic · cumulative load
│   ├── mqtt_subscriber.py       # paho-mqtt client · enriched message handler
│   ├── db.py                    # SQLite · append-only writes · views · exports
│   ├── brief_generator.py       # Executive brief · judgment logic · derive_* functions
│   └── models.py                # Pydantic schemas · OperatorResponse · ExecutiveBrief
├── frontend/
│   ├── index.html               # Three.js entry point · operator HUD
│   ├── engineer.html            # Engineer data view · regime comparison · charts
│   ├── respond.html             # Operator response form · mobile-optimised
│   ├── libs/
│   │   └── three.min.js         # Local copy — no CDN dependency
│   ├── js/
│   │   ├── scene.js             # Room + purifier render · glTF loader
│   │   └── state.js             # WebSocket binding · HUD updates · degraded badge
│   ├── css/
│   │   └── style.css
│   └── assets/
│       ├── room.glb             # Polycam → Blender export
│       └── purifier.glb         # Blender-refined Starkvind mesh
├── pi/
│   ├── sds011_reader.py         # Serial reader · edge processing · MQTT publisher
│   ├── config.py                # Broker IP · topic names · edge constants
│   ├── mosquitto.conf           # Broker config · LAN listener · allow_anonymous
│   ├── sds011.service           # systemd unit — auto-start on boot
│   └── requirements.txt         # pyserial · paho-mqtt
├── assets/
│   ├── device_profiles.json     # Device specs · filter costs · CADR values
│   └── config.json              # Approval thresholds · room classes · brief config
├── data/
│   ├── twin.db                  # SQLite — gitignored
│   ├── twin_state.json          # Runtime state — gitignored
│   └── asset_registry.json      # Asset instances · room class · profile ref
├── .env.example                 # Environment variable template
├── .gitignore
├── requirements.txt             # fastapi · uvicorn · paho-mqtt · python-dotenv · slowapi
└── README.md
```

---

## Setup

> Prerequisites: Python 3.11+, Git, VS Code with Git Bash

### Windows — backend

```bash
git clone git@github.com:mmvanpelt/air-twin.git
cd air-twin
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# fill in Pi's static IP in .env
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

### Raspberry Pi

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y mosquitto mosquitto-clients
sudo systemctl enable mosquitto
git clone git@github.com:mmvanpelt/air-twin.git
cd air-twin/pi
pip3 install -r requirements.txt
# configure mosquitto.conf — add listener 1883 + allow_anonymous true
# configure Zigbee2MQTT — pair Starkvind
python3 sds011_reader.py
```

### Environment variables (.env)

```
MQTT_BROKER=192.168.x.x
MQTT_PORT=1883
DB_PATH=data/twin.db
STATE_PATH=data/twin_state.json
ASSET_REGISTRY_PATH=data/asset_registry.json
DEVICE_PROFILES_PATH=assets/device_profiles.json
CONFIG_PATH=assets/config.json
FASTAPI_HOST=0.0.0.0
FASTAPI_PORT=8000
RESPONSE_KEY=your-random-key-here
```

---

## Hard constraints — MVP feature freeze

- One room · one purifier · one primary sensor
- Manual / QR maintenance input only
- No machine learning
- No optimisation
- No predictions
- No autonomous control (Phase 3)
- No multi-room aggregation
- No automated reporting distribution
- No internet required at runtime

> Prediction deferred to Phase 2. Autonomous control deferred to Phase 3. Phase 1 data structures support both without modification.

---

## Success criteria

1. QR scan immediately changes twin state and closes the prior regime
2. PM2.5 behaviour after the scan is treated as a new regime
3. Historical data before and after a filter change is never mixed
4. A human can clearly explain why the twin believes what it does
5. The system generates an executive brief that stands on its own

> If the system can defend its beliefs, the twin is credible.

---

## Phase roadmap

| Phase | Focus | Key additions |
|---|---|---|
| 1 — MVP (now) | Trustworthy representation · state continuity | Regime boundaries · QR maintenance · executive brief · four-role value |
| 2 — Prediction | Local linear prediction on Pi · filter life estimation | trend_slope × horizon · prediction validation · scheduled PDF brief |
| 3 — Control | Autonomous fan speed control | Bidirectional MQTT · control relinquishment · control_events active |

---

## Project status

| Component | Status |
|---|---|
| MVP specification | Complete |
| Architecture design | Complete — regime model, edge processing, three-tier roadmap, role value, security |
| Software stack | Decided |
| Network topology | Decided |
| GitHub repository | Live — github.com/mmvanpelt/air-twin |
| VS Code + Git | Configured |
| Hardware | Delivered |
| 3D assets | Polycam scan complete — Blender import next |
| Virtual environment | Next step |
| Pi setup | Not started |
| Backend code | Not started |
| Frontend code | Not started |

---

## License

MIT — see [LICENSE](LICENSE)
