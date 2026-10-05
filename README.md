# Ignition Gateway Harness

An automated Docker orchestration and testing framework for Inductive Automation's Ignition 8.1.51 Gateway fleets.

The harness discovers `.gwbk` gateway backup archives, manages persistent volume storage, generates unified multi-gateway Docker Compose fleet architectures, isolates ports, applies low-RAM JVM and container tuning, and supports dual operating modes (initial backup restore vs. running existing persistent containers).

---

## Key Features

- **100% Production Isolation (Zero Outbound Leaks & Email Sink Trap)**:
  - **Host Port Publishing Preserved**: Standard Docker bridge `ignition_network` allows direct host browser access to gateway web UIs (e.g. `http://localhost:8100`). Defense-in-depth isolation is provided by `extra_hosts` blackholing and container mock redirection.
  - **Zero Outbound Emails**: Discovered corporate mail relays (`smtp.office365.com`, `smtp.oshkoshglobal.com`, `sszsmtp`) resolve directly to Mailpit. Real alarm emails can NEVER reach real people. All emails are captured in the Mailpit sink web UI at `http://localhost:8025`.
  - **DNS & Host Blackholing / Redirection (`extra_hosts`)**: All discovered production database servers, SMTP relays, MQTT brokers, and OPC endpoints are injected into `extra_hosts` redirecting to local simulation containers or blackholing to `127.0.0.1` / `0.0.0.0`.
- **Single-Command Orchestration (`up` / `down`)**:
  - `ignition-gateway-harness up`: 1 command to generate isolated configs, launch simulation stack, start gateway profile, poll `/StatusPing` until RUNNING, and trigger Playwright trial reset automatically.
  - `ignition-gateway-harness down`: 1 command to cleanly stop all fleet and simulation containers.
  - `docker-compose.yml`: "One file creates all" combined compose file to run simulation and fleet in 1 raw `docker compose` command without flags.
- **Dual-Mode Operation**:
  - **Restore Mode**: Restores gateway configurations and projects from `.gwbk` backup archives on container initialization (`-r /restore.gwbk`).
  - **Persistent Run Mode**: Starts existing containers directly from Docker persistent named volumes (`<service_name>_data`) without re-restoring, preserving all runtime state, SQLite configurations, and user changes across container restarts.
- **Low-RAM Tuning**:
  - Wrapper memory percentage calculations disabled (`wrapper.java.initmemory.percent=0`, `wrapper.java.maxmemory.percent=0`).
  - Strict JVM limits: Initial Heap `256m`, Max Heap `-Xmx1024m`, Max Metaspace `256m`, Thread Stack `-Xss256k`.
  - Docker cgroup resource caps (`mem_limit: 1800m`, `deploy.resources.limits.memory: 1800M`).
  - SameSite cookie laxity enabled for localhost browser session persistence (`-Dignition.http.session.cookie.same-site.value=Lax`).
- **Single-Gateway Architecture**:
  - `docker-compose.yml`: Run mode using persistent volume storage (`ignition_data:/usr/local/bin/ignition/data`).
  - `docker-compose.restore.yml`: Companion restore overlay that binds `${GATEWAY_RESTORE_FILE}:/restore.gwbk:ro` and prepends `-r /restore.gwbk` to the startup command.
- **Multi-Gateway Fleet Orchestration**:
  - Automatically scans `./backups` (recursively discovering all `.gwbk` archives across NGDV Dev, Production, and Test profiles).
  - Dynamically assigns non-conflicting host ports (starting at 8100) and GAN network aliases.
  - Generates unified `docker-compose.fleet.yml` and companion `docker-compose.fleet.restore.yml` overlays.
- **Source of Truth Backup Analyzer**:
  - Automatically inspects `.gwbk` gateway backups and embedded SQLite configuration databases (`config.idb`) to extract exact expected JDBC database connections, redundancy partners, GAN remote providers, OPC-UA servers, PLC field devices, and SMTP/MQTT peripherals without modifying the gateways.
- **Peripheral Simulation Stack**:
  - Generates containerized simulation services (`docker-compose.sim.yml`) providing external dependencies expected by the backups:
    - Microsoft SQL Server 2022 (`sim-mssql` on port 1433) pre-configured with discovered database schemas (`mes`, `scada`, `prod`, etc.), user roles, sidecar database initializer (`sim-db-init`), and standard Ignition historian/audit tables.
    - Mailpit SMTP container for alarm notifications.
    - Mosquitto MQTT broker for Sparkplug B and Cirrus Link modules.
    - Mock OPC-UA & Rockwell CIP / EtherNet/IP socket responder.
  - Automatically enriches fleet containers with production DNS/network aliases (`svc.gan_aliases`) so GAN routes, redundancy peers, and client connections resolve seamlessly over Docker bridge networks.
