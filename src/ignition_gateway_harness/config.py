"""Gateway configuration loading, merging, and scaffolding."""

from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set, Tuple
import yaml

from ignition_gateway_harness.discovery import (
    derive_service_identity,
    find_gateway_yaml,
    sanitize_service_name,
)
from ignition_gateway_harness.exceptions import ConfigurationError
from ignition_gateway_harness.models import GatewayServiceConfig


def build_default_environment(system_name: str) -> Dict[str, str]:
    """Return sensible default environment variables for an Ignition Gateway container."""
    return {
        "ACCEPT_IGNITION_EULA": "Y",
        "IGNITION_EDITION": "standard",
        "GATEWAY_SYSTEM_NAME": system_name,
        "GATEWAY_ADMIN_USERNAME": "admin",
        "GATEWAY_ADMIN_PASSWORD": "password",
        "GATEWAY_HTTP_PORT": "8088",
        "GATEWAY_HTTPS_PORT": "8043",
        "GATEWAY_GAN_PORT": "8060",
        "ACCEPT_MODULE_LICENSES": "all",
        "ACCEPT_MODULE_CERTS": "all",
    }


def normalize_heap_limit(raw_val: Any) -> Optional[str]:
    """Normalize heap limit to value like '1024m', '2048m', or None."""
    if raw_val is None:
        return None
    val_str = str(raw_val).strip()
    if not val_str or val_str.lower() in ("none", "null", "0", "false"):
        return None
    if val_str.startswith("-Xmx"):
        val_str = val_str[4:].strip()
    if val_str.isdigit():
        return f"{val_str}m"
    return val_str


def parse_environment_override(env_data: Any) -> Dict[str, str]:
    """Parse environment variables from dictionary or list format."""
    parsed: Dict[str, str] = {}
    if isinstance(env_data, dict):
        for k, v in env_data.items():
            parsed[str(k)] = str(v) if v is not None else ""
    elif isinstance(env_data, list):
        for item in env_data:
            if isinstance(item, str) and "=" in item:
                k, v = item.split("=", 1)
                parsed[k.strip()] = v.strip()
            elif isinstance(item, str) and item.strip():
                parsed[item.strip()] = ""
            elif isinstance(item, dict):
                for k, v in item.items():
                    parsed[str(k)] = str(v) if v is not None else ""
    return parsed


def parse_ports_override(ports_data: Any) -> List[str]:
    """Parse port mappings into list of compose port strings."""
    result: List[str] = []
    if isinstance(ports_data, (int, str)):
        val = str(ports_data).strip()
        result.append(val if ":" in val else f"{val}:8088")
    elif isinstance(ports_data, list):
        for item in ports_data:
            if isinstance(item, dict):
                for host, container in item.items():
                    result.append(f"{host}:{container}")
            elif isinstance(item, (int, str)):
                val = str(item).strip()
                result.append(val if ":" in val else f"{val}:8088")
    elif isinstance(ports_data, dict):
        for host, container in ports_data.items():
            result.append(f"{host}:{container}")
    return result


def load_gateway_yaml(config_path: Path) -> Dict[str, Any]:
    """Safely load a gateway.yaml configuration override file."""
    if not config_path.exists():
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            if data is None:
                return {}
            if not isinstance(data, dict):
                raise ConfigurationError(
                    f"Configuration in {config_path} must be a YAML mapping/dictionary"
                )
            return data
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Error parsing YAML in {config_path}: {exc}") from exc


