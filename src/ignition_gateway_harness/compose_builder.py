"""Docker Compose file builder for the Ignition gateway fleet."""

import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional
import yaml

from ignition_gateway_harness.models import GatewayServiceConfig


class CleanYamlDumper(yaml.SafeDumper):
    """YAML Dumper that formats None as empty scalar for clean compose files."""


CleanYamlDumper.add_representer(
    type(None),
    lambda dumper, value: dumper.represent_scalar("tag:yaml.org,2002:null", ""),
)


def format_relative_path(target_path: Path, relative_to_dir: Path) -> str:
    """Format target_path as a forward-slash relative path starting with ./ if relative."""
    try:
        rel = os.path.relpath(target_path.resolve(), relative_to_dir.resolve())
        rel_clean = rel.replace("\\", "/")
        if not rel_clean.startswith(".") and not rel_clean.startswith("/"):
            rel_clean = f"./{rel_clean}"
        return rel_clean
    except ValueError:
        # Cross-drive on Windows
        return str(target_path.resolve()).replace("\\", "/")


def is_bind_mount_source(source: str) -> bool:
    """Return True if source represents a host bind mount path rather than a named Docker volume."""
    return (
        source.startswith(".")
        or source.startswith("/")
        or source.startswith("~")
        or "\\" in source
        or (len(source) > 1 and source[1] == ":")
    )


def extract_volume_source(vol: Any) -> Optional[str]:
    """Extract host source or volume name from volume specification."""
    if isinstance(vol, dict):
        vol_type = vol.get("type")
        source = vol.get("source")
        if vol_type == "bind":
            return None
        return str(source).strip() if source else None

    vol_str = str(vol).strip()
    if not vol_str:
        return None

    # Handle Windows drive letters like C:\path or C:/path
    match = re.match(r"^([a-zA-Z]:[\\/][^:]+):", vol_str)
    if match:
        return match.group(1).strip()

    parts = vol_str.split(":")
    if len(parts) >= 2:
        return parts[0].strip()
    return vol_str


