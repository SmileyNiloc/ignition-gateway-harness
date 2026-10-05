"""Domain service for dynamic Ignition gateway lifecycles and Docker Appliance orchestration."""

from dataclasses import dataclass
import logging
from pathlib import Path
import threading
import time
from typing import Any, Dict, List, Optional, Set, Union
import yaml

from ignition_gateway_harness.compose_builder import (
    build_fleet_compose_dict,
    build_unified_compose_dict,
    render_compose_yaml,
)
from ignition_gateway_harness.core.database import DatabaseProvisioner
from ignition_gateway_harness.core.inspector import BackupInspector, GatewaySpec
from ignition_gateway_harness.core.orchestrator import GatewayOrchestrator, GatewayStatus
from ignition_gateway_harness.exceptions import ConfigurationError, PortConflictError
from ignition_gateway_harness.models import GatewayServiceConfig
from ignition_gateway_harness.port_allocator import allocate_ports
from ignition_gateway_harness.trial_reset import TrialResetAutomator, TrialResetResult

logger = logging.getLogger(__name__)


@dataclass
class DeployedGateway:
    """Represents an actively deployed Ignition gateway instance."""

    service_name: str
    container_name: str
    port: int
    url: str
    spec: GatewaySpec
    service_config: GatewayServiceConfig
    is_ready: bool = False

    def get_status(self, orchestrator: Optional[GatewayOrchestrator] = None) -> GatewayStatus:
        orch = orchestrator or GatewayOrchestrator()
        return orch.get_gateway_status(self.service_name, self.container_name, self.port)