- **Playwright Automated Trial Reset**:
  - Headless/headed Playwright automation (`src/ignition_gateway_harness/trial_reset.py`) that navigates to any or all fleet gateways, logs in with admin credentials, detects active/expired trials, and triggers the 2-hour trial reset flow.
  - Supports Edge and Perspective gateways, modal confirmation dialogs, concurrent multi-gateway execution, and periodic background daemon mode.

---

## ⚡ 1-Command Quick Start

### Spin Up Everything (1 Command)
```bash
# Start default dev stack + simulation stack + health check + automatic trial reset:
uv run ignition-gateway-harness up

# Start all 17 gateways + simulation stack:
uv run ignition-gateway-harness up --all

# Start specific profile (e.g. prod or test):
uv run ignition-gateway-harness up --profile prod
```
What this single command does automatically:
1. Generates and synchronizes `docker-compose.fleet.yml`, `docker-compose.sim.yml`, and `docker-compose.yml` ("One file creates all") preserving host port publishing.
2. Starts the peripheral simulation stack (MSSQL 2022 on :1433, Mailpit, Mosquitto, Mock OPC).
3. Starts the fleet gateway containers in the Docker bridge network.
4. Polls HTTP `/StatusPing` until gateways report healthy and `RUNNING`.
5. Runs the Playwright automated trial reset across all ready gateways.
6. Prints gateway URLs and Mailpit email sink URL (`http://localhost:8025`).

### Tear Down Everything (1 Command)
```bash
uv run ignition-gateway-harness down

# Or also delete persistent named volumes:
uv run ignition-gateway-harness down -v
```

### Or Run via Pure Docker Compose (1 Command — "One File Creates All")
```bash
# Spin up simulation stack and dev fleet profile in 1 docker command:
docker compose --profile dev up -d

# Spin up entire 17-gateway fleet and simulation stack:
docker compose --profile "*" up -d
```

---

## 🔒 100% Production Isolation Guarantee

| Protection Layer | Mechanism | Effect |
| :--- | :--- | :--- |
| **Network Isolation** | Bridge `ignition_network` (no `internal: true`) | Preserves host browser port publishing (localhost:8100) while defense-in-depth isolation is provided by `extra_hosts` blackholing and mock redirection. |
| **Email Sink Trap** | Mailpit on port 1025/8025 with SMTP aliases | Discovered corporate mail hosts (`smtp.office365.com`, `smtp.oshkoshglobal.com`, `sszsmtp`) route to Mailpit inside Docker. Zero emails reach real people. |
| **DNS Redirection** | Docker embedded DNS (`127.0.0.11`) + `extra_hosts` | External corporate DNS cannot be resolved. Discovered hostnames route to local mock containers or are blackholed to `127.0.0.1`. |
| **Port Publishing** | Inbound-only localhost port forwarding (`8100:8088`) | Host can access gateways via localhost; gateways cannot initiate outbound connections to host network. |

---

## Quick Start

### 1. Prerequisites
- Docker & Docker Compose v2+
- Python 3.12+
- `uv` package manager

### 2. Single-Gateway Harness

#### A. Initial Spin-Up (Restore from Backup)
Mount the backup file defined in `.env` and initialize the persistent volume:
```bash
docker compose -f docker-compose.yml -f docker-compose.restore.yml up -d
```

#### B. Subsequent Runs (Persistent Storage Run Mode)
Run the gateway container using existing persistent volume data without reloading the backup:
```bash
docker compose up -d
```

---

### 3. Multi-Gateway Fleet Orchestration

#### A. Generate Fleet Compose Files

**Generate Fleet with Simulation Stack and Production Network Aliases**:
```bash
uv run ignition-gateway-harness --run --restore-overlay --with-sim --low-ram
```

**Generate Standalone Peripheral Simulation Stack**:
```bash
uv run ignition-gateway-harness --generate-sim
```

**Inspect Backups as Source of Truth & Output Dependency Report**:
```bash
uv run ignition-gateway-harness --analyze
# Or export to markdown / json:
uv run ignition-gateway-harness --analyze --export-report fleet-dependencies.md
```

#### B. Running Fleet Profiles & Simulation Stack

