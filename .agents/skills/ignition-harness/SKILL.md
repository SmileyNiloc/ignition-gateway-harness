---
name: ignition-harness
description: Operational runbooks, architecture patterns, and automation workflows for the Ignition Gateway Harness fleet testing and simulation toolkit.
---

# Ignition Gateway Harness — Operational Runbooks & Skill Guide

This skill guide provides operational runbooks, architecture patterns, configuration rules, and troubleshooting procedures for working with the **Ignition Gateway Harness** (`ignition-gateway-harness`).

---

## 📖 Runbook 1: Working with Gateway Overrides (`gateway.yaml`)

### 1.1 How Overrides Merge with Discovery (`config.py`)
The harness follows a hierarchical configuration pattern:
1. **Backup Discovery (`discovery.py`)**: The harness recursively scans `./backups` for `.gwbk` archives. Each archive automatically derives a base service name, system name, and default compose profile based on its folder path (e.g. `backups/dev/gateway.gwbk` -> profile `dev`).
2. **Override Discovery (`config.py`)**: The harness checks for a `gateway.yaml` file adjacent to the backup archive. If found, `merge_service_config()` merges the YAML mapping on top of the discovered defaults.
3. **CLI Arguments (`generator.py`)**: Global flags passed at runtime (such as `--low-ram`, `--heap-max`, `--mem-limit`, or `--mode`) apply default limits across all services unless overridden by a service's `gateway.yaml`.

### 1.2 Supported Keys in `gateway.yaml`

| Key | Accepted Aliases | Types | Default / Behavior | Description |
| :--- | :--- | :--- | :--- | :--- |
| `service_name` | - | string | Sanitized backup filename | Unique service name in Docker Compose. |
| `container_name` | `name` | string | `ignition-<service_name>` | Explicit Docker container name. |
| `hostname` | - | string | `<service_name>` | Container hostname on `ignition_network`. |
| `system_name` | - | string | Discovered system name | Ignition Gateway System Name (`GATEWAY_SYSTEM_NAME`). |
| `image` | - | string | `inductiveautomation/ignition:8.1.51` | Base Ignition Docker image. |
| `ports` / `port` | - | list / string / int | Auto-assigned from 8100 | Port mapping (`"8100:8088"` or list `["8100:8088", "8043:8043"]`). |
| `http_port` | - | int / string | None | Dedicated HTTP port shortcut (maps `<http_port>:8088`). |
| `https_port` | - | int / string | None | Dedicated HTTPS port shortcut (maps `<https_port>:8043`). |
| `gan_port` | - | int / string | None | Dedicated GAN port shortcut (maps `<gan_port>:8060`). |
| `heap_max` | `max_memory`, `jvm_max_memory`, `heap_limit` | string | `"1024m"` (or `"1024m"` in low-RAM) | JVM `-Xmx` ceiling (e.g. `"1024m"`, `"2048m"`). |
| `mem_limit` | `memory_limit`, `mem_ceiling` | string | `"1800M"` in low-RAM | Docker cgroup container limit (e.g. `"1800M"`, `"2G"`). |
| `low_ram` | `low_mem`, `compact` | boolean | `false` (or CLI `--low-ram`) | Enables low-RAM tuning (disables wrapper percents, caps heap, metaspace, stack). |
| `mode` / `restore` | `load_from_backup`, `restore_backup`, `from_backup` | string / boolean | `restore: true` | Operating mode: `"restore"` loads `.gwbk` via `-r /restore.gwbk`; `"run"` starts persistent volume without restoring. |
| `data_volume` | `volume_name`, `persistent_volume` | string | `<service_name>_data` | Persistent Docker named volume mounted at `/usr/local/bin/ignition/data`. |
| `gan_aliases` | `network_aliases`, `aliases`, `gan_alias` | list[string] | `[<service_name>]` | Hostnames and network aliases for GAN routing on `ignition_network`. |
| `extra_hosts` | - | list / dict | `[]` | DNS mappings (`hostname:ip`) for DNS redirection or blackholing to `127.0.0.1`. |
| `jvm_args` | - | list[string] | `[]` | Extra JVM options appended to container command. |
| `command` | - | list[string] / str | Auto-generated | Replaces the entire container command array. |
| `environment` | `env` | dict / list | Standard defaults | Additional environment variables merged with base defaults. |
| `profiles` | `profile` | list[string] / str | Directory name (e.g. `dev`) | Compose profiles (e.g. `["dev"]`, `["prod"]`). |
| `volumes` | - | list | `[]` | Additional volume or bind mounts. |