def merge_service_config(
    backup_path: Path,
    backups_dir: Path,
    config_path: Optional[Path] = None,
    existing_services: Optional[Set[str]] = None,
    default_low_ram: bool = False,
    default_heap_max: Optional[str] = None,
    default_mem_limit: Optional[str] = None,
    default_restore: bool = True,
) -> GatewayServiceConfig:
    """Merge discovery metadata with gateway.yaml overrides to build service configuration."""
    service_name, system_name, default_profiles = derive_service_identity(
        backup_path, backups_dir, existing_services
    )

    if config_path is None:
        config_path = find_gateway_yaml(backup_path)

    overrides = load_gateway_yaml(config_path) if config_path else {}

    # Check low_ram configuration
    low_ram = default_low_ram
    for k in ("low_ram", "low_mem", "compact"):
        if k in overrides and overrides[k] is not None:
            low_ram = bool(overrides[k])
            break

    # Service name override
    if "service_name" in overrides and overrides["service_name"]:
        service_name = sanitize_service_name(str(overrides["service_name"]).strip())

    # Container name override (defaults to ignition-<service_name>)
    container_name = overrides.get("container_name") or overrides.get("name")
    if container_name:
        container_name = str(container_name).strip()
    else:
        container_name = f"ignition-{service_name}"

    # Hostname override
    hostname = overrides.get("hostname")
    if hostname:
        hostname = str(hostname).strip()
    else:
        hostname = service_name

    # System name override
    if "system_name" in overrides and overrides["system_name"]:
        system_name = str(overrides["system_name"]).strip()

    # Base environment
    env = build_default_environment(system_name)
    if "environment" in overrides:
        env.update(parse_environment_override(overrides["environment"]))
    elif "env" in overrides:
        env.update(parse_environment_override(overrides["env"]))

    # Ports override
    ports: List[str] = []
    if "ports" in overrides and overrides["ports"] is not None:
        ports.extend(parse_ports_override(overrides["ports"]))
    if "port" in overrides and overrides["port"]:
        p = str(overrides["port"]).strip()
        ports.append(p if ":" in p else f"{p}:8088")
    if "http_port" in overrides and overrides["http_port"]:
        hp = str(overrides["http_port"]).strip()
        ports.append(hp if ":" in hp else f"{hp}:8088")
    if "https_port" in overrides and overrides["https_port"]:
        hsp = str(overrides["https_port"]).strip()
        ports.append(hsp if ":" in hsp else f"{hsp}:8043")
    if "gan_port" in overrides and overrides["gan_port"]:
        gp = str(overrides["gan_port"]).strip()
        ports.append(gp if ":" in gp else f"{gp}:8060")

    # JVM Heap limit
    heap_keys = ("heap_max", "max_memory", "jvm_max_memory", "heap_limit")
    heap_max_raw = None
    has_heap_override = False
    for k in heap_keys:
        if k in overrides:
            heap_max_raw = overrides[k]
            has_heap_override = True
            break

    if has_heap_override:
        heap_max = normalize_heap_limit(heap_max_raw)
    elif default_heap_max is not None:
        heap_max = normalize_heap_limit(default_heap_max)
    elif low_ram:
        heap_max = "1024m"
    else:
        heap_max = "1024m"

    # Memory limit
    mem_limit = None
    for k in ("mem_limit", "memory_limit", "mem_ceiling"):
        if k in overrides and overrides[k] is not None:
            mem_limit = str(overrides[k]).strip()
            break
    if mem_limit is None:
        if default_mem_limit is not None:
            mem_limit = default_mem_limit
        elif low_ram:
            mem_limit = "1800M"

    # Extra JVM args
    jvm_args: List[str] = []
    if "jvm_args" in overrides and isinstance(overrides["jvm_args"], list):
        jvm_args = [str(arg) for arg in overrides["jvm_args"]]

    # Command override
    command: Optional[List[str]] = None
    if "command" in overrides and overrides["command"]:
        if isinstance(overrides["command"], list):
            command = [str(c) for c in overrides["command"]]
        elif isinstance(overrides["command"], str):
            command = overrides["command"].split()

    # Profiles override
    profiles: List[str] = default_profiles
    for key in ("profiles", "profile"):
        if key in overrides:
            raw_prof = overrides[key]
            if raw_prof is None:
                profiles = []
            elif isinstance(raw_prof, list):
                profiles = [str(p).strip() for p in raw_prof if str(p).strip()]
            elif isinstance(raw_prof, str):
                s = raw_prof.strip()
                profiles = [s] if s else []
            break

    # GAN aliases override
    gan_aliases: List[str] = [service_name]
    for key in ("gan_aliases", "network_aliases", "aliases", "gan_alias"):
        if key in overrides:
            raw_gan = overrides[key]
            if raw_gan is None:
                gan_aliases = []
            elif isinstance(raw_gan, list):
                gan_aliases = [str(a).strip() for a in raw_gan if str(a).strip()]
            elif isinstance(raw_gan, str):
                s = raw_gan.strip()
                gan_aliases = [s] if s else []
            break

    # Restore mode override (defaults to default_restore)
    restore = default_restore
    explicit_restore: Optional[bool] = None
    for k in ("restore", "load_from_backup", "restore_backup", "from_backup"):
        if k in overrides and overrides[k] is not None:
            restore = bool(overrides[k])
            explicit_restore = restore
            break
    if "mode" in overrides and overrides["mode"]:
        m = str(overrides["mode"]).strip().lower()
        if m in ("run", "persistent", "volume", "no-restore", "existing"):
            restore = False
            explicit_restore = False
        elif m in ("restore", "backup", "load"):
            restore = True
            explicit_restore = True

    # Data volume override
    data_volume: Optional[str] = None
    for k in ("data_volume", "volume_name", "persistent_volume"):
        if k in overrides and overrides[k]:
            data_volume = str(overrides[k]).strip()
            break

    # Extra volumes
    extra_volumes: List[Any] = []
    if "volumes" in overrides and isinstance(overrides["volumes"], list):
        extra_volumes = [v if isinstance(v, dict) else str(v) for v in overrides["volumes"]]

    image = overrides.get("image", "inductiveautomation/ignition:8.1.51")

    # Collect any extra unrecognized docker-compose keys (e.g. restart, labels, deploy)
    standard_keys = {
        "service_name",
        "container_name",
        "name",
        "hostname",
        "system_name",
        "image",
        "ports",
        "port",
        "http_port",
        "https_port",
        "gan_port",
        "heap_max",
        "max_memory",
        "jvm_max_memory",
        "heap_limit",
        "jvm_args",
        "command",
        "environment",
        "env",
        "profiles",
        "profile",
        "gan_aliases",
        "network_aliases",
        "aliases",
        "gan_alias",
        "volumes",
        "low_ram",
        "low_mem",
        "compact",
        "mem_limit",
        "memory_limit",
        "mem_ceiling",
        "restore",
        "load_from_backup",
        "restore_backup",
        "from_backup",
        "mode",
        "data_volume",
        "volume_name",
        "persistent_volume",
    }
    raw_overrides = {k: v for k, v in overrides.items() if k not in standard_keys}

    return GatewayServiceConfig(
        backup_path=backup_path,
        service_name=service_name,
        config_path=config_path,
        container_name=container_name,
        hostname=hostname,
        system_name=system_name,
        image=image,
        ports=ports,
        heap_max=heap_max,
        jvm_args=jvm_args,
        command=command,
        environment=env,
        profiles=profiles,
        gan_aliases=gan_aliases,
        extra_volumes=extra_volumes,
        raw_overrides=raw_overrides,
        low_ram=low_ram,
        mem_limit=mem_limit,
        restore=restore,
        data_volume=data_volume,
        explicit_restore=explicit_restore,
    )


