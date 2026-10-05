# AGENTS.md — AI Agent Guidance & Repository Rules

Welcome, Agent. This repository contains the **Ignition Gateway Harness**, an automated Docker orchestration, testing, and peripheral simulation framework for Inductive Automation's **Ignition 8.1.51 Gateway fleets**.

This guide establishes the operating rules, performance protocols, architectural gotchas, and file navigation paths to ensure you operate at maximum velocity without regressions.

---

## ⚡ Speed Rule #1: Fast Testing Protocol

> **CRITICAL**: DO NOT run the full test suite (`uv run pytest`) for routine changes or iterative development.
> The full suite takes **~90 seconds** across 258+ tests (primarily due to `tests/test_backups.py` unpacking and validating 102 individual `.gwbk` archives, which takes ~45s alone).

### Targeted Test Commands (Execute in ~2–5 seconds)

Run **only** the test suite corresponding to the module you touched:

| Component Touched | WSL / Linux Bash Command | Windows PowerShell Command | Duration |
| :--- | :--- | :--- | :--- |
| **Dynamic Gateway Appliance Engine & Lifecycle** | `uv run pytest tests/test_dynamic_lifecycle.py` | `wsl --cd "/mnt/c/Users/Colin Aten/Projects/ignition-gateway-harness" /home/wsl/.local/bin/uv run pytest tests/test_dynamic_lifecycle.py` | ~2s |
| **Generator, CLI, Port Allocator, Models** | `uv run pytest tests/test_generator.py` | `wsl --cd "/mnt/c/Users/Colin Aten/Projects/ignition-gateway-harness" /home/wsl/.local/bin/uv run pytest tests/test_generator.py` | ~2–4s |
| **Backup Analyzer, SQLite `config.idb`, XML** | `uv run pytest tests/test_backup_analyzer.py` | `wsl --cd "/mnt/c/Users/Colin Aten/Projects/ignition-gateway-harness" /home/wsl/.local/bin/uv run pytest tests/test_backup_analyzer.py` | ~2–3s |
| **Simulation Generator, MSSQL DDL, Network Aliases** | `uv run pytest tests/test_sim_generator.py` | `wsl --cd "/mnt/c/Users/Colin Aten/Projects/ignition-gateway-harness" /home/wsl/.local/bin/uv run pytest tests/test_sim_generator.py` | ~2–3s |
| **Playwright Trial Reset, Target Resolution, Daemon** | `uv run pytest tests/test_trial_reset.py` | `wsl --cd "/mnt/c/Users/Colin Aten/Projects/ignition-gateway-harness" /home/wsl/.local/bin/uv run pytest tests/test_trial_reset.py` | ~3–5s |
| **Production Isolation, DNS Redirection, Email Sink** | `uv run pytest tests/test_isolation.py` | `wsl --cd "/mnt/c/Users/Colin Aten/Projects/ignition-gateway-harness" /home/wsl/.local/bin/uv run pytest tests/test_isolation.py` | ~2–4s |
| **Single Specific Test Function** | `uv run pytest tests/test_generator.py -k <test_name>` | `wsl --cd "/mnt/c/Users/Colin Aten/Projects/ignition-gateway-harness" /home/wsl/.local/bin/uv run pytest tests/test_generator.py -k <test_name>` | ~1s |

### Environment Note
- Package manager: `uv` with Python 3.12.
- In Windows PowerShell, `uv` is installed inside WSL at `/home/wsl/.local/bin/uv`. Running bare `uv` directly in PowerShell will fail with `CommandNotFoundException`. Always prefix with `wsl --cd "/mnt/c/Users/Colin Aten/Projects/ignition-gateway-harness" /home/wsl/.local/bin/uv ...` when in PowerShell.
- **Final Verification Rule**: Run the full suite (`uv run pytest` or WSL wrapper) **only once** as a final sanity check right before reporting completion.

---

## 🧠 Critical Architecture Gotchas (Never Repeat Mistakes)

These gotchas represent hard-earned bug fixes and architectural constraints. Violating these will cause container crashes, OOM kills, or broken network connectivity.

### 1. Tanuki JVM Wrapper 21GB Balloon
- **The Problem**: Production `ignition.conf` files calculate JVM heap sizes against total host physical RAM using percentage directives (`wrapper.java.initmemory.percent=58`, `wrapper.java.maxmemory.percent=66`). When mounted inside containers on developer workstations or CI runners with 32GB–64GB RAM, the JVM balloons to **21GB+ heap**, exhausting system memory and causing instant OOM crashes.
- **The Rule**: Always disable wrapper percentage calculations by injecting:
  ```properties
  wrapper.java.initmemory.percent=0
  wrapper.java.maxmemory.percent=0
  ```