#### Raw / Pass-Through Compose Keys
Any unrecognized top-level keys in `gateway.yaml` (e.g. `restart`, `labels`, `deploy`, `depends_on`, `dns`, `healthcheck`) are automatically captured in `raw_overrides` and passed directly into the service definition in the generated Docker Compose file.

### 1.3 Example Configurations

**Dev Low-RAM Compact Gateway (`backups/dev/gateway.yaml`)**:
```yaml
service_name: dev-hmi
heap_max: 512m
mem_limit: 1200M
low_ram: true
ports:
  - "8100:8088"
environment:
  GATEWAY_ADMIN_PASSWORD: password
profiles:
  - dev
```

**Production Node with GAN Aliases & Custom DB Redirection (`backups/prod/gateway.yaml`)**:
```yaml
service_name: prod-scada-node1
hostname: scada-node1
heap_max: 2048m
mem_limit: 3500M
gan_aliases:
  - scada-node1.corp.internal
  - prod-gateway-redundant
extra_hosts:
  - "prod-sql-cluster.corp.internal:127.0.0.1"
  - "historian-db.corp.internal:127.0.0.1"
ports:
  - "8110:8088"
  - "8060:8060"
profiles:
  - prod
```

### 1.4 Scaffolding New Overrides
To scaffold template `gateway.yaml` files next to all `.gwbk` archives that do not yet have one:
```bash
uv run ignition-gateway-harness --init-configs
```
To overwrite existing files with fresh scaffolding:
```bash
uv run ignition-gateway-harness --init-configs --force
```

---

## 🗄️ Runbook 2: Extending the Simulation Stack (`sim_generator.py`)

### 2.1 Peripheral Architecture Overview
The peripheral simulation stack (`docker-compose.sim.yml`) satisfies all external dependencies discovered across gateway backups without modifying the backup files themselves:

| Service | Image / Role | Ports | Key Responsibility |
| :--- | :--- | :--- | :--- |
| **`sim-mssql`** | `mcr.microsoft.com/mssql/server:2022-latest` | `1433:1433` | Microsoft SQL Server 2022 providing MES, SCADA, and historian databases. |
| **`sim-db-init`** | `mcr.microsoft.com/mssql/server:2022-latest` | None | Ephemeral sidecar container executing `sim_init/init-databases.sql` via `sqlcmd18` upon MSSQL readiness. |
| **`sim-mailpit`** | `axllent/mailpit:latest` | `1025:1025`, `8025:8025` | SMTP sink capturing all alarm notification emails. Zero emails leak to real servers. |
| **`sim-mosquitto`** | `eclipse-mosquitto:2` | `1883:1883`, `1885:1885`, `9001:9001` | MQTT broker for Sparkplug B and Cirrus Link modules (anonymous connections enabled). |
| **`sim-opc-plc`** | `python:3.12-slim` | `4840:4840`, `49320:49320`, `62541:62541`, `44818:44818` | Async Python server simulating OPC-UA binary (HEL/ACK) and Rockwell EtherNet/IP CIP (RegisterSession) handshakes. |

#### Supported Database Types (`--db-type`)
While Microsoft SQL Server 2022 is the default (`--db-type mssql`), `sim_generator.py` also supports:
- `--db-type mysql`: Spins up `mysql:8.0` on port 3306 with auto-provisioned users, databases, and historian tables via `docker-entrypoint-initdb.d`.
- `--db-type timescale`: Spins up `timescale/timescaledb:latest-pg15` on port 5432 with schema initialization.

### 2.2 T-SQL DDL Generation & Execution (`sim-db-init`)
When generating the simulation stack:
1. `backup_analyzer.py` inspects the embedded `config.idb` SQLite databases in all backups, discovering target database names (`mes`, `scada`, `prod`) and connection usernames.
2. `sim_generator.py::generate_init_sql()` outputs `sim_init/init-databases.sql`.
3. **Database Creation**: Creates databases with case-insensitive collation (`SQL_Latin1_General_CP1_CI_AS`) and simple recovery mode.
4. **Login Provisioning**:
   - Creates/alters server logins with password `password`, `DEFAULT_DATABASE = [master]`, and `CHECK_EXPIRATION = OFF, CHECK_POLICY = OFF;`.
   - Normalizes `sa` password to `password` with `CHECK_POLICY = OFF`.