def build_service_dict(svc: GatewayServiceConfig, output_dir: Path) -> Dict[str, Any]:
    """Construct the Docker Compose dictionary definition for a single service."""
    # Build command
    if svc.command is not None:
        cmd = list(svc.command)
    else:
        if svc.restore:
            cmd = ["-r", "/restore.gwbk", "--", "-Dignition.http.session.cookie.same-site.value=Lax"]
        else:
            cmd = ["--", "-Dignition.http.session.cookie.same-site.value=Lax"]

        if svc.low_ram:
            heap_str = svc.heap_max or "1024m"
            heap_int_mb = 1024
            val = heap_str.lower().rstrip("m").rstrip("g")
            try:
                heap_int_mb = int(val)
                if heap_str.lower().endswith("g"):
                    heap_int_mb *= 1024
            except ValueError:
                heap_int_mb = 1024

            init_heap = min(256, max(128, heap_int_mb // 4))

            cmd.extend([
                "wrapper.java.initmemory.percent=0",
                "wrapper.java.maxmemory.percent=0",
                f"wrapper.java.initmemory={init_heap}",
                f"wrapper.java.maxmemory={heap_int_mb}",
                f"-Xmx{heap_str}",
                "-XX:MaxMetaspaceSize=256m",
                "-Xss256k",
            ])
        else:
            if svc.heap_max:
                cmd.append(f"-Xmx{svc.heap_max}")
        if svc.jvm_args:
            cmd.extend(svc.jvm_args)

    # Backup bind mount and persistent data volume
    volumes: List[str] = []
    if svc.restore:
        rel_backup_path = format_relative_path(svc.backup_path, output_dir)
        volumes.append(f"{rel_backup_path}:/restore.gwbk:ro")

    volumes.append(f"{svc.get_data_volume_name()}:/usr/local/bin/ignition/data")

    if svc.extra_volumes:
        volumes.extend(svc.extra_volumes)

    service_def: Dict[str, Any] = {
        "image": svc.image,
        "container_name": svc.container_name,
        "hostname": svc.hostname,
        "command": cmd,
        "ports": list(svc.ports),
        "environment": dict(svc.environment),
        "volumes": volumes,
    }

    # Low-RAM Docker cgroup resource limit
    if svc.mem_limit or svc.low_ram:
        limit_val = svc.mem_limit or "1800M"
        service_def["mem_limit"] = limit_val.lower()
        service_def["deploy"] = {
            "resources": {
                "limits": {
                    "memory": limit_val,
                }
            }
        }

    # Network configuration with GAN aliases
    if svc.gan_aliases:
        service_def["networks"] = {
            "ignition_network": {
                "aliases": list(svc.gan_aliases),
            }
        }
    else:
        service_def["networks"] = ["ignition_network"]

    # Compose profiles
    if svc.profiles:
        service_def["profiles"] = list(svc.profiles)

    # Merge any extra user overrides (e.g. restart, labels, deploy)
    if svc.raw_overrides:
        service_def.update(svc.raw_overrides)

    return service_def


def build_fleet_compose_dict(
    services: List[GatewayServiceConfig], output_path: Path
) -> Dict[str, Any]:
    """Construct the complete Docker Compose fleet dictionary."""
    output_dir = output_path.parent.resolve()

    compose_services: Dict[str, Any] = {}
    compose_volumes: Dict[str, Any] = {}

    for svc in services:
        compose_services[svc.service_name] = build_service_dict(svc, output_dir)
        data_vol = svc.get_data_volume_name()
        if not is_bind_mount_source(data_vol) and data_vol:
            compose_volumes[data_vol] = None

        # Register any extra named volumes in top-level volumes
        for vol in svc.extra_volumes:
            source = extract_volume_source(vol)
            if source and not is_bind_mount_source(source):
                compose_volumes[source] = None

    compose_dict: Dict[str, Any] = {
        "services": compose_services,
        "networks": {
            "ignition_network": {
                "driver": "bridge",
            }
        },
        "volumes": compose_volumes,
    }

    return compose_dict


def build_restore_overlay_service_dict(
    svc: GatewayServiceConfig, output_dir: Path
) -> Dict[str, Any]:
    """Construct Docker Compose overlay service definition to mount backup and execute restore."""
    rel_backup_path = format_relative_path(svc.backup_path, output_dir)
    if svc.command is not None:
        cmd = list(svc.command)
        if "-r" not in cmd and "/restore.gwbk" not in cmd:
            if cmd and cmd[0] == "--":
                cmd = ["-r", "/restore.gwbk"] + cmd
            else:
                cmd = ["-r", "/restore.gwbk", "--"] + cmd
    else:
        cmd = ["-r", "/restore.gwbk", "--", "-Dignition.http.session.cookie.same-site.value=Lax"]
        if svc.low_ram:
            heap_str = svc.heap_max or "1024m"
            heap_int_mb = 1024
            val = heap_str.lower().rstrip("m").rstrip("g")
            try:
                heap_int_mb = int(val)
                if heap_str.lower().endswith("g"):
                    heap_int_mb *= 1024
            except ValueError:
                heap_int_mb = 1024

            init_heap = min(256, max(128, heap_int_mb // 4))

            cmd.extend([
                "wrapper.java.initmemory.percent=0",
                "wrapper.java.maxmemory.percent=0",
                f"wrapper.java.initmemory={init_heap}",
                f"wrapper.java.maxmemory={heap_int_mb}",
                f"-Xmx{heap_str}",
                "-XX:MaxMetaspaceSize=256m",
                "-Xss256k",
            ])
        else:
            if svc.heap_max:
                cmd.append(f"-Xmx{svc.heap_max}")
        if svc.jvm_args:
            cmd.extend(svc.jvm_args)

    return {
        "volumes": [f"{rel_backup_path}:/restore.gwbk:ro"],
        "command": cmd,
    }


def build_fleet_restore_overlay_dict(
    services: List[GatewayServiceConfig], output_path: Path
) -> Dict[str, Any]:
    """Construct Docker Compose overlay dictionary for restoring fleet from backups."""
    output_dir = output_path.parent.resolve()
    overlay_services: Dict[str, Any] = {}
    for svc in services:
        if svc.explicit_restore is False:
            continue
        overlay_services[svc.service_name] = build_restore_overlay_service_dict(svc, output_dir)
    return {"services": overlay_services}


def render_compose_yaml(compose_dict: Dict[str, Any]) -> str:
    """Render the compose dictionary into a YAML string with a header comment."""
    header = (
        "# Code generated by ignition-gateway-harness fleet generator. DO NOT EDIT MANUALLY.\n"
        "# To regenerate or modify configurations, run:\n"
        "#   uv run ignition-gateway-harness\n\n"
    )
    body = yaml.dump(
        compose_dict,
        Dumper=CleanYamlDumper,
        sort_keys=False,
        default_flow_style=False,
    )
    return header + body
