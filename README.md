# Ignition Gateway Harness

An automated Docker orchestration and testing framework for Inductive Automation's Ignition 8.1.51 Gateway fleets.

The harness discovers `.gwbk` gateway backup archives, manages persistent volume storage, generates unified multi-gateway Docker Compose fleet architectures, isolates ports, applies low-RAM JVM and container tuning, and supports dual operating modes (initial backup restore vs. running existing persistent containers).

---

## Key Features

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
- **Automated Health & Status Check**:
  - Built-in CLI status monitor (`--status`) querying Docker container health and Ignition HTTP `/StatusPing` endpoints.

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

**Generate Fleet in Persistent Run Mode with Restore Overlay**:
```bash
# Using uv CLI
uv run ignition-gateway-harness --run --restore-overlay --low-ram
```
This generates:
- `docker-compose.fleet.yml`: Configured to run from persistent named volumes (`<service_name>_data:/usr/local/bin/ignition/data`).
- `docker-compose.fleet.restore.yml`: Companion overlay specifying the backup bind mount and `-r /restore.gwbk` command.

**Generate Fleet in Direct Restore Mode (Default)**:
```bash
uv run ignition-gateway-harness --low-ram
```

#### B. Running Fleet Profiles

**Restore a profile from backup archives**:
```bash
docker compose -f docker-compose.fleet.yml -f docker-compose.fleet.restore.yml --profile dev up -d
```

**Run already spun-up containers from persistent volumes**:
```bash
docker compose -f docker-compose.fleet.yml --profile dev up -d
```

**Stop running containers**:
```bash
docker compose -f docker-compose.fleet.yml --profile dev down
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

---

## Testing

Run unit and integration tests using `uv`:

```bash
# Run fleet generator and storage mode tests
uv run pytest tests/test_generator.py

# Run backup archive integrity tests
uv run pytest tests/test_backups.py
```

---

## Production Simulation Roadmap

For an in-depth analysis of what is still needed to achieve full production parity (databases, field PLCs, OPC-UA simulation, GAN certificate automation, Keycloak IdP, and SMTP mocks), see [SIMULATE_PRODUCTION.md](SIMULATE_PRODUCTION.md).
