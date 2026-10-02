"""CLI and core orchestration for the Ignition gateway fleet compose generator."""

import argparse
from pathlib import Path
import sys
from typing import List, Optional, Tuple
import yaml

from ignition_gateway_harness.compose_builder import (
    build_fleet_compose_dict,
    build_fleet_restore_overlay_dict,
    render_compose_yaml,
)
from ignition_gateway_harness.config import (
    merge_service_config,
    scaffold_gateway_yaml,
)
from ignition_gateway_harness.discovery import (
    find_all_gwbk_files,
    find_gateway_yaml,
    matches_filter,
    matches_profiles,
)
from ignition_gateway_harness.exceptions import (
    BackupDiscoveryError,
    ConfigurationError,
    FleetGeneratorError,
    PortConflictError,
)
from ignition_gateway_harness.models import GatewayServiceConfig
from ignition_gateway_harness.port_allocator import allocate_ports


def parse_args(args: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command line arguments for the fleet generator CLI."""
    parser = argparse.ArgumentParser(
        prog="ignition-gateway-harness",
        description="Scan backups directory and generate a unified docker-compose.fleet.yml for Ignition gateways.",
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
        help="Filter backups by name, path, or glob pattern (can be specified multiple times or comma-separated)",
    )
    parser.add_argument(
        "--profiles",
        "--profile",
        "-p",
        action="append",
        default=[],
        dest="profiles",
        help="Filter services by compose profile(s) (can be specified multiple times or comma-separated)",
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
        help="Apply tuned low-RAM optimizations (512MB heap cap, disabled wrapper percentages, Metaspace/stack caps, Docker cgroup limits)",
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
        help="Default Docker container memory limit (e.g. 1024M, 1200M)",
    )
    parser.add_argument(
        "--mode",
        choices=["restore", "run"],
        default=None,
        help="Harness mode: 'restore' to load backups into persistent volumes (default), or 'run' to run already spun-up containers from persistent volumes without restoring",
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
        help="Load/restore from backup archives into persistent storage on container startup (default)",
    )
    parser.add_argument(
        "--restore-overlay",
        type=Path,
        nargs="?",
        const=Path("docker-compose.fleet.restore.yml"),
        default=None,
        help="Also generate a companion restore overlay compose file (default: docker-compose.fleet.restore.yml)",
    )

    return parser.parse_args(args)


def flatten_arg_list(raw_items: List[str]) -> List[str]:
    """Split comma-separated arguments and strip whitespace."""
    flattened: List[str] = []
    for item in raw_items:
        for part in item.split(","):
            val = part.strip()
            if val:
                flattened.append(val)
    return flattened


def generate_fleet_compose(
    backups_dir: Path | str = Path("backups"),
    output_path: Path | str = Path("docker-compose.fleet.yml"),
    base_port: int = 8100,
    filters: Optional[List[str]] = None,
    profiles: Optional[List[str]] = None,
    dry_run: bool = False,
    init_configs: bool = False,
    force_init: bool = False,
    low_ram: bool = False,
    default_heap_max: Optional[str] = None,
    default_mem_limit: Optional[str] = None,
    restore: bool = True,
    restore_overlay_path: Optional[Path | str] = None,
) -> Tuple[str, List[GatewayServiceConfig]]:
    """Scan backups, scaffold configs if requested, merge overrides, allocate ports, and generate compose YAML.

    Returns (yaml_content, list_of_service_configs).
    """
    backups_path = Path(backups_dir).resolve()
    out_path = Path(output_path).resolve()

    # 1. Discover all .gwbk files
    gwbk_files = find_all_gwbk_files(backups_path)
    if not gwbk_files:
        raise BackupDiscoveryError(f"No .gwbk archives found in {backups_path}")

    # 2. Scaffold missing configs if requested (only when not dry-run)
    if init_configs and not dry_run:
        for gwbk in gwbk_files:
            scaffold_gateway_yaml(
                backup_path=gwbk,
                backups_dir=backups_path,
                target_path=None,
                force=force_init,
            )

    # 3. Process candidate backups and build initial configurations
    norm_filters = flatten_arg_list(filters) if filters else []
    norm_profiles = flatten_arg_list(profiles) if profiles else []

    service_configs: List[GatewayServiceConfig] = []
    seen_services: dict[str, Path] = {}
    seen_containers: dict[str, str] = {}

    for gwbk in gwbk_files:
        # Load and merge config
        cfg_path = find_gateway_yaml(gwbk)
        svc_cfg = merge_service_config(
            backup_path=gwbk,
            backups_dir=backups_path,
            config_path=cfg_path,
            existing_services=set(seen_services.keys()),
            default_low_ram=low_ram,
            default_heap_max=default_heap_max,
            default_mem_limit=default_mem_limit,
            default_restore=restore,
        )

        # Check filter if specified
        if norm_filters:
            candidates = [
                gwbk.name,
                gwbk.stem,
                gwbk.parent.name,
                str(gwbk.relative_to(backups_path)),
                svc_cfg.service_name,
                svc_cfg.container_name or "",
                svc_cfg.system_name or "",
            ]
            if not any(matches_filter(candidates, f) for f in norm_filters):
                continue

        # Check profiles filter if specified
        if norm_profiles and not matches_profiles(svc_cfg.profiles, norm_profiles):
            continue

        # Check for service name collisions
        if svc_cfg.service_name in seen_services:
            prior = seen_services[svc_cfg.service_name]
            raise ConfigurationError(
                f"Duplicate service name '{svc_cfg.service_name}' claimed by '{gwbk.name}' "
                f"and '{prior.name}'"
            )

        # Check for container name collisions
        if svc_cfg.container_name:
            if svc_cfg.container_name in seen_containers:
                prior_svc = seen_containers[svc_cfg.container_name]
                raise ConfigurationError(
                    f"Duplicate container name '{svc_cfg.container_name}' claimed by '{svc_cfg.service_name}' "
                    f"and '{prior_svc}'"
                )
            seen_containers[svc_cfg.container_name] = svc_cfg.service_name

        seen_services[svc_cfg.service_name] = gwbk
        service_configs.append(svc_cfg)

    if not service_configs:
        raise ConfigurationError("No backups matched the specified filter / profiles criteria.")

    # 4. Allocate ports and detect port conflicts
    allocate_ports(service_configs, base_port=base_port)

    # 5. Build compose data structure and render YAML
    compose_dict = build_fleet_compose_dict(service_configs, out_path)
    yaml_content = render_compose_yaml(compose_dict)

    # 6. Build companion restore overlay if requested
    if restore_overlay_path:
        overlay_out_path = Path(restore_overlay_path).resolve()
        overlay_dict = build_fleet_restore_overlay_dict(service_configs, overlay_out_path)
        overlay_content = render_compose_yaml(overlay_dict)
        if dry_run:
            print("--- # Restore Overlay ---")
            print(overlay_content)
        else:
            overlay_out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(overlay_out_path, "w", encoding="utf-8") as f:
                f.write(overlay_content)

    # 7. Output to file or print if dry-run
    if dry_run:
        print(yaml_content)
    else:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(yaml_content)

    return yaml_content, service_configs


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

    import json
    import subprocess
    import urllib.request
    import urllib.error

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
        http_port = "-"
        for p in ports:
            p_str = str(p)
            if ":8088" in p_str:
                http_port = p_str.split(":8088")[0].split(":")[-1]
                break

        # Check Docker container state
        try:
            res = subprocess.run(
                ["docker", "inspect", container_name, "--format", "{{json .State}}"],
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode == 0 and res.stdout.strip():
                state_dict = json.loads(res.stdout.strip())
                status = state_dict.get("Status", "unknown")
                if status == "running":
                    any_running = True
                    docker_desc = "Running"
                elif status == "exited":
                    exit_code = state_dict.get("ExitCode", 0)
                    if exit_code == 137:
                        docker_desc = "Exited (137 OOM)"
                    else:
                        docker_desc = f"Exited ({exit_code})"
                    has_errors = True
                else:
                    docker_desc = status.capitalize()
            else:
                docker_desc = "Not Created"
        except Exception:
            docker_desc = "Docker Error"
            has_errors = True

        # Check Gateway HTTP StatusPing
        gw_state = "-"
        if docker_desc == "Running" and http_port != "-":
            try:
                url = f"http://localhost:{http_port}/StatusPing"
                req = urllib.request.Request(url, headers={"User-Agent": "HarnessHealthCheck"})
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    if resp.status == 200:
                        payload = json.loads(resp.read().decode())
                        gw_state = payload.get("state", "UNKNOWN")
                        if gw_state != "RUNNING":
                            has_errors = True
                    else:
                        gw_state = f"HTTP {resp.status}"
                        has_errors = True
            except Exception:
                gw_state = "STARTING..."
                has_errors = True
        elif docker_desc.startswith("Exited"):
            gw_state = "OFFLINE"

        print(f"{svc_name:<22} {container_name:<30} {http_port:<6} {docker_desc:<18} {gw_state:<15}")

    print("=" * len(header))
    if not any_running:
        print("Note: No fleet containers are currently running.")
        print(f"To start services, run: docker compose -f {compose_path} --profile <profile> up -d")
    return 1 if has_errors else 0


def main(args: Optional[List[str]] = None) -> int:
    """CLI entrypoint."""
    parsed = parse_args(args)
    if getattr(parsed, "status", False):
        return check_fleet_status(parsed.output)

    if getattr(parsed, "run_mode", False) and getattr(parsed, "restore_mode", False):
        print("Error: Cannot specify both --run/--no-restore and --restore flags simultaneously.", file=sys.stderr)
        return 1

    if getattr(parsed, "mode", None) == "run" and getattr(parsed, "restore_mode", False):
        print("Error: Conflicting mode arguments: --mode run and --restore.", file=sys.stderr)
        return 1

    if getattr(parsed, "mode", None) == "restore" and getattr(parsed, "run_mode", False):
        print("Error: Conflicting mode arguments: --mode restore and --run/--no-restore.", file=sys.stderr)
        return 1

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

