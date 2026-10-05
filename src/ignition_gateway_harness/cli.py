"""Thin CLI entrypoint for Ignition Gateway Harness with zero business logic."""

import argparse
import json
import logging
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional
import urllib.request
import yaml

from ignition_gateway_harness.core.orchestrator import GatewayOrchestrator
from ignition_gateway_harness.exceptions import (
    BackupDiscoveryError,
    ConfigurationError,
    FleetGeneratorError,
    PortConflictError,
)

logger = logging.getLogger(__name__)


def flatten_arg_list(raw_items: List[str]) -> List[str]:
    """Split comma-separated arguments and strip whitespace."""
    flattened: List[str] = []
    for item in raw_items:
        for part in item.split(","):
            val = part.strip()
            if val:
                flattened.append(val)
    return flattened


def parse_args(args: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command line arguments for the harness CLI."""
    if args is None:
        raw_args = list(sys.argv[1:])
    else:
        raw_args = list(args)

    subcommand_map = {
        "trial-reset": "--reset-trials",
        "reset-trials": "--reset-trials",
        "analyze": "--analyze",
        "analyze-backups": "--analyze",
        "sim": "--generate-sim",
        "generate-sim": "--generate-sim",
        "status": "--status",
        "up": "--up",
        "start": "--up",
        "down": "--down",
        "stop": "--down",
        "deploy": "--deploy",
        "run-gateway": "--deploy",
    }
    options_with_value = {
        "-o", "--output",
        "--base-port",
        "-f", "--filter",
        "-p", "--profile", "--profiles",
        "-d", "--backups-dir",
        "--export-report",
        "--sim-output",
        "--sim-init-dir",
        "--db-type",
        "--trial-targets",
        "--trial-username",
        "--trial-password",
        "--trial-interval",
        "--wait-timeout",
        "--unified-output",
        "--heap-max",
        "--mem-limit",
        "--mode",
        "--port",
        "-b", "--backup",
    }

    normalized_args: List[str] = []
    i = 0
    while i < len(raw_args):
        token = raw_args[i]
        token_clean = token.lower().replace("_", "-")
        if token in options_with_value:
            normalized_args.append(token)
            if i + 1 < len(raw_args):
                i += 1
                normalized_args.append(raw_args[i])
        elif token_clean in subcommand_map:
            normalized_args.append(subcommand_map[token_clean])
        else:
            normalized_args.append(token)
        i += 1

    parser = argparse.ArgumentParser(
        prog="ignition-gateway-harness",
        description="Dynamic Docker orchestration and fleet manager for Ignition gateways.",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("docker-compose.fleet.yml"),
        help="Path for generated docker-compose.fleet.yml (default: docker-compose.fleet.yml)",
    )
    parser.add_argument(
        "--base-port",
        type=int,
        default=8100,
        help="Base host port for sequential auto-assignment (default: 8100)",
    )
    parser.add_argument(
        "--filter",
        "-f",
        action="append",
        default=[],
        help="Filter backups by name, path, or glob pattern",
    )
    parser.add_argument(
        "--profiles",
        "--profile",
        "-p",
        action="append",
        default=[],
        dest="profiles",
        help="Filter services by compose profile(s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Output generated compose YAML to stdout without writing to file",
    )
    parser.add_argument(
        "--init-configs",
        action="store_true",
        help="Scaffold missing gateway.yaml configuration override files next to each .gwbk",
    )
    parser.add_argument(
        "--backups-dir",
        "-d",
        type=Path,
        default=Path("backups"),
        help="Path to directory containing .gwbk backup archives (default: backups)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force overwrite existing gateway.yaml files when --init-configs is used",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Check live status, Docker health, and HTTP StatusPing for fleet containers",
    )
    parser.add_argument(
        "--low-ram",
        "-l",
        action="store_true",
        help="Apply tuned low-RAM optimizations (256m/1024m heap, disabled wrapper percentages, Metaspace caps, Docker cgroup limits)",
    )
    parser.add_argument(
        "--heap-max",
        type=str,
        default=None,
        help="Default JVM max heap limit for services (e.g. 512m, 1024m)",
    )
    parser.add_argument(
        "--mem-limit",
        type=str,
        default=None,
        help="Default Docker container memory limit (e.g. 1024M, 1800m)",
    )
    parser.add_argument(
        "--mode",
        choices=["restore", "run"],
        default=None,
        help="Harness mode: 'restore' to load backups into persistent volumes, or 'run' without restoring",
    )
    parser.add_argument(
        "--no-restore",
        "--run",
        dest="run_mode",
        action="store_true",
        help="Run existing containers from persistent storage without restoring from backups",
    )
    parser.add_argument(
        "--restore",
        dest="restore_mode",
        action="store_true",
        help="Load/restore from backup archives into persistent storage on container startup",
    )
    parser.add_argument(
        "--restore-overlay",
        type=Path,
        nargs="?",
        const=Path("docker-compose.fleet.restore.yml"),
        default=None,
        help="Also generate a companion restore overlay compose file",
    )
    parser.add_argument(
        "--analyze",
        "--analyze-backups",
        dest="analyze_mode",
        action="store_true",
        help="Inspect backup archives (.gwbk / config.idb) and output dependency report",
    )
    parser.add_argument(
        "--export-report",
        type=Path,
        default=None,
        help="Export backup analysis report to a Markdown or JSON file",
    )
    parser.add_argument(
        "--sim",
        "--generate-sim",
        dest="sim_mode",
        action="store_true",
        help="Generate docker-compose.sim.yml and simulation peripheral scripts",
    )
    parser.add_argument(
        "--sim-output",
        type=Path,
        default=Path("docker-compose.sim.yml"),
        help="Output path for simulation docker compose file",
    )
    parser.add_argument(
        "--sim-init-dir",
        type=Path,
        default=Path("sim_init"),
        help="Directory for simulation init scripts and configs",
    )
    parser.add_argument(
        "--with-sim",
        action="store_true",
        help="Automatically generate simulation peripheral stack alongside fleet compose",
    )
    parser.add_argument(
        "--db-type",
        choices=["mssql", "mysql", "timescale"],
        default="mssql",
        help="Database simulation type: 'mssql' (default), 'mysql', or 'timescale'",
    )
    parser.add_argument(
        "--reset-trials",
        "--trial-reset",
        dest="trial_reset_mode",
        action="store_true",
        help="Execute automated Playwright trial reset across fleet gateways",
    )
    parser.add_argument(
        "--trial-targets",
        type=str,
        default="all",
        help="Target gateways to reset (comma-separated service names, ports, URLs, or 'all')",
    )
    parser.add_argument(
        "--trial-username",
        type=str,
        default=None,
        help="Ignition Gateway admin username for trial reset",
    )
    parser.add_argument(
        "--trial-password",
        type=str,
        default=None,
        help="Ignition Gateway admin password for trial reset",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run browser in headed mode during trial reset (default: headless)",
    )
    parser.add_argument(
        "--daemon-trials",
        action="store_true",
        help="Run trial reset continuously in background daemon loop",
    )
    parser.add_argument(
        "--trial-interval",
        type=int,
        default=105,
        help="Interval in minutes between trial resets in daemon mode (default: 105)",
    )
    parser.add_argument(
        "--up",
        "--start",
        dest="up_mode",
        action="store_true",
        help="One-command startup: generate compose, launch peripherals and fleet, poll health, reset trials",
    )
    parser.add_argument(
        "--down",
        "--stop",
        dest="down_mode",
        action="store_true",
        help="One-command teardown: shut down all containers and clean up networks",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Target all profiles / all gateways",
    )
    parser.add_argument(
        "--wait-timeout",
        type=int,
        default=120,
        help="Timeout in seconds to wait for gateways to reach RUNNING status (default: 120)",
    )
    parser.add_argument(
        "--no-trial-reset",
        action="store_true",
        help="Skip automatic Playwright trial reset after startup",
    )
    parser.add_argument(
        "--isolate",
        dest="isolate",
        action="store_true",
        default=True,
        help="Enforce production network isolation (extra_hosts blackholing/redirection)",
    )
    parser.add_argument(
        "--no-isolate",
        dest="isolate",
        action="store_false",
        help="Disable production network isolation and extra_hosts injection",
    )
    parser.add_argument(
        "--blackhole-hosts",
        action="store_true",
        help="Explicitly blackhole all discovered external hostnames to 127.0.0.1 in extra_hosts",
    )
    parser.add_argument(
        "--generate-unified",
        "--unified",
        dest="generate_unified",
        action="store_true",
        help="Also generate a single combined docker-compose.yml file",
    )
    parser.add_argument(
        "--unified-output",
        type=Path,
        default=Path("docker-compose.yml"),
        help="Output path for unified docker compose file",
    )
    parser.add_argument(
        "-v",
        "--volumes",
        action="store_true",
        dest="down_volumes",
        help="Remove named volumes on teardown",
    )
    parser.add_argument(
        "--deploy",
        dest="deploy_mode",
        action="store_true",
        help="Dynamically deploy an individual .gwbk gateway appliance",
    )
    parser.add_argument(
        "--backup",
        "-b",
        type=Path,
        default=None,
        help="Path to individual .gwbk backup archive to deploy dynamically",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Host port for dynamic gateway deployment (default: 8100 or auto)",
    )
    parser.add_argument(
        "backup_file",
        nargs="?",
        default=None,
        type=Path,
        help="Optional positional backup file path or service name",
    )

    return parser.parse_args(normalized_args)


def check_fleet_status(compose_path: Path) -> int:
    """Check live status of fleet containers declared in compose_path."""
    if not compose_path.exists():
        print(f"Error: {compose_path} not found. Run 'ignition-fleet-generator' first.", file=sys.stderr)
        return 1

    try:
        with open(compose_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as exc:
        print(f"Error reading {compose_path}: {exc}", file=sys.stderr)
        return 1

    orchestrator = GatewayOrchestrator()
    services = data.get("services", {})
    if not services:
        print("No services found in compose file.")
        return 0

    header = f"{'SERVICE':<22} {'CONTAINER':<30} {'PORT':<6} {'DOCKER STATE':<18} {'GATEWAY STATE':<15}"
    print("=" * len(header))
    print(header)
    print("=" * len(header))

    has_errors = False
    any_running = False

    for svc_name, cfg in services.items():
        container_name = cfg.get("container_name", f"ignition-{svc_name}")
        ports = cfg.get("ports", [])
        http_port = None
        for p in ports:
            p_str = str(p)
            if ":8088" in p_str:
                try:
                    http_port = int(p_str.split(":8088")[0].split(":")[-1])
                except ValueError:
                    pass
                break

        status = orchestrator.get_gateway_status(svc_name, container_name, http_port)
        if status.docker_state == "Running":
            any_running = True
        if status.docker_state.startswith("Exited") or (status.docker_state == "Running" and status.gateway_state not in ("RUNNING", "STARTING...")):
            has_errors = True

        port_str = str(status.port) if status.port else "-"
        print(f"{svc_name:<22} {container_name:<30} {port_str:<6} {status.docker_state:<18} {status.gateway_state:<15}")

    print("=" * len(header))
    if not any_running:
        print("Note: No fleet containers are currently running.")
        print(f"To start services, run: docker compose -f {compose_path} --profile <profile> up -d")
    return 1 if has_errors else 0


def run_analyze_cli(parsed: argparse.Namespace) -> int:
    """Execute backup analysis and print or export report."""
    from ignition_gateway_harness.backup_analyzer import analyze_fleet

    backups_dir = getattr(parsed, "backups_dir", Path("backups"))
    filters = getattr(parsed, "filter", [])
    report = analyze_fleet(backups_dir, filters=filters)

    export_path = getattr(parsed, "export_report", None)
    if export_path:
        exp = Path(export_path).resolve()
        exp.parent.mkdir(parents=True, exist_ok=True)
        if exp.suffix.lower() == ".json":
            exp.write_text(report.to_json(), encoding="utf-8")
        else:
            exp.write_text(report.to_markdown(), encoding="utf-8")
        print(f"Exported backup analysis report to {exp}")
    else:
        print(report.to_markdown())

    return 0


def run_sim_cli(parsed: argparse.Namespace) -> int:
    """Execute peripheral simulation compose generation."""
    from ignition_gateway_harness.backup_analyzer import analyze_fleet
    from ignition_gateway_harness.sim_generator import write_simulation_stack

    backups_dir = getattr(parsed, "backups_dir", Path("backups"))
    filters = getattr(parsed, "filter", [])
    report = analyze_fleet(backups_dir, filters=filters)

    sim_out = getattr(parsed, "sim_output", Path("docker-compose.sim.yml"))
    sim_init = getattr(parsed, "sim_init_dir", Path("sim_init"))
    db_type = getattr(parsed, "db_type", "mssql")

    compose_path, generated_files = write_simulation_stack(
        report=report,
        output_compose=sim_out,
        sim_init_dir=sim_init,
        db_type=db_type,
    )
    print(f"Successfully generated simulation compose stack at {compose_path}:")
    for f in generated_files:
        print(f"  - {f}")
    return 0


def run_trial_reset_cli(parsed: argparse.Namespace) -> int:
    """Execute Playwright automated trial reset across fleet gateways."""
    import os
    from ignition_gateway_harness.trial_reset import (
        TrialResetAutomator,
        print_trial_results_table,
        resolve_gateway_targets,
    )

    targets_raw = getattr(parsed, "trial_targets", "all")
    targets_list = [t.strip() for t in targets_raw.split(",") if t.strip()]

    compose_path = getattr(parsed, "output", Path("docker-compose.fleet.yml"))
    targets = resolve_gateway_targets(targets_list, compose_path=compose_path)

    username = getattr(parsed, "trial_username", None) or os.getenv("IGNITION_ADMIN_USERNAME", "admin")
    password = getattr(parsed, "trial_password", None) or os.getenv("IGNITION_ADMIN_PASSWORD", "password")
    headed = getattr(parsed, "headed", False)

    automator = TrialResetAutomator(
        username=username,
        password=password,
        headless=not headed,
    )

    if getattr(parsed, "daemon_trials", False):
        interval = getattr(parsed, "trial_interval", 105)
        automator.run_daemon(targets, interval_minutes=interval)
        return 0

    results = automator.reset_fleet(targets)
    return print_trial_results_table(results)


def run_up_cli(parsed: argparse.Namespace) -> int:
    """Execute one-command startup."""
    import os
    from ignition_gateway_harness.generator import generate_fleet_compose
    from ignition_gateway_harness.trial_reset import TrialResetAutomator, print_trial_results_table

    print("=" * 65)
    print("🚀 Ignition Gateway Harness: 1-Command Startup")
    print("=" * 65)

    backups_dir = getattr(parsed, "backups_dir", Path("backups"))
    fleet_compose = getattr(parsed, "output", Path("docker-compose.fleet.yml"))
    sim_compose = getattr(parsed, "sim_output", Path("docker-compose.sim.yml"))
    unified_compose = getattr(parsed, "unified_output", Path("docker-compose.yml"))
    sim_init = getattr(parsed, "sim_init_dir", Path("sim_init"))
    dry_run = getattr(parsed, "dry_run", False)

    target_all = getattr(parsed, "all", False)
    cli_profiles = getattr(parsed, "profiles", [])
    if target_all:
        selected_profiles = None
        profile_desc = "all gateways"
    elif cli_profiles:
        selected_profiles = flatten_arg_list(cli_profiles)
        profile_desc = f"profile(s): {', '.join(selected_profiles)}"
    else:
        selected_profiles = ["dev"]
        profile_desc = "default profile: dev"

    restore_flag = True
    if getattr(parsed, "run_mode", False) or getattr(parsed, "mode", None) == "run":
        restore_flag = False
    elif getattr(parsed, "restore_mode", False) or getattr(parsed, "mode", None) == "restore":
        restore_flag = True

    print(f"[*] Generating and synchronizing isolated compose configurations ({profile_desc})...")
    try:
        yaml_content, services = generate_fleet_compose(
            backups_dir=backups_dir,
            output_path=fleet_compose,
            base_port=parsed.base_port,
            filters=parsed.filter,
            profiles=None,
            dry_run=dry_run,
            low_ram=getattr(parsed, "low_ram", False),
            default_heap_max=getattr(parsed, "heap_max", None),
            default_mem_limit=getattr(parsed, "mem_limit", None),
            restore=restore_flag,
            restore_overlay_path=getattr(parsed, "restore_overlay", None),
            with_sim=True,
            sim_compose_path=sim_compose,
            sim_init_dir=sim_init,
            isolate=getattr(parsed, "isolate", True),
            blackhole_hosts=getattr(parsed, "blackhole_hosts", False),
            generate_unified=True,
            unified_output_path=unified_compose,
            db_type=getattr(parsed, "db_type", "mssql"),
        )
    except Exception as exc:
        print(f"Error generating compose configurations: {exc}", file=sys.stderr)
        return 1

    active_gateways: List[Dict[str, Any]] = []
    source_services: Dict[str, Any] = {}
    if not dry_run and fleet_compose.exists():
        try:
            with open(fleet_compose, "r", encoding="utf-8") as f:
                source_services = yaml.safe_load(f).get("services", {})
        except Exception as e:
            logger.warning("Error reading fleet compose for health check: %s", e)
    elif dry_run:
        try:
            source_services = yaml.safe_load(yaml_content).get("services", {})
        except Exception:
            source_services = {}

    for s_name, s_def in source_services.items():
        s_profiles = s_def.get("profiles", [])
        if selected_profiles is not None and s_profiles and not any(p in selected_profiles for p in s_profiles):
            continue
        c_name = s_def.get("container_name", f"ignition-{s_name}")
        http_port = None
        for p in s_def.get("ports", []):
            p_str = str(p)
            if ":8088" in p_str:
                http_port = int(p_str.split(":8088")[0].split(":")[-1])
                break
        if http_port:
            active_gateways.append({
                "service": s_name,
                "container": c_name,
                "port": http_port,
                "url": f"http://localhost:{http_port}",
                "ready": False,
            })

    if dry_run:
        print(f"✔ [DRY RUN] Generated isolated compose configuration for {profile_desc}.")
        print(f"✔ [DRY RUN] Would start simulation stack ({sim_compose.name}).")
        print(f"✔ [DRY RUN] Would launch {len(active_gateways)} gateway containers in ignition_network.")
        for gw in active_gateways:
            print(f"    - {gw['service']:<22} -> {gw['url']}")
        print(f"✔ [DRY RUN] Would poll health on endpoints and run automated Playwright trial reset.")
        return 0

    docker_bin = shutil.which("docker")
    if not docker_bin:
        print("Error: Docker executable not found in PATH. Please install Docker or start Docker Desktop.", file=sys.stderr)
        return 1

    check_daemon = subprocess.run([docker_bin, "info"], capture_output=True, text=True, check=False)
    if check_daemon.returncode != 0:
        print(f"Error: Docker daemon is not running or accessible:\n{check_daemon.stderr}", file=sys.stderr)
        return 1

    use_unified = getattr(parsed, "generate_unified", True)
    if use_unified and unified_compose.exists():
        print(f"[*] Starting unified stack ({unified_compose.name} - {profile_desc})...")
        unified_cmd = [docker_bin, "compose", "-f", str(unified_compose)]
        if selected_profiles is None:
            unified_cmd.extend(["--profile", "*"])
        else:
            for p in selected_profiles:
                unified_cmd.extend(["--profile", p])
        unified_cmd.extend(["up", "-d"])
        res_uni = subprocess.run(unified_cmd, capture_output=True, text=True, check=False)
        if res_uni.returncode != 0:
            print(f"Error starting unified containers:\n{res_uni.stderr}", file=sys.stderr)
            return 1
        print("    ✔ Unified simulation and fleet containers launched in ignition_network")
    else:
        print(f"[*] Starting peripheral simulation stack ({sim_compose.name})...")
        sim_cmd = [docker_bin, "compose", "-f", str(sim_compose), "up", "-d"]
        res_sim = subprocess.run(sim_cmd, capture_output=True, text=True, check=False)
        if res_sim.returncode != 0:
            print(f"Error starting simulation stack:\n{res_sim.stderr}", file=sys.stderr)
            return 1
        print("    ✔ Simulation stack running")

        print(f"[*] Starting fleet gateways ({profile_desc})...")
        fleet_cmd = [docker_bin, "compose", "-f", str(fleet_compose)]
        if selected_profiles is None:
            fleet_cmd.extend(["--profile", "*"])
        else:
            for p in selected_profiles:
                fleet_cmd.extend(["--profile", p])
        fleet_cmd.extend(["up", "-d"])
        res_fleet = subprocess.run(fleet_cmd, capture_output=True, text=True, check=False)
        if res_fleet.returncode != 0:
            print(f"Error starting fleet containers:\n{res_fleet.stderr}", file=sys.stderr)
            return 1
        print("    ✔ Gateway containers launched in ignition_network")

    if not active_gateways:
        print("Note: No gateway services matched the selected profile criteria.")
        return 0

    orchestrator = GatewayOrchestrator(docker_bin=docker_bin)
    timeout_secs = getattr(parsed, "wait_timeout", 120)
    print(f"[*] Waiting for {len(active_gateways)} gateway(s) to reach RUNNING state (timeout: {timeout_secs}s)...")
    start_time = time.time()

    while time.time() - start_time < timeout_secs:
        all_ready = True
        for gw in active_gateways:
            if gw["ready"]:
                continue
            if orchestrator.check_status_ping(gw["port"]) == "RUNNING":
                gw["ready"] = True
                print(f"    ✔ [{gw['service']}] Gateway is RUNNING at {gw['url']}")
                continue
            all_ready = False
        if all_ready:
            break
        time.sleep(2)

    unready = [gw for gw in active_gateways if not gw["ready"]]
    if unready:
        print(f"\nWarning: {len(unready)} gateway(s) did not reach RUNNING within {timeout_secs}s:", file=sys.stderr)
        for gw in unready:
            print(f"  - {gw['service']} ({gw['url']})", file=sys.stderr)
    else:
        print(f"\n✔ All {len(active_gateways)} gateway(s) healthy and RUNNING!")

    if not getattr(parsed, "no_trial_reset", False):
        ready_gateways = [gw for gw in active_gateways if gw["ready"]]
        if ready_gateways:
            print("[*] Running automated Playwright trial reset across running gateways...")
            username = getattr(parsed, "trial_username", None) or os.getenv("IGNITION_ADMIN_USERNAME", "admin")
            password = getattr(parsed, "trial_password", None) or os.getenv("IGNITION_ADMIN_PASSWORD", "password")
            headed = getattr(parsed, "headed", False)

            automator = TrialResetAutomator(
                username=username,
                password=password,
                headless=not headed,
            )
            targets_to_reset = {gw["service"]: gw["url"] for gw in ready_gateways}
            try:
                results = automator.reset_fleet(targets_to_reset)
                print_trial_results_table(results)
            except Exception as exc:
                print(f"Warning: Trial reset encountered an error: {exc}", file=sys.stderr)

    print("\n" + "=" * 65)
    print("🎉 Ignition Gateway Harness is UP and ISOLATED")
    print("=" * 65)
    print(f"• Active Profile: {profile_desc}")
    print(f"• Network: Standard bridge ignition_network (host port publishing preserved)")
    print(f"• Email Trap: Mailpit Web UI available at http://localhost:8025")
    print("• Gateway Endpoints:")
    for gw in active_gateways:
        status_mark = "✔ RUNNING" if gw["ready"] else "⏳ STARTING"
        print(f"    - {gw['service']:<22} -> {gw['url']} ({status_mark})")
    print("\nTo tear down everything in 1 command:")
    print("    ignition-gateway-harness down")
    print("=" * 65)
    return 0


def run_deploy_cli(parsed: argparse.Namespace) -> int:
    """Execute dynamic on-demand deployment of a single .gwbk gateway appliance."""
    backup_path = getattr(parsed, "backup", None) or getattr(parsed, "backup_file", None)
    if not backup_path:
        print("Error: No backup file specified for deployment. Usage: ignition-gateway-harness deploy <path_to_gwbk>", file=sys.stderr)
        return 1

    bpath = Path(backup_path).resolve()
    if not bpath.exists() or not bpath.is_file():
        print(f"Error: Backup file not found: {bpath}", file=sys.stderr)
        return 1

    from ignition_gateway_harness.core.manager import GatewayManager

    manager = GatewayManager()
    port = getattr(parsed, "port", None)
    low_ram = getattr(parsed, "low_ram", True)
    timeout_secs = getattr(parsed, "wait_timeout", 120)
    auto_trial = not getattr(parsed, "no_trial_reset", False)
    dry_run = getattr(parsed, "dry_run", False)
    restore_flag = True
    if getattr(parsed, "run_mode", False) or getattr(parsed, "mode", None) == "run":
        restore_flag = False

    print("=" * 65)
    print("🚀 Dynamic Ignition Gateway Appliance Deployment")
    print("=" * 65)
    print(f"• Backup:      {bpath.name}")
    print(f"• Target Port: {port if port else 'auto-assigned'}")
    print(f"• Low-RAM:     {low_ram}")
    print(f"• Restore:     {restore_flag}")
    print("=" * 65)

    if dry_run:
        from ignition_gateway_harness.core.inspector import inspect_backup
        spec = inspect_backup(bpath)
        print(f"✔ [DRY RUN] Would deploy {spec.service_name} from {bpath.name}")
        print(f"✔ [DRY RUN] Discovered databases: {sorted(list(spec.database_names))}")
        print(f"✔ [DRY RUN] Discovered users: {sorted(list(spec.database_users))}")
        return 0

    try:
        deployed = manager.deploy_gateway(
            backup_path=bpath,
            port=port,
            low_ram=low_ram,
            restore=restore_flag,
            wait_ready=True,
            timeout_secs=timeout_secs,
            auto_reset_trial=auto_trial,
        )
        print("\n✔ Gateway appliance deployed successfully!")
        print(f"• Service:   {deployed.service_name}")
        print(f"• Container: {deployed.container_name}")
        print(f"• URL:       {deployed.url}")
        print(f"• Status:    {'✔ RUNNING' if deployed.is_ready else '⏳ STARTING/OFFLINE'}")
        print(f"\nTo stop this gateway: ignition-gateway-harness stop {deployed.service_name}")
        return 0
    except Exception as exc:
        print(f"Error deploying gateway: {exc}", file=sys.stderr)
        return 1


def run_down_cli(parsed: argparse.Namespace) -> int:
    """Execute one-command teardown or stop a single gateway."""
    target_svc = getattr(parsed, "backup_file", None)
    if target_svc and str(target_svc).lower() not in ("all", "fleet"):
        from ignition_gateway_harness.core.manager import GatewayManager
        manager = GatewayManager()
        svc_str = str(target_svc).strip()
        print(f"[*] Stopping gateway appliance '{svc_str}'...")
        stopped = manager.stop_gateway(svc_str, remove_volumes=getattr(parsed, "down_volumes", False))
        if stopped:
            print(f"✔ Gateway '{svc_str}' stopped and removed.")
        else:
            print(f"Note: Could not stop gateway '{svc_str}' (may not be running).", file=sys.stderr)
        return 0

    print("=" * 65)
    print("🛑 Ignition Gateway Harness: 1-Command Teardown")
    print("=" * 65)

    fleet_compose = getattr(parsed, "output", Path("docker-compose.fleet.yml"))
    sim_compose = getattr(parsed, "sim_output", Path("docker-compose.sim.yml"))
    unified_compose = getattr(parsed, "unified_output", Path("docker-compose.yml"))
    remove_vols = getattr(parsed, "down_volumes", False)

    docker_bin = shutil.which("docker")
    if not docker_bin:
        print("Note: Docker executable not found in PATH.", file=sys.stderr)
        return 0

    down_args = ["down"]
    if remove_vols:
        down_args.append("-v")

    if unified_compose.exists():
        print(f"[*] Stopping containers from {unified_compose.name}...")
        subprocess.run([docker_bin, "compose", "-f", str(unified_compose)] + down_args, check=False)

    if fleet_compose.exists():
        print(f"[*] Stopping fleet containers ({fleet_compose.name})...")
        subprocess.run([docker_bin, "compose", "-f", str(fleet_compose)] + down_args, check=False)

    if sim_compose.exists():
        print(f"[*] Stopping simulation stack ({sim_compose.name})...")
        subprocess.run([docker_bin, "compose", "-f", str(sim_compose)] + down_args, check=False)

    print("✔ All fleet and simulation containers and networks have been stopped.")
    return 0


def main(args: Optional[List[str]] = None) -> int:
    """Main CLI entrypoint."""
    parsed = parse_args(args)
    if getattr(parsed, "status", False):
        return check_fleet_status(parsed.output)

    if getattr(parsed, "deploy_mode", False) or (getattr(parsed, "backup", None) and not getattr(parsed, "up_mode", False)):
        return run_deploy_cli(parsed)

    if getattr(parsed, "analyze_mode", False):
        return run_analyze_cli(parsed)

    if getattr(parsed, "sim_mode", False):
        return run_sim_cli(parsed)

    if getattr(parsed, "trial_reset_mode", False):
        return run_trial_reset_cli(parsed)

    if getattr(parsed, "up_mode", False):
        if getattr(parsed, "backup", None) or (getattr(parsed, "backup_file", None) and str(getattr(parsed, "backup_file")).endswith(".gwbk")):
            return run_deploy_cli(parsed)
        return run_up_cli(parsed)

    if getattr(parsed, "down_mode", False):
        return run_down_cli(parsed)

    if getattr(parsed, "run_mode", False) and getattr(parsed, "restore_mode", False):
        print("Error: Cannot specify both --run/--no-restore and --restore flags simultaneously.", file=sys.stderr)
        return 1

    if getattr(parsed, "mode", None) == "run" and getattr(parsed, "restore_mode", False):
        print("Error: Conflicting mode arguments: --mode run and --restore.", file=sys.stderr)
        return 1

    if getattr(parsed, "mode", None) == "restore" and getattr(parsed, "run_mode", False):
        print("Error: Conflicting mode arguments: --mode restore and --run/--no-restore.", file=sys.stderr)
        return 1

    from ignition_gateway_harness.generator import generate_fleet_compose

    restore_flag = True
    if getattr(parsed, "run_mode", False) or getattr(parsed, "mode", None) == "run":
        restore_flag = False
    elif getattr(parsed, "restore_mode", False) or getattr(parsed, "mode", None) == "restore":
        restore_flag = True

    try:
        yaml_content, services = generate_fleet_compose(
            backups_dir=parsed.backups_dir,
            output_path=parsed.output,
            base_port=parsed.base_port,
            filters=parsed.filter,
            profiles=parsed.profiles,
            dry_run=parsed.dry_run,
            init_configs=parsed.init_configs,
            force_init=parsed.force,
            low_ram=getattr(parsed, "low_ram", False),
            default_heap_max=getattr(parsed, "heap_max", None),
            default_mem_limit=getattr(parsed, "mem_limit", None),
            restore=restore_flag,
            restore_overlay_path=getattr(parsed, "restore_overlay", None),
            with_sim=getattr(parsed, "with_sim", False),
            sim_compose_path=getattr(parsed, "sim_output", None),
            sim_init_dir=getattr(parsed, "sim_init_dir", None),
            isolate=getattr(parsed, "isolate", True),
            blackhole_hosts=getattr(parsed, "blackhole_hosts", False),
            generate_unified=getattr(parsed, "generate_unified", False),
            unified_output_path=getattr(parsed, "unified_output", None),
            db_type=getattr(parsed, "db_type", "mssql"),
        )

        if not parsed.dry_run:
            mode_desc = "restore mode" if restore_flag else "persistent storage run mode"
            print(
                f"Successfully generated {parsed.output} ({mode_desc}) with {len(services)} gateway services."
            )
            if getattr(parsed, "restore_overlay", None):
                print(f"Successfully generated restore overlay at {parsed.restore_overlay}.")
        return 0
    except (FleetGeneratorError, ConfigurationError, PortConflictError, BackupDiscoveryError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Unexpected error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