- **Low-RAM Implementation**: In `compose_builder.py`, low-RAM mode injects:
  - `wrapper.java.initmemory.percent=0`
  - `wrapper.java.maxmemory.percent=0`
  - `wrapper.java.initmemory=256`
  - `wrapper.java.maxmemory=1024` (or configured `heap_max`)
  - `-Xmx1024m`
  - `-XX:MaxMetaspaceSize=256m`
  - `-Xss256k`
  - Docker container cgroup memory limits (`mem_limit: 1800m`, `deploy.resources.limits.memory: 1800M`).

### 2. Docker Port Publishing vs. `internal: true`
- **The Problem**: Marking the Docker network as `internal: true` (`networks.ignition_network.internal: true`) suppresses all Docker host port publishing (`-p 8100:8088`). The gateway web interfaces become completely unreachable from the host browser at `http://localhost:8100`.
- **The Rule**: **NEVER set `internal: true` on the Docker bridge network.**
- **Isolation Enforcement**: 100% production isolation is achieved through defense-in-depth:
  - Standard Docker bridge `ignition_network` preserves inbound localhost port mapping.
  - DNS Redirection & Blackholing: External corporate hostnames (SMTP, MSSQL, PLC endpoints) discovered in backups are injected into container `extra_hosts` pointing to mock containers or blackholed to `127.0.0.1`.
  - Email Sink Trap: Mailpit captures all outbound emails; real emails can never leave the network.

### 3. SQL Server 2022 Password Complexity vs. Application Logins
- **The Problem**: The Microsoft SQL Server 2022 container enforces strict Windows/MSSQL password complexity policies for the `sa` account during initialization (requiring uppercase, lowercase, numbers, and symbols like `Password123!`). However, legacy Ignition backups configure application database connections using simple passwords (e.g. `password`). If created under default policy enforcement, MSSQL rejects application logins.
- **The Rule**: In T-SQL DDL generation (`sim_generator.py` / `init-databases.sql`), always provision application server logins with:
  ```sql
  CREATE LOGIN [app_user] WITH PASSWORD = N'password',
      DEFAULT_DATABASE = [master],
      CHECK_EXPIRATION = OFF,
      CHECK_POLICY = OFF;
  ```
- The `sa` account password is also normalized to `password` with `CHECK_POLICY = OFF` inside `sim-db-init` post-boot.

### 4. Playwright Sync API / Asyncio Event Loop Collision
- **The Problem**: `sync_playwright()` raises a fatal runtime error:
  `Error: It looks like you are using Playwright Sync API inside the asyncio loop. Please use the Async API instead.`
  This occurs whenever `sync_playwright()` is called from a thread where an asyncio event loop is active (e.g. under pytest-asyncio, AnyIO, or async frameworks).
- **The Rule**: In `trial_reset.py`, never invoke synchronous Playwright directly within an active asyncio event loop thread. Offload synchronous browser automation to dedicated worker threads using `ThreadPoolExecutor` or `asyncio.to_thread`.
- Fleet-wide trial resets already use `ThreadPoolExecutor(max_workers=workers)` in `TrialResetAutomator.reset_fleet()`, which maintains thread safety and enables concurrent resets across multiple gateways.

---

## 🔑 Standard Credential & Service Reference Matrix

Never guess ports or credentials. All services spun up by this harness adhere to the following canonical matrix:

| Service / Component | Host Port | In-Network Alias | Username | Password | Diagnostic / Health Command |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Ignition Dev Gateway** | `8100:8088` | `ngdv_dev_ignition` | `admin` | `password` | `curl -s http://localhost:8100/StatusPing` |
| **Ignition Fleet Gateways** | `8101`–`8116` | `<service_name>` | `admin` | `password` | `curl -s http://localhost:<port>/StatusPing` |
| **MSSQL 2022 DB** | `1433:1433` | `sim-mssql`, `mssql`, `sqlserver` | `sa`, `DEFSVCMESHEADPRD` | `password` | `docker compose exec sim-mssql /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P password -C -Q "SELECT 1"` |
| **Mailpit Sink UI** | `8025:8025` | `sim-mailpit` | *(none)* | *(none)* | `curl -s http://localhost:8025/api/v1/messages` |
| **Mailpit SMTP Trap** | `1025:1025` | `smtp.office365.com`, `smtp.oshkoshglobal.com` | *(any)* | *(any)* | Send SMTP traffic to `localhost:1025` or `sim-mailpit:1025` |
| **Mosquitto MQTT** | `1883`, `1885` | `sim-mosquitto`, `mqtt` | *(anonymous)* | *(none)* | TCP socket listener on `1883` |
| **Mock OPC / CIP** | `4840`, `44818`, `62541` | `sim-opc-plc`, `opc-mock`, PLC IPs | *(none)* | *(none)* | TCP binary handshake responder |