**Start Peripheral Simulation Stack (Databases, Mailpit, MQTT, Mock OPC)**:
```bash
docker compose -f docker-compose.sim.yml up -d
```

**Restore Fleet from Backup Archives**:
```bash
docker compose -f docker-compose.fleet.yml -f docker-compose.fleet.restore.yml --profile dev up -d
```

**Run Fleet from Persistent Storage**:
```bash
docker compose -f docker-compose.fleet.yml --profile dev up -d
```

---

### 4. Automated Playwright Trial Reset

Reset 2-hour trials across running fleet gateways:
```bash
# Reset all running gateways discovered in docker-compose.fleet.yml:
uv run ignition-gateway-harness --reset-trials

# Target specific host ports or service names:
uv run ignition-gateway-harness --reset-trials --trial-targets 8100,8101,prod-fe1

# Run in headed browser mode for visual debugging:
uv run ignition-gateway-harness --reset-trials --headed

# Run continuously as a background daemon (every 105 minutes):
uv run ignition-gateway-harness --reset-trials --daemon-trials --trial-interval 105
```

---

## Fleet CLI Reference

```
usage: ignition-gateway-harness [-h] [--output OUTPUT] [--base-port BASE_PORT]
                                [--filter FILTER] [--profiles PROFILES]
                                [--dry-run] [--init-configs]
                                [--backups-dir BACKUPS_DIR] [--force]
                                [--status] [--low-ram] [--heap-max HEAP_MAX]
                                [--mem-limit MEM_LIMIT] [--mode {restore,run}]
                                [--no-restore] [--restore]
                                [--restore-overlay [RESTORE_OVERLAY]]
                                [--analyze] [--export-report EXPORT_REPORT]
                                [--sim] [--sim-output SIM_OUTPUT]
                                [--sim-init-dir SIM_INIT_DIR] [--with-sim]
                                [--reset-trials] [--trial-targets TRIAL_TARGETS]
                                [--trial-username TRIAL_USERNAME]
                                [--trial-password TRIAL_PASSWORD] [--headed]
                                [--daemon-trials] [--trial-interval TRIAL_INTERVAL]
```

### Key Options
| Flag | Description |
| :--- | :--- |
| `--mode {restore,run}` | Set harness mode (`restore` loads backups into persistent volumes; `run` runs existing volumes). |
| `--no-restore`, `--run` | Shorthand for run mode (persistent storage without `-r /restore.gwbk`). |
| `--restore` | Shorthand for restore mode (default). |
| `--restore-overlay [FILE]` | Generate companion restore overlay compose file (default: `docker-compose.fleet.restore.yml`). |
| `--low-ram`, `-l` | Apply low-RAM limits (`1024m` heap cap, Metaspace/stack caps, `1800m` container limits). |
| `--profiles`, `-p` | Filter backups by Compose profile (e.g. `dev`, `prod`, `test`). |
| `--filter`, `-f` | Filter backups by name, glob pattern, or subdirectory. |
| `--status` | Query Docker status and HTTP `/StatusPing` for all declared fleet services. |
| `--analyze` | Inspect `.gwbk` / `config.idb` SQLite databases and output source-of-truth dependency report. |
| `--export-report [FILE]` | Export backup analysis report to `.md` or `.json`. |
| `--sim` | Generate `docker-compose.sim.yml` and simulation initialization files in `sim_init/`. |
| `--with-sim` | Automatically generate peripheral stack and enrich fleet with discovered network aliases. |
| `--reset-trials` | Trigger Playwright automated login and trial reset across fleet gateways. |
| `--trial-targets [TARGETS]` | Comma-separated list of service names, ports, or URLs to reset (default: `all`). |
| `--daemon-trials` | Run trial reset continuously in background daemon loop before 2-hour expiration. |

---

## Testing

Run unit and integration tests using `uv`:

```bash
# Run entire test suite (219 passed, 11 skipped when gateways are offline):
uv run pytest

# Run specific test suites:
uv run pytest tests/test_generator.py
uv run pytest tests/test_backup_analyzer.py
uv run pytest tests/test_sim_generator.py
uv run pytest tests/test_trial_reset.py
```

---

## Production Simulation Architecture

For the complete architectural blueprint and production parity roadmap (Microsoft SQL Server 2022, Mailpit, Mosquitto, Snap7/Milo PLC simulation, Keycloak IdP, Traefik ingress), see [SIMULATE_PRODUCTION.md](SIMULATE_PRODUCTION.md).