def scaffold_gateway_yaml(
    backup_path: Path,
    backups_dir: Path,
    target_path: Optional[Path] = None,
    force: bool = False,
) -> Tuple[Path, bool]:
    """Scaffold a missing gateway.yaml configuration next to the backup file.

    Returns (target_path, was_created).
    """
    if target_path is None:
        parent = backup_path.parent
        gwbk_in_dir = list(parent.glob("*.gwbk"))
        if len(gwbk_in_dir) > 1 and backup_path.name.lower() not in ("backup.gwbk", "gateway.gwbk"):
            target_path = backup_path.with_name(f"{backup_path.stem}.gateway.yaml")
        else:
            target_path = parent / "gateway.yaml"

    if target_path.exists() and not force:
        return target_path, False

    service_name, system_name, profiles = derive_service_identity(backup_path, backups_dir)
    profile_str = profiles[0] if profiles else "default"

    template = f"""# Gateway fleet configuration override for {service_name}
# Automatically generated configuration scaffold
container_name: ignition-{service_name}
hostname: {service_name}

# Host port mapping (uncomment to override sequential allocation starting at 8100)
# ports:
#   - "8100:8088"

# JVM heap memory cap (default: 1024m)
heap_max: "1024m"

# Restore mode: set to false to run from persistent volume without restoring from backup
# restore: true
# data_volume: {service_name}_data

# Compose profiles
profiles:
  - {profile_str}

# Gateway Area Network (GAN) network aliases
gan_aliases:
  - {service_name}

# Environment variables
environment:
  ACCEPT_IGNITION_EULA: "Y"
  IGNITION_EDITION: "standard"
  GATEWAY_SYSTEM_NAME: "{system_name}"
  GATEWAY_ADMIN_USERNAME: "admin"
  GATEWAY_ADMIN_PASSWORD: "password"
"""

    target_path.parent.mkdir(parents=True, exist_ok=True)
    with open(target_path, "w", encoding="utf-8") as f:
        f.write(template)

    return target_path, True