---

## ✅ Pre-Flight Verification Gate & Quality Standards

Before concluding any turn or declaring a task complete, you MUST execute the following 3-step verification gate:

### 1. Targeted Pytest Run (<10 Seconds)
- Run the targeted pytest command corresponding to the touched module (see Section 1).
- Ensure 100% of targeted tests pass. Report the exact test count and runtime (e.g. `85 passed in 2.3s`).

### 2. Docker Compose Syntax Validation
- If any Compose files, templates, or generators were modified, run:
  ```bash
  wsl --cd "/mnt/c/Users/Colin Aten/Projects/ignition-gateway-harness" bash -c "docker compose --profile dev config --quiet"
  ```
- Catches invalid YAML syntax, missing volume references, or broken port mappings before user interaction.

### 3. Deliverable Communication & Noise Hygiene
- **Mandatory Clickable Links**: Always link referenced files, functions, and classes using Markdown `file:///` URLs (e.g. `[generator.py::run_up_cli](file:///C:/Users/Colin%20Aten/Projects/ignition-gateway-harness/src/ignition_gateway_harness/generator.py#L750-L870)`).
- **Clean Diff Reporting**: Never paste giant 1000-line YAML dumps or massive raw logs into the chat. Provide concise diffs, clear explanations of non-obvious rationale, and exact copy-pasteable verification commands.
- **Git Hygiene**: Run `git status` to ensure no temporary scripts, `.pyc`, or debug files are left untracked.

---

## 🗺️ Repository & File Map