5. **Permissions**: Maps users to each database with `db_owner` role.
6. **Historian & Alarm Journal Tables**: Pre-creates standard Ignition schema tables:
   - Tag Historian: `sqlth_drv`, `sqlth_tables`, `sqlth_te`, `sqlth_sce`
   - Alarm Journal: `alarm_events`, `alarm_event_data`
   - Audit Log: `audit_events` (plus synonym `AUDIT_EVENTS`)

### 2.3 How to Add a New Simulated Service
To add a new simulated dependency (e.g. Keycloak IdP, mock REST server, or Redis):
1. Open `src/ignition_gateway_harness/sim_generator.py`.
2. Add the service definition dictionary to `build_sim_compose_dict()`.
3. If the service requires init files (e.g. configs, SQL scripts), add generation functions in `sim_generator.py` and write them to `sim_init_dir` in `write_simulation_stack()`.
4. Ensure the service attaches to `ignition_network`.
5. Add production DNS aliases to the service in `enrich_fleet_with_discovered_aliases()`.
6. Add unit tests to `tests/test_sim_generator.py`.

---

## ⏱️ Runbook 3: Playwright Trial Reset Automation (`trial_reset.py`)

### 3.1 Overview & Capabilities
Ignition gateways in trial mode operate on a 2-hour countdown. When the trial expires, tag execution, communication drivers, and project clients halt.
The `TrialResetAutomator` uses Playwright to automate logging into the gateway web UI and resetting the trial timer.

Key capabilities:
- **Headless / Headed Modes**: Runs headless by default; supports `--headed` for visual debugging.
- **Form Login Automation**: Automatically fills Ignition Gateway login forms using credentials (`admin` / `password`).
- **Modal Confirmation Handling**: Detects and confirms trial reset confirmation dialogs.
- **Perspective Route Handling**: Automatically redirects back to Gateway Web Home if routed to a Perspective project.
- **Concurrent Fleet Execution**: Resets multiple gateways concurrently using `ThreadPoolExecutor`.
- **Daemon Mode**: Continuously runs reset cycles on an interval (default: every 105 minutes).

### 3.2 Target Resolution
The CLI flag `--trial-targets` accepts flexible descriptors:
```bash
# Reset all running gateways found in docker-compose.fleet.yml:
uv run ignition-gateway-harness --reset-trials

# Target specific host ports:
uv run ignition-gateway-harness --reset-trials --trial-targets 8100,8101

# Target specific Compose service names:
uv run ignition-gateway-harness --reset-trials --trial-targets dev-fe1,prod-gateway

# Target custom URLs:
uv run ignition-gateway-harness --reset-trials --trial-targets http://localhost:8088
```

### 3.3 Thread Safety & Asyncio Protection
`sync_playwright()` cannot run on a thread that has an active asyncio event loop.
- **Single Gateway**: `TrialResetAutomator.reset_gateway(name, url)` automatically checks `asyncio.get_running_loop()`. If an active event loop is detected on the current thread, it delegates execution to a clean worker thread via `ThreadPoolExecutor(max_workers=1)`.
- **Fleet Resets**: `TrialResetAutomator.reset_fleet()` always offloads individual gateway reset calls across concurrent worker threads inside a `ThreadPoolExecutor(max_workers=workers)`.
- When integrating `TrialResetAutomator` into an async runtime or test, invoke via `asyncio.to_thread(automator.reset_gateway, name, url)` or `automator.reset_fleet()`.

### 3.4 Running as a Daemon
To keep fleet gateways alive indefinitely during development:
```bash
uv run ignition-gateway-harness --reset-trials --daemon-trials --trial-interval 105
```

---

## 🚀 Runbook 4: 1-Command Orchestration (`generator.py`)

### 4.1 Spin Up Fleet & Simulation Stack (`up`)
The `up` command is the primary entry point to launch an isolated environment in a single step:

```bash
# Launch default 'dev' profile + simulation stack + health check + trial reset:
uv run ignition-gateway-harness up

# Launch all gateways across all profiles (Dev, Prod, Test):
uv run ignition-gateway-harness up --all

# Launch specific profile:
uv run ignition-gateway-harness up --profile prod

# Dry-run plan without launching Docker containers:
uv run ignition-gateway-harness up --dry-run

# Launch with custom health check timeout (default: 120s):
uv run ignition-gateway-harness up --wait-timeout 180

# Skip automatic post-boot trial reset:
uv run ignition-gateway-harness up --no-trial-reset
```

