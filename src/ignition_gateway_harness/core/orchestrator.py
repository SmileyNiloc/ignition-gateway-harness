"""Dynamic container lifecycle orchestrator and health polling engine."""

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional
import urllib.error
import urllib.request

from ignition_gateway_harness.models import GatewayServiceConfig

logger = logging.getLogger(__name__)


@dataclass
class GatewayStatus:
    """Status summary for an Ignition gateway container."""

    service_name: str
    container_name: str
    port: Optional[int] = None
    url: Optional[str] = None
    docker_state: str = "Not Created"
    gateway_state: str = "OFFLINE"
    is_ready: bool = False
    exit_code: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "service_name": self.service_name,
            "container_name": self.container_name,
            "port": self.port,
            "url": self.url,
            "docker_state": self.docker_state,
            "gateway_state": self.gateway_state,
            "is_ready": self.is_ready,
            "exit_code": self.exit_code,
        }


class GatewayOrchestrator:
    """Orchestrates Docker container lifecycles, health checks, and peripheral stacks."""

    def __init__(self, docker_bin: Optional[str] = None):
        self.docker_bin = docker_bin or shutil.which("docker") or "docker"

    def is_docker_available(self) -> bool:
        """Check if Docker daemon is running and accessible."""
        try:
            res = subprocess.run(
                [self.docker_bin, "info"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            return res.returncode == 0
        except Exception as exc:
            logger.debug("Docker not accessible: %s", exc)
            return False

    def ensure_network(self, network_name: str = "ignition_network") -> bool:
        """Ensure standard Docker bridge network exists (never internal: true)."""
        try:
            res = subprocess.run(
                [self.docker_bin, "network", "inspect", network_name],
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode == 0:
                return True
            res_create = subprocess.run(
                [self.docker_bin, "network", "create", "--driver", "bridge", network_name],
                capture_output=True,
                text=True,
                check=False,
            )
            return res_create.returncode == 0
        except Exception as exc:
            logger.debug("Failed ensuring network %s: %s", network_name, exc)
            return False

    def ensure_peripherals(self, sim_compose_path: Path | str) -> bool:
        """Launch the peripheral simulation stack if not already running."""
        comp_path = Path(sim_compose_path).resolve()
        if not comp_path.exists():
            logger.warning("Simulation compose file %s not found", comp_path)
            return False

        try:
            cmd = [self.docker_bin, "compose", "-f", str(comp_path), "up", "-d"]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if res.returncode != 0:
                logger.warning("Error launching simulation peripherals: %s", res.stderr)
                return False
            return True
        except Exception as exc:
            logger.debug("Exception running docker compose for simulation stack: %s", exc)
            return False

    def stop_peripherals(self, sim_compose_path: Path | str, remove_volumes: bool = False) -> bool:
        """Stop peripheral simulation stack containers."""
        comp_path = Path(sim_compose_path).resolve()
        if not comp_path.exists():
            return True

        cmd = [self.docker_bin, "compose", "-f", str(comp_path), "down"]
        if remove_volumes:
            cmd.append("-v")
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            return res.returncode == 0
        except Exception as exc:
            logger.debug("Failed stopping simulation stack: %s", exc)
            return False

    def get_container_state(self, container_name: str) -> Dict[str, Any]:
        """Query Docker inspect for container state."""
        try:
            res = subprocess.run(
                [self.docker_bin, "inspect", container_name, "--format", "{{json .State}}"],
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode == 0 and res.stdout.strip():
                return json.loads(res.stdout.strip())
        except Exception as exc:
            logger.debug("Error inspecting container %s: %s", container_name, exc)
        return {}

    def get_gateway_status(
        self,
        service_name: str,
        container_name: Optional[str] = None,
        port: Optional[int] = None,
    ) -> GatewayStatus:
        """Query Docker and HTTP /StatusPing for a gateway's live status."""
        c_name = container_name or f"ignition-{service_name}"
        url = f"http://localhost:{port}" if port else None
        state_dict = self.get_container_state(c_name)

        if not state_dict:
            return GatewayStatus(
                service_name=service_name,
                container_name=c_name,
                port=port,
                url=url,
                docker_state="Not Created",
                gateway_state="OFFLINE",
                is_ready=False,
            )

        status_str = state_dict.get("Status", "unknown")
        exit_code = state_dict.get("ExitCode")

        if status_str == "running":
            docker_desc = "Running"
        elif status_str == "exited":
            docker_desc = "Exited (137 OOM)" if exit_code == 137 else f"Exited ({exit_code})"
        else:
            docker_desc = status_str.capitalize()

        gw_state = "OFFLINE"
        is_ready = False

        if docker_desc == "Running" and port:
            gw_state = self.check_status_ping(port)
            if gw_state == "RUNNING":
                is_ready = True

        return GatewayStatus(
            service_name=service_name,
            container_name=c_name,
            port=port,
            url=url,
            docker_state=docker_desc,
            gateway_state=gw_state,
            is_ready=is_ready,
            exit_code=exit_code,
        )

    def check_status_ping(self, port: int, timeout_secs: float = 2.0) -> str:
        """Perform a single HTTP check against /StatusPing."""
        url = f"http://localhost:{port}/StatusPing"
        req = urllib.request.Request(url, headers={"User-Agent": "HarnessHealthCheck"})
        try:
            with urllib.request.urlopen(req, timeout=timeout_secs) as resp:
                if resp.status == 200:
                    payload = json.loads(resp.read().decode())
                    return payload.get("state", "UNKNOWN")
                return f"HTTP {resp.status}"
        except Exception:
            return "STARTING..."

    def poll_gateway_health(
        self,
        port: int,
        timeout_secs: int = 120,
        interval_secs: float = 2.0,
    ) -> bool:
        """Poll /StatusPing until RUNNING state is reached or timeout expires."""
        start_time = time.time()
        while time.time() - start_time < timeout_secs:
            state = self.check_status_ping(port, timeout_secs=1.5)
            if state == "RUNNING":
                return True
            time.sleep(interval_secs)
        return False

    def spin_up_gateway(
        self,
        compose_path: Path | str,
        service_name: str,
        timeout_secs: int = 120,
        port: Optional[int] = None,
    ) -> bool:
        """Launch an individual gateway service defined in a Compose file and wait for health."""
        comp = Path(compose_path).resolve()
        cmd = [self.docker_bin, "compose", "-f", str(comp), "up", "-d", service_name]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if res.returncode != 0:
                logger.warning("Error launching service %s: %s", service_name, res.stderr)
                return False
        except Exception as exc:
            logger.debug("Failed executing docker compose up for %s: %s", service_name, exc)
            return False

        if port:
            return self.poll_gateway_health(port, timeout_secs=timeout_secs)
        return True

    def stop_gateway(self, compose_path: Path | str, service_name: str) -> bool:
        """Stop an individual gateway container."""
        comp = Path(compose_path).resolve()
        cmd = [self.docker_bin, "compose", "-f", str(comp), "stop", service_name]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            return res.returncode == 0
        except Exception as exc:
            logger.debug("Failed executing docker compose stop for %s: %s", service_name, exc)
            return False

    def down_gateway(self, compose_path: Path | str, remove_volumes: bool = False) -> bool:
        """Tear down and remove containers defined in an ephemeral compose file."""
        comp = Path(compose_path).resolve()
        cmd = [self.docker_bin, "compose", "-f", str(comp), "down"]
        if remove_volumes:
            cmd.append("-v")
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            return res.returncode == 0
        except Exception as exc:
            logger.debug("Failed executing docker compose down for %s: %s", comp.name, exc)
            return False