| Path | Purpose / Description | Primary Role |
| :--- | :--- | :--- |
| **`src/ignition_gateway_harness/__init__.py`** | Package initialization & top-level public API exports (`GatewayManager`, `GatewaySpec`, `main`, etc.). | Public API |
| **`src/ignition_gateway_harness/__main__.py`** | Package CLI entrypoint allowing execution via `python -m ignition_gateway_harness`. | CLI Entrypoint |
| **`src/ignition_gateway_harness/cli.py`** | Thin CLI entrypoint with zero business logic (`parse_args`, `deploy`, `up`, `down`, `stop`, `status`, etc.). | CLI Engine |
| **`src/ignition_gateway_harness/core/inspector.py`** | Pure in-memory SQLite `config.idb` inspection parser producing strongly-typed `GatewaySpec`. | Core Inspection |
| **`src/ignition_gateway_harness/core/orchestrator.py`** | Dynamic container lifecycle, `/StatusPing` health check polling, and peripheral orchestration. | Core Orchestration |
| **`src/ignition_gateway_harness/core/database.py`** | Idempotent T-SQL DDL generation and live login/database provisioning against MSSQL. | Core Database |
| **`src/ignition_gateway_harness/core/manager.py`** | Domain service `GatewayManager` coordinating peripheral stack, dynamic gateway deployment, and trial resets. | Domain Service |
| **`src/ignition_gateway_harness/templates/`** | Isolated template assets for Mosquitto MQTT config and mock OPC/CIP Python socket responders. | Template Assets |
| **`src/ignition_gateway_harness/generator.py`** | Fleet compose generation engine (`generate_fleet_compose`) and backward-compatibility re-exports. | Compose Generator |
| **`src/ignition_gateway_harness/models.py`** | Dataclasses for gateway configuration (`GatewayServiceConfig`). | Data Models |
| **`src/ignition_gateway_harness/config.py`** | Discovers and merges `gateway.yaml` overrides with backup metadata; handles environment scaffolding. | Configuration |
| **`src/ignition_gateway_harness/discovery.py`** | Recursively scans `backups/` for `.gwbk` files, sanitizes identifiers, and maps profile directories. | Discovery |
| **`src/ignition_gateway_harness/compose_builder.py`** | Generates Docker Compose YAML dictionaries (`fleet`, `restore overlay`, `unified`), memory limits, volume mounts, and network definitions. | Compose Engine |
| **`src/ignition_gateway_harness/backup_analyzer.py`** | Deep inspection of `.gwbk` ZIP archives and embedded SQLite `config.idb`. Discovers JDBC datasources, redundancy pairs, GAN links, OPC servers, devices, SMTP, and MQTT. | Analysis |
| **`src/ignition_gateway_harness/sim_generator.py`** | Generates simulation stack (`docker-compose.sim.yml`), T-SQL DDL (`init-databases.sql`), Mosquitto config, mock OPC scripts, and enriches fleet with network aliases. | Simulation |
| **`src/ignition_gateway_harness/trial_reset.py`** | Playwright automation to log into Ignition gateways, bypass IdP logins, handle confirmation modals, and reset the 2-hour trial timer. | Automation |
| **`src/ignition_gateway_harness/port_allocator.py`** | Assigns non-conflicting host ports starting at 8100, respecting explicit overrides. | Networking |
| **`src/ignition_gateway_harness/exceptions.py`** | Custom exception hierarchy (`FleetGeneratorError`, `PortConflictError`, `ConfigurationError`, `BackupDiscoveryError`). | Error Handling |
| **`sim_init/init-databases.sql`** | Auto-generated T-SQL script executed by `sim-db-init` to set up MSSQL databases, logins, permissions, historian tables, and alarm journals. | Simulation Asset |
| **`sim_init/mock_opc_server.py`** | Async Python server simulating OPC-UA and TCP field device sockets. | Simulation Asset |
| **`sim_init/mosquitto.conf`** | Configuration file for simulated Mosquitto MQTT broker. | Simulation Asset |
| **`tests/conftest.py`** | Shared pytest fixtures (`base_url`, `wait_for_gateway_ready`, `http_client`, `async_http_client`, readiness retries). | Test Infrastructure |
| **`tests/test_dynamic_lifecycle.py`** | Unit & integration tests for in-memory inspection, T-SQL provisioning, orchestrator status, and `GatewayManager`. | Test Suite |
| **`tests/test_generator.py`** | Unit & integration tests for CLI, compose generation, port allocation, low-RAM flags, and profile filtering. | Test Suite |
| **`tests/test_backup_analyzer.py`** | Tests for `.gwbk` archive extraction, SQLite schema parsing, and dependency report generation. | Test Suite |
| **`tests/test_sim_generator.py`** | Tests for simulation compose generation, T-SQL script generation, and fleet network alias enrichment. | Test Suite |
| **`tests/test_trial_reset.py`** | Tests for target resolution, status detection, daemon loop, and Playwright execution paths. | Test Suite |
| **`tests/test_isolation.py`** | Tests verifying 100% network isolation, `extra_hosts` blackholing, and email sink redirection. | Test Suite |
| **`tests/test_backups.py`** | Tests verifying archive integrity and schema across all 102 `.gwbk` files in `backups/` (~45s execution). | Test Suite |
| **`tests/test_fleet_live.py`** | Dynamic live integration tests querying `/StatusPing` on all active containers in `docker-compose.fleet.yml`. | Test Suite |
| **`tests/test_gateway_api.py`** | Direct HTTP integration tests verifying `/StatusPing` JSON payload structure, homepage rendering, and session cookie security. | Test Suite |
| **`tests/test_gateway_ui.py`** | Playwright end-to-end browser automation tests verifying Gateway Web UI shell navigation and IdP login redirection. | Test Suite |
| **`docker-compose.fleet.yml`** | Generated fleet compose file defining all discovered gateway services in run mode. | Docker Compose |
| **`docker-compose.fleet.restore.yml`** | Generated companion overlay mounting backup archives and adding `-r /restore.gwbk`. | Docker Compose |
| **`docker-compose.sim.yml`** | Peripheral simulation compose stack (MSSQL 2022, Mailpit, Mosquitto, Mock OPC). | Docker Compose |
| **`docker-compose.yml`** | "One file creates all" unified compose file combining simulation stack and all 17 fleet gateways. | Docker Compose |

---

## 🛠️ Living Documentation Rule (Authorized to Improve)

**Agents working in this codebase are authorized and explicitly expected to update `AGENTS.md` and `.agents/skills/ignition-harness/SKILL.md` whenever an improvement is found.**

You should update these documents when:
1. **You discover a new gotcha or edge case**: E.g., a specific module failure, Docker networking behavior, or Ignition runtime quirk.
2. **You find a faster testing shortcut**: E.g., a specific test flag or mock pattern that shaves seconds off test runs.
3. **You add or refactor capabilities**: E.g., adding a new simulated service, introducing a new CLI option, or changing configuration schemas.
4. **You optimize resource consumption**: E.g., better memory boundaries or cleaner startup synchronization.

Treat this documentation as a living contract that continuously evolves to make every successive agent faster and more reliable.
