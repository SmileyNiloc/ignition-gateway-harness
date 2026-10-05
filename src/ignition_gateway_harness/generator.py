"""Core fleet compose generator for Ignition gateway fleets."""

import logging
from pathlib import Path
import sys
from typing import List, Optional, Tuple
import yaml

from ignition_gateway_harness.cli import (
    check_fleet_status,
    flatten_arg_list,
    main,
    parse_args,
    run_analyze_cli,
    run_deploy_cli,
    run_down_cli,
    run_sim_cli,
    run_trial_reset_cli,
    run_up_cli,
)
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

logger = logging.getLogger(__name__)


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
    enrich_aliases: bool = False,
    with_sim: bool = False,
    sim_compose_path: Optional[Path | str] = None,
    sim_init_dir: Optional[Path | str] = None,
    isolate: bool = True,
    blackhole_hosts: bool = False,
    generate_unified: bool = False,
    unified_output_path: Optional[Path | str] = None,
    db_type: str = "mssql",
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

    # 4b. Enrich fleet services with discovered production host aliases and generate sim stack
    sim_report = None
    if enrich_aliases or with_sim or isolate or blackhole_hosts or generate_unified:
        try:
            from ignition_gateway_harness.backup_analyzer import analyze_fleet
            from ignition_gateway_harness.sim_generator import (
                enrich_fleet_with_discovered_aliases,
                enrich_fleet_with_isolation_hosts,
                write_simulation_stack,
            )
            sim_report = analyze_fleet(backups_path, filters=filters)
            if enrich_aliases or with_sim:
                enrich_fleet_with_discovered_aliases(service_configs, sim_report)
            if isolate or blackhole_hosts:
                enrich_fleet_with_isolation_hosts(
                    service_configs,
                    sim_report,
                    blackhole_ip="127.0.0.1",
                    redirect_to_sim=bool(with_sim and not blackhole_hosts),
                )
            if with_sim and not dry_run:
                sim_out = Path(sim_compose_path or "docker-compose.sim.yml").resolve()
                sim_init = Path(sim_init_dir or "sim_init").resolve()
                write_simulation_stack(sim_report, output_compose=sim_out, sim_init_dir=sim_init, db_type=db_type)
        except Exception as exc:
            logger.warning("Error generating or enriching simulation stack: %s", exc)

    # 5. Build compose data structure and render YAML
    compose_dict = build_fleet_compose_dict(service_configs, out_path)
    yaml_content = render_compose_yaml(compose_dict)

    # 5b. Build unified compose if requested
    if generate_unified and sim_report is not None and not dry_run:
        try:
            from ignition_gateway_harness.compose_builder import build_unified_compose_dict
            uni_path = Path(unified_output_path or "docker-compose.yml").resolve()
            sim_init = Path(sim_init_dir or "sim_init").resolve()
            uni_dict = build_unified_compose_dict(service_configs, sim_report, sim_init, uni_path, db_type=db_type)
            uni_yaml = render_compose_yaml(uni_dict)
            uni_path.parent.mkdir(parents=True, exist_ok=True)
            with open(uni_path, "w", encoding="utf-8") as f:
                f.write(uni_yaml)
        except Exception as exc:
            logger.warning("Error generating unified compose file: %s", exc)

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


__all__ = [
    "check_fleet_status",
    "flatten_arg_list",
    "generate_fleet_compose",
    "main",
    "parse_args",
    "run_analyze_cli",
    "run_deploy_cli",
    "run_down_cli",
    "run_sim_cli",
    "run_trial_reset_cli",
    "run_up_cli",
]


if __name__ == "__main__":
    sys.exit(main())