class GatewayManager:
    """Dynamic gateway lifecycle manager following the Docker Appliance pattern."""

    def __init__(
        self,
        project_root: Optional[Path | str] = None,
        sim_compose_path: Optional[Path | str] = None,
        fleet_compose_path: Optional[Path | str] = None,
        unified_compose_path: Optional[Path | str] = None,
        sim_init_dir: Optional[Path | str] = None,
        db_type: str = "mssql",
        admin_password: str = "password",
    ):
        self.root = Path(project_root or Path.cwd()).resolve()
        self.sim_compose = Path(sim_compose_path or self.root / "docker-compose.sim.yml").resolve()
        self.fleet_compose = Path(fleet_compose_path or self.root / "docker-compose.fleet.yml").resolve()
        self.unified_compose = Path(unified_compose_path or self.root / "docker-compose.yml").resolve()
        self.sim_init = Path(sim_init_dir or self.root / "sim_init").resolve()
        self.db_type = db_type
        self.admin_password = admin_password

        self.orchestrator = GatewayOrchestrator()
        self.db_provisioner = DatabaseProvisioner(default_password=admin_password)
        self.deployed_gateways: Dict[str, DeployedGateway] = {}
        self._registered_trial_gateways: Dict[str, str] = {}
        self._trial_daemon_thread: Optional[threading.Thread] = None
        self._trial_daemon_stop = threading.Event()

    def ensure_peripherals(self) -> bool:
        """Idempotently start peripheral simulation stack (MSSQL, Mailpit, Mosquitto, Mock OPC)."""
        logger.info("Ensuring peripheral simulation stack is active (%s)...", self.sim_compose.name)
        self.orchestrator.ensure_network("ignition_network")
        if not self.sim_compose.exists():
            # If sim compose doesn't exist yet, generate from sim_generator
            try:
                from ignition_gateway_harness.backup_analyzer import analyze_fleet
                from ignition_gateway_harness.sim_generator import write_simulation_stack
                backups_dir = self.root / "backups"
                report = analyze_fleet(backups_dir) if backups_dir.exists() else None
                if report:
                    write_simulation_stack(
                        report,
                        output_compose=self.sim_compose,
                        sim_init_dir=self.sim_init,
                        db_type=self.db_type,
                    )
            except Exception as exc:
                logger.warning("Could not pre-generate sim compose: %s", exc)

        return self.orchestrator.ensure_peripherals(self.sim_compose)

    def find_available_port(self, base_port: int = 8100) -> int:
        """Find the next available non-conflicting host port starting from base_port."""
        if not (1 <= base_port <= 65535):
            raise ConfigurationError(f"Invalid base_port: {base_port}")
        claimed = {gw.port for gw in self.deployed_gateways.values()}
        port = base_port
        while port in claimed:
            port += 1
            if port > 65535:
                raise PortConflictError("Exhausted valid TCP port range (>65535)")
        return port

    def register_trial_gateway(self, target: str, url: str) -> None:
        """Register a gateway target and URL with the trial reset daemon."""
        self._registered_trial_gateways[target] = url
        logger.info("Registered gateway '%s' (%s) with trial reset daemon", target, url)

    def start_trial_daemon(self, interval_minutes: int = 105) -> None:
        """Start the background trial reset daemon thread if not already running."""
        if self._trial_daemon_thread and self._trial_daemon_thread.is_alive():
            logger.info("Trial reset daemon is already running")
            return

        self._trial_daemon_stop.clear()

        def _daemon_worker():
            logger.info("Background trial reset daemon worker started (interval: %dm)", interval_minutes)
            automator = TrialResetAutomator(
                username="admin",
                password=self.admin_password,
                headless=True,
            )
            while not self._trial_daemon_stop.is_set():
                if self._registered_trial_gateways:
                    targets = list(self._registered_trial_gateways.items())
                    logger.info("Running daemon trial reset cycle for %d targets...", len(targets))
                    try:
                        automator.reset_fleet(targets)
                    except Exception as exc:
                        logger.warning("Error during daemon trial reset cycle: %s", exc)

                # Sleep in short slices to respond cleanly to stop signal
                sleep_secs = interval_minutes * 60
                end_time = time.time() + sleep_secs
                while time.time() < end_time and not self._trial_daemon_stop.is_set():
                    time.sleep(1.0)
            logger.info("Background trial reset daemon worker stopped")

        self._trial_daemon_thread = threading.Thread(
            target=_daemon_worker,
            name="TrialResetDaemonWorker",
            daemon=True,
        )
        self._trial_daemon_thread.start()

    def stop_trial_daemon(self) -> None:
        """Signal and stop the background trial reset daemon thread."""
        self._trial_daemon_stop.set()
        if self._trial_daemon_thread and self._trial_daemon_thread.is_alive():
            self._trial_daemon_thread.join(timeout=5.0)
        self._trial_daemon_thread = None

    def deploy_gateway(
        self,
        backup_path: Union[Path, str],
        port: Optional[int] = None,
        low_ram: bool = True,
        restore: bool = True,
        wait_ready: bool = True,
        timeout_secs: int = 120,
        auto_reset_trial: bool = True,
        extra_hosts: Optional[List[str]] = None,
    ) -> DeployedGateway:
        """Dynamically inspect a .gwbk backup and deploy its container appliance."""
        bpath = Path(backup_path)
        if not bpath.is_absolute():
            bpath = (self.root / bpath).resolve()

        if not bpath.exists():
            raise FileNotFoundError(f"Backup file not found: {bpath}")

        # 1. Pure in-memory inspection of backup
        logger.info("Inspecting backup in-memory: %s", bpath.name)
        spec = BackupInspector.inspect(bpath, backups_dir=self.root / "backups")

        # Determine non-conflicting host port
        if port is None:
            target_port = self.find_available_port(8100)
        else:
            target_port = int(port)
            claimed = {gw.service_name: gw.port for gw in self.deployed_gateways.values()}
            for s_name, p in claimed.items():
                if p == target_port and s_name != spec.service_name:
                    raise PortConflictError(
                        f"Host port {target_port} is already claimed by deployed gateway '{s_name}'"
                    )

        # 2. Start simulation stack if not already running
        self.ensure_peripherals()

        # 3. Idempotently provision discovered databases and logins in MSSQL
        if spec.database_names or spec.database_users:
            logger.info(
                "Provisioning required databases %s and users %s in MSSQL...",
                spec.database_names,
                spec.database_users,
            )
            self.db_provisioner.provision_for_spec(spec)

        # 4. Build GatewayServiceConfig with low-RAM boundaries
        svc_config = spec.to_service_config(
            port=target_port,
            low_ram=low_ram,
            restore=restore,
        )
        if extra_hosts:
            svc_config.extra_hosts.extend(extra_hosts)

        # Add DNS blackholing for unmocked external hostnames
        for h in spec.database_hosts:
            if h.lower() not in ("localhost", "127.0.0.1", "sim-mssql", "mssql"):
                entry = f"{h.lower()}:127.0.0.1"
                if entry not in svc_config.extra_hosts:
                    svc_config.extra_hosts.append(entry)

        # 5. Build dynamic compose definition and write to dynamic compose or unified
        dynamic_compose_path = self.root / f"docker-compose.{spec.service_name}.yml"
        compose_dict = build_fleet_compose_dict([svc_config], dynamic_compose_path)
        dynamic_yaml = render_compose_yaml(compose_dict)
        dynamic_compose_path.write_text(dynamic_yaml, encoding="utf-8")

        # 6. Spin up container and poll /StatusPing (without double-polling)
        logger.info("Launching gateway %s on host port %d...", spec.service_name, target_port)
        is_ready = False
        launched = self.orchestrator.spin_up_gateway(
            dynamic_compose_path,
            svc_config.service_name,
            timeout_secs=0,
            port=None,
        )

        if wait_ready and launched:
            is_ready = self.orchestrator.poll_gateway_health(target_port, timeout_secs=timeout_secs)

        deployed = DeployedGateway(
            service_name=spec.service_name,
            container_name=svc_config.container_name or f"ignition-{spec.service_name}",
            port=target_port,
            url=f"http://localhost:{target_port}",
            spec=spec,
            service_config=svc_config,
            is_ready=is_ready,
        )
        self.deployed_gateways[spec.service_name] = deployed

        # Register gateway with background trial reset daemon
        self.register_trial_gateway(spec.service_name, deployed.url)

        # 7. Automated trial reset if ready
        if auto_reset_trial and is_ready:
            logger.info("Triggering Playwright trial reset for %s...", spec.service_name)
            self.reset_gateway_trial(spec.service_name)

        return deployed

    def get_gateway_status(
        self,
        service_name: str,
        port: Optional[int] = None,
        container_name: Optional[str] = None,
    ) -> GatewayStatus:
        """Query live status for a gateway."""
        if service_name in self.deployed_gateways:
            gw = self.deployed_gateways[service_name]
            return gw.get_status(self.orchestrator)
        return self.orchestrator.get_gateway_status(
            service_name=service_name,
            container_name=container_name,
            port=port,
        )

    def reset_gateway_trial(
        self,
        target: Union[str, int],
        username: str = "admin",
        password: Optional[str] = None,
        headless: bool = True,
    ) -> TrialResetResult:
        """Trigger an automated Playwright trial reset for a running gateway."""
        pwd = password or self.admin_password
        automator = TrialResetAutomator(username=username, password=pwd, headless=headless)

        if isinstance(target, int):
            name = f"gateway-{target}"
            url = f"http://localhost:{target}"
        elif target.startswith("http://") or target.startswith("https://"):
            name = target
            url = target
        elif target in self.deployed_gateways:
            gw = self.deployed_gateways[target]
            name = gw.service_name
            url = gw.url
        else:
            name = target
            url = f"http://localhost:8100"

        return automator.reset_gateway(name, url)

    def stop_gateway(self, service_name: str, remove_volumes: bool = False) -> bool:
        """Stop and remove a specific running gateway appliance."""
        dynamic_compose_path = self.root / f"docker-compose.{service_name}.yml"
        if dynamic_compose_path.exists():
            res = self.orchestrator.down_gateway(dynamic_compose_path, remove_volumes=remove_volumes)
            try:
                dynamic_compose_path.unlink()
            except OSError as exc:
                logger.debug("Failed unlinking dynamic compose file %s: %s", dynamic_compose_path, exc)
            self.deployed_gateways.pop(service_name, None)
            self._registered_trial_gateways.pop(service_name, None)
            return res

        if self.fleet_compose.exists():
            return self.orchestrator.stop_gateway(self.fleet_compose, service_name)
        return False

    def deploy_profile(
        self,
        profile_name: str = "dev",
        base_port: int = 8100,
        low_ram: bool = True,
        timeout_secs: int = 120,
    ) -> List[DeployedGateway]:
        """Deploy all gateways belonging to a specific profile (e.g. dev, prod, test)."""
        from ignition_gateway_harness.generator import generate_fleet_compose

        self.ensure_peripherals()
        _, service_configs = generate_fleet_compose(
            backups_dir=self.root / "backups",
            output_path=self.fleet_compose,
            base_port=base_port,
            profiles=[profile_name],
            low_ram=low_ram,
            with_sim=True,
            sim_compose_path=self.sim_compose,
            sim_init_dir=self.sim_init,
            generate_unified=True,
            unified_output_path=self.unified_compose,
        )

        deployed_list: List[DeployedGateway] = []
        for svc in service_configs:
            port = None
            for p in svc.ports:
                if ":8088" in str(p):
                    port = int(str(p).split(":8088")[0].split(":")[-1])
                    break
            port = port or base_port
            deployed = self.deploy_gateway(
                backup_path=svc.backup_path,
                port=port,
                low_ram=low_ram,
                timeout_secs=timeout_secs,
            )
            deployed_list.append(deployed)

        return deployed_list
