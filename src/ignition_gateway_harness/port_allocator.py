"""Port allocation and port conflict detection for the gateway fleet."""

from typing import Dict, List, Union

from ignition_gateway_harness.exceptions import ConfigurationError, PortConflictError
from ignition_gateway_harness.models import GatewayServiceConfig


def extract_host_port(port_mapping: Union[str, int]) -> int:
    """Extract and validate the host port integer from a Docker port mapping string."""
    val = str(port_mapping).strip()
    # Strip protocol suffix if present (e.g., /tcp, /udp)
    if "/" in val:
        val = val.split("/")[0]

    # Handle IPv6 brackets e.g. [::1]:8100:8088 or [::]:8100
    if val.startswith("[") and "]" in val:
        closing_bracket = val.index("]")
        remainder = val[closing_bracket + 1 :].lstrip(":")
        parts = remainder.split(":")
        host_port_str = parts[0]
    else:
        parts = val.split(":")
        if len(parts) == 1:
            # e.g. "8100"
            host_port_str = parts[0]
        elif len(parts) == 2:
            # e.g. "8100:8088" or "127.0.0.1:8100"
            if "." in parts[0]:
                host_port_str = parts[1]
            else:
                host_port_str = parts[0]
        elif len(parts) == 3:
            # e.g. "127.0.0.1:8100:8088"
            host_port_str = parts[1]
        else:
            raise ConfigurationError(f"Invalid port mapping format: '{port_mapping}'")

    try:
        port = int(host_port_str)
    except ValueError as exc:
        raise ConfigurationError(
            f"Invalid non-integer host port '{host_port_str}' in mapping '{port_mapping}'"
        ) from exc

    if not (1 <= port <= 65535):
        raise ConfigurationError(f"Host port {port} out of valid TCP range (1-65535)")

    return port


def allocate_ports(services: List[GatewayServiceConfig], base_port: int = 8100) -> None:
    """Assign non-conflicting host ports starting at base_port and detect any conflicts.

    Modifies the service.ports in place for services that do not have explicit port overrides.
    Raises PortConflictError if any port is assigned to more than one service.
    """
    if not (1 <= base_port <= 65535):
        raise ConfigurationError(f"base_port {base_port} is not a valid port number (1-65535)")

    claimed_ports: Dict[int, str] = {}

    # Phase 1: Register and validate explicitly configured ports
    for svc in services:
        if svc.ports:
            for p_str in svc.ports:
                host_port = extract_host_port(p_str)
                if host_port in claimed_ports:
                    existing_svc = claimed_ports[host_port]
                    if existing_svc == svc.service_name:
                        raise PortConflictError(
                            f"Port conflict: Host port {host_port} is specified multiple times "
                            f"within service '{svc.service_name}'"
                        )
                    raise PortConflictError(
                        f"Port conflict: Host port {host_port} is claimed by multiple services "
                        f"('{existing_svc}' and '{svc.service_name}')"
                    )
                claimed_ports[host_port] = svc.service_name

    # Phase 2: Auto-assign sequential ports for services without explicit ports
    current_port = base_port
    for svc in services:
        if not svc.ports:
            while current_port in claimed_ports:
                current_port += 1
            if current_port > 65535:
                raise ConfigurationError(
                    f"Exhausted valid TCP port range (>65535) while assigning port to '{svc.service_name}'"
                )
            claimed_ports[current_port] = svc.service_name
            svc.ports = [f"{current_port}:8088"]
            current_port += 1
