"""Data models for gateway configuration and compose generation."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class GatewayServiceConfig:
    """Configuration for an individual Ignition gateway container in the fleet."""

    backup_path: Path
    service_name: str
    config_path: Optional[Path] = None
    container_name: Optional[str] = None
    hostname: Optional[str] = None
    system_name: Optional[str] = None
    image: str = "inductiveautomation/ignition:8.1.51"
    ports: List[str] = field(default_factory=list)
    heap_max: Optional[str] = "1024m"
    jvm_args: List[str] = field(default_factory=list)
    command: Optional[List[str]] = None
    environment: Dict[str, str] = field(default_factory=dict)
    profiles: List[str] = field(default_factory=list)
    gan_aliases: List[str] = field(default_factory=list)
    extra_volumes: List[Any] = field(default_factory=list)
    raw_overrides: Dict[str, Any] = field(default_factory=dict)
    low_ram: bool = False
    mem_limit: Optional[str] = None
    restore: bool = True
    data_volume: Optional[str] = None
    explicit_restore: Optional[bool] = None
    extra_hosts: List[str] = field(default_factory=list)

    def get_data_volume_name(self) -> str:
        """Return the named data volume for this service."""
        if self.data_volume:
            return self.data_volume
        return f"{self.service_name}_data"