#### What `up` executes under the hood:
1. **Analyze & Sync**: Discovers backups, analyzes dependencies, generates `docker-compose.sim.yml`, `docker-compose.fleet.yml`, and `docker-compose.yml` ("One file creates all").
2. **Start Simulation Stack & Fleet**: Launches containers via `docker-compose.yml` (MSSQL, Mailpit, Mosquitto, Mock OPC, plus targeted fleet profile).
3. **Poll Gateway Health**: Polls `http://localhost:<port>/StatusPing` on each gateway until the response JSON indicates `state == "RUNNING"` (or `--wait-timeout` expires).
4. **Automated Trial Reset**: Automatically triggers Playwright trial reset across all ready gateways.
5. **Summary Output**: Prints reachable gateway URLs, status indicators, and the Mailpit email sink URL (`http://localhost:8025`).

### 4.2 Teardown (`down`)
To cleanly shut down all running containers, remove networks, and stop services:
```bash
uv run ignition-gateway-harness down

# To also delete named persistent data volumes (clean slate):
uv run ignition-gateway-harness down -v
```

### 4.3 Pure Docker Compose Execution (One File Creates All)
Because `docker-compose.yml` is the combined unified file, standard `docker compose` commands work directly without passing `-f`:
```bash
# Spin up simulation stack and dev profile:
docker compose --profile dev up -d

# Spin up entire fleet and simulation stack:
docker compose --profile "*" up -d

# Check live logs:
docker compose logs -f
```

---

## 🔍 Runbook 5: Live Diagnostic & Service Verification

When testing or troubleshooting running gateways and peripherals, use these verified diagnostic patterns:

### 5.1 Gateway HTTP Health & State Inspection
Ignition provides `/StatusPing` which returns an immediate JSON status payload without requiring authentication:
```bash
# Query dev gateway health (port 8100):
curl -s http://localhost:8100/StatusPing | jq .

# Expected JSON structure:
# {
#   "state": "RUNNING",
#   "edition": "standard"
# }
```
If `state` is `"STARTING"`, the gateway is still deploying modules or restoring projects from `.gwbk`. Wait 15–30s before querying again.

### 5.2 Direct MSSQL Database & Historian Inspection
To verify database schemas, tables, or records created by `sim-db-init`:
```bash
# List all databases:
docker compose exec sim-mssql /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P password -C -Q "SELECT name FROM sys.databases;"

# Query standard Ignition historian driver table:
docker compose exec sim-mssql /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P password -C -Q "SELECT * FROM [prod].[dbo].[sqlth_drv];"

# Check active connections:
docker compose exec sim-mssql /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P password -C -Q "SELECT DB_NAME(dbid) as db, COUNT(dbid) as connections FROM sys.sysprocesses WHERE dbid > 0 GROUP BY dbid;"
```

### 5.3 Mailpit Email Sink Verification in Tests
To programmatically verify that an Ignition alarm notification email was dispatched and intercepted:
```python
import httpx

# Mailpit REST API endpoint on host port 8025:
response = httpx.get("http://localhost:8025/api/v1/messages")
data = response.json()
total_emails = data["total"]
latest_message = data["messages"][0] if total_emails > 0 else None

# Assert that real alarm email was safely trapped:
assert total_emails > 0
assert "Alarm" in latest_message["Subject"]
```

### 5.4 Mosquitto MQTT & Mock OPC Socket Verification
```bash
# Check Mosquitto MQTT broker responsiveness (ports 1883, 1885):
nc -zv localhost 1883

# Check Mock OPC-UA binary listener (ports 4840, 62541):
nc -zv localhost 62541
```

---

## 🎭 Runbook 6: Playwright Visual Debugging & Artifact Diagnostics

When trial resets or Gateway Web UI navigation fail, headless execution can hide UI modals, banner changes, or IdP redirects.

### 6.1 Headed Debugging
Run browser automation with `--headed` to visually observe browser actions in real-time:
```bash
uv run ignition-gateway-harness trial-reset --target 8100 --headed
```

### 6.2 Automatic Artifact Capture on Error
In custom Playwright test suites or automations, capture failure artifacts (screenshots and page DOM HTML) to diagnose issues quickly:
```python
from pathlib import Path
from playwright.sync_api import Page

def capture_failure_artifact(page: Page, artifact_name: str, out_dir: Path = Path(".artifacts")):
    out_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = out_dir / f"{artifact_name}.png"
    html_path = out_dir / f"{artifact_name}.html"
    page.screenshot(path=str(screenshot_path), full_page=True)
    html_path.write_text(page.content(), encoding="utf-8")
    print(f"Captured failure artifacts at {screenshot_path}")
```

### 6.3 Handling Common Gateway Modals
- **License / Trial Banner**: Located at `div[class*='trial-banner']` or link text `Reset Trial`.
- **Session Timeout Confirmation**: Handled by clicking buttons with text matching `Stay Logged In` or `Dismiss`.
- **First-Time Password Change**: Avoided by setting `GATEWAY_ADMIN_PASSWORD=password` during initial compose build.

---

## 🧩 Runbook 7: Dynamic Gateway Appliance API (`GatewayManager`)

The harness provides a domain service API (`GatewayManager`) for on-demand gateway deployments following the Docker Appliance pattern.

### 7.1 Programmatic Appliance Workflow
```python
from ignition_gateway_harness import GatewayManager, inspect_backup

# 1. Initialize manager
manager = GatewayManager()

# 2. Start simulation stack if not running (MSSQL 2022, Mailpit, Mosquitto, Mock OPC):
manager.ensure_peripherals()

# 3. Deploy gateway dynamically from backup:
gateway = manager.deploy_gateway(
    backup_path="backups/AllGatewayBackups_10-01-2026/NGDV_Dev_Ignition-backup-20261001-1038.gwbk",
    port=8100,
    low_ram=True,
    wait_ready=True,
)

# 4. Query live health and container state:
status = manager.get_gateway_status(gateway.service_name)
print(f"Status: {status.docker_state} / {status.gateway_state}")

# 5. Trigger automated Playwright trial reset:
manager.reset_gateway_trial(gateway.service_name)

# 6. Teardown specific gateway:
manager.stop_gateway(gateway.service_name)
```

### 7.2 In-Memory Backup Inspection (`GatewaySpec`)
Use `inspect_backup` for fast zero-disk inspection using Python SQLite `conn.deserialize()`:
```python
from ignition_gateway_harness import inspect_backup

spec = inspect_backup("backups/.../backup.gwbk")
print(f"Service: {spec.service_name}")
print(f"Databases: {spec.database_names}")
print(f"Users: {spec.database_users}")
print(f"OPC Endpoints: {spec.opc_endpoints}")
```

### 7.3 Dynamic Gateway Appliance CLI
Deploy individual `.gwbk` archives dynamically directly from the CLI without generating static fleet YAML:
```bash
# 1. Deploy single gateway on auto-assigned port (starts simulation stack if needed):
uv run ignition-gateway-harness deploy backups/AllGatewayBackups_10-01-2026/NGDV_Dev_Ignition-backup-20261001-1038.gwbk

# 2. Deploy with explicit port and low-RAM boundaries:
uv run ignition-gateway-harness deploy backups/.../gateway.gwbk --port 8105 --low-ram

# 3. Dry-run inspection:
uv run ignition-gateway-harness deploy backups/.../gateway.gwbk --dry-run

# 4. Stop and remove specific dynamically deployed gateway container:
uv run ignition-gateway-harness stop ngdv_dev_ignition
```

### 7.4 Background Trial Reset Daemon Registration
Gateways deployed via `GatewayManager` are automatically registered with the background trial reset daemon:
```python
# Register custom target
manager.register_trial_gateway("custom_gw", "http://localhost:8105")

# Start background daemon worker (executes resets every 105 minutes)
manager.start_trial_daemon(interval_minutes=105)

# Stop daemon worker
manager.stop_trial_daemon()
```

---

## 🛠️ Living Documentation Directive (Authorized to Improve)

**This skill guide is a living repository document.**

Agents and engineers are authorized and explicitly expected to update `SKILL.md` and `AGENTS.md` whenever changes will be an improvement:
- A new runbook or operational procedure is established.
- Supported `gateway.yaml` keys, aliases, or defaults are added or changed.
- New simulation services (e.g. Keycloak, Redis) or database backends are added to `sim_generator.py`.
- New CLI options or orchestration commands are implemented.
- Faster test commands, flags, or test bypasses are discovered.
- Troubleshooting workflows or gotcha resolutions are refined.

