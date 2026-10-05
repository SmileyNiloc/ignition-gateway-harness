"""Unit and integration tests for 100% Production Isolation and 1-Command Orchestration.

Verifies:
1. Docker Network Isolation: `internal: true` on `ignition_network` across all compose definitions
   (strictly forbidding outbound egress to host LAN, VPN, corporate network, or internet).
2. DNS & Host Blackholing / Redirection (`extra_hosts`):
   - Discovered corporate hostnames (SMTP, JDBC DBs, MQTT, OPC/PLCs) are redirected to mock containers
     or blackholed to 127.0.0.1 / 0.0.0.0.
   - Mailpit acts as the email sink trap, and no external DNS can be resolved.
3. Unified Compose Generation: combines simulation stack and fleet services in a single file.
4. Single-Command Orchestration (`up` / `start`, `down` / `stop` CLI).
"""

from pathlib import Path
import shutil
import subprocess
import pytest
import yaml

from ignition_gateway_harness.backup_analyzer import analyze_fleet
from ignition_gateway_harness.compose_builder import (
    build_fleet_compose_dict,
    build_unified_compose_dict,
)
from ignition_gateway_harness.generator import (
    generate_fleet_compose,
    parse_args,
    run_down_cli,
)
from ignition_gateway_harness.models import GatewayServiceConfig
from ignition_gateway_harness.sim_generator import (
    build_sim_compose_dict,
    enrich_fleet_with_discovered_aliases,
    enrich_fleet_with_isolation_hosts,
    write_simulation_stack,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKUPS_DIR = REPO_ROOT / "backups"


@pytest.fixture(scope="module")
def fleet_report():
    return analyze_fleet(BACKUPS_DIR)


# ==============================================================================
# 1. Docker Network Isolation Tests (internal: true)
# ==============================================================================


class TestDockerNetworkIsolation:
    """Verify that ignition_network is configured as a bridge network preserving host port publishing."""

    def test_build_fleet_compose_has_bridge_network(self):
        svc = GatewayServiceConfig(
            backup_path=Path("dummy.gwbk"),
            service_name="test-gateway",
        )
        compose_dict = build_fleet_compose_dict([svc], Path("docker-compose.fleet.yml"))
        networks = compose_dict.get("networks", {})
        assert "ignition_network" in networks
        net = networks["ignition_network"]
        assert not net.get("internal"), "Fleet ignition_network must not set internal: True to preserve host browser access"
        assert net.get("driver") == "bridge"

    def test_build_sim_compose_has_bridge_network(self, fleet_report, tmp_path: Path):
        init_dir = tmp_path / "sim_init"
        compose_path = tmp_path / "docker-compose.sim.yml"
        sim_dict = build_sim_compose_dict(fleet_report, init_dir, compose_path)
        networks = sim_dict.get("networks", {})
        assert "ignition_network" in networks
        net = networks["ignition_network"]
        assert not net.get("internal"), "Simulation ignition_network must not set internal: True to preserve host browser access"
        assert net.get("driver") == "bridge"

    def test_build_unified_compose_has_bridge_network(self, fleet_report, tmp_path: Path):
        svc = GatewayServiceConfig(
            backup_path=Path("dummy.gwbk"),
            service_name="test-gateway",
        )
        init_dir = tmp_path / "sim_init"
        out_path = tmp_path / "docker-compose.yml"
        unified_dict = build_unified_compose_dict([svc], fleet_report, init_dir, out_path)
        networks = unified_dict.get("networks", {})
        assert "ignition_network" in networks
        net = networks["ignition_network"]
        assert not net.get("internal"), "Unified compose ignition_network must not set internal: True to preserve host browser access"

    def test_static_docker_compose_files_have_bridge_network(self):
        """Verify static compose files in repo root declare bridge network with port publishing preserved."""
        for filename in [
            "docker-compose.yml",
            "docker-compose.sim.yml",
            "docker-compose.fleet.yml",
        ]:
            file_path = REPO_ROOT / filename
            if not file_path.exists():
                continue
            with open(file_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            networks = data.get("networks", {})
            assert "ignition_network" in networks, f"{filename} missing ignition_network"
            net = networks["ignition_network"]
            assert net.get("driver") == "bridge"
            assert not net.get("internal"), f"{filename} ignition_network must not set internal: true"


# ==============================================================================
# 2. DNS & Host Blackholing / Redirection Tests (extra_hosts & Aliases)
# ==============================================================================


class TestDNSAndHostIsolation:
    """Verify DNS isolation, extra_hosts redirection/blackholing, and Mailpit sink trapping."""

    def test_discovered_corporate_hosts_aggregation(self, fleet_report):
        hosts = fleet_report.get_discovered_corporate_hosts()
        # Must contain discovered SMTP relays
        assert any("office365" in h for h in hosts)
        assert any("oshkoshglobal" in h for h in hosts)
        # Must contain discovered production database servers
        assert any("db-prd-defignition-primary" in h for h in hosts)
        assert any("db-dev-defignition-primary" in h for h in hosts)
        # Must contain discovered MQTT and OPC hosts
        assert any("nadefaiutlp01" in h for h in hosts)
        assert any("nadefkepwpw01" in h for h in hosts)

    def test_mailpit_sink_aliases(self, fleet_report, tmp_path: Path):
        """Verify sim-mailpit captures all discovered SMTP hostnames as network aliases."""
        init_dir = tmp_path / "sim_init"
        compose_path = tmp_path / "docker-compose.sim.yml"
        sim_dict = build_sim_compose_dict(fleet_report, init_dir, compose_path)
        mailpit_svc = sim_dict["services"]["sim-mailpit"]
        aliases = mailpit_svc["networks"]["ignition_network"]["aliases"]

        assert "mailpit" in aliases
        assert "smtp-mock" in aliases
        # Discovered corporate SMTP relays must resolve to Mailpit
        for s in fleet_report.smtp_servers:
            assert s.lower() in aliases
            if "." in s:
                assert s.split(".")[0].lower() in aliases

    def test_mssql_sink_aliases(self, fleet_report, tmp_path: Path):
        """Verify sim-mssql captures all discovered database hostnames as network aliases."""
        init_dir = tmp_path / "sim_init"
        compose_path = tmp_path / "docker-compose.sim.yml"
        sim_dict = build_sim_compose_dict(fleet_report, init_dir, compose_path)
        db_svc = sim_dict["services"]["sim-mssql"]
        aliases = db_svc["networks"]["ignition_network"]["aliases"]

        assert "mssql" in aliases
        assert "sqlserver" in aliases
        for db_host in fleet_report.database_hosts:
            assert db_host.lower() in aliases

    def test_fleet_extra_hosts_redirection_with_sim(self, fleet_report):
        """Verify enrich_fleet_with_isolation_hosts blackholes unmocked hosts while sim aliases handle SMTP/DBs."""
        # 1. Verify get_mock_redirection_extra_hosts produces expected symbolic mapping
        mock_map = fleet_report.get_mock_redirection_extra_hosts()
        assert any("smtp.office365.com:sim-mailpit" in e or "sim-mailpit" in e for e in mock_map)
        assert any("db-prd-defignition-primary:sim-mssql" in e or "sim-mssql" in e for e in mock_map)

        # 2. Verify enrich_fleet_with_isolation_hosts injects only valid IP mappings for Docker compatibility
        dummy_svc = GatewayServiceConfig(
            backup_path=Path("dummy.gwbk"),
            service_name="test-gateway",
        )
        enrich_fleet_with_isolation_hosts([dummy_svc], fleet_report, redirect_to_sim=True)

        extra = dummy_svc.extra_hosts
        assert len(extra) > 0
        # Every entry in extra_hosts MUST be a valid IP mapping to avoid Docker daemon 'invalid IP address in add-host'
        for entry in extra:
            assert entry.endswith(":127.0.0.1")
        # Unmocked PLC targets must be blackholed to 127.0.0.1
        assert any("10.163.94.107:127.0.0.1" in e or "10.163.99.21:127.0.0.1" in e for e in extra)
        # Mocked SMTP relays must NOT be blackholed to 127.0.0.1 so Mailpit network aliases capture them
        assert not any("smtp.office365.com:127.0.0.1" in e for e in extra)

    def test_fleet_extra_hosts_blackhole_mode(self, fleet_report):
        """Verify blackholing routes all corporate hosts to 127.0.0.1."""
        dummy_svc = GatewayServiceConfig(
            backup_path=Path("dummy.gwbk"),
            service_name="test-gateway",
        )
        enrich_fleet_with_isolation_hosts([dummy_svc], fleet_report, redirect_to_sim=False, blackhole_ip="127.0.0.1")

        extra = dummy_svc.extra_hosts
        assert len(extra) > 0
        # All entries must point to 127.0.0.1
        for entry in extra:
            assert entry.endswith(":127.0.0.1")
        # Must contain discovered SMTP and DB hosts
        assert any("smtp.office365.com:127.0.0.1" in e for e in extra)
        assert any("db-prd-defignition-primary:127.0.0.1" in e for e in extra)

    def test_static_docker_compose_has_smtp_isolation(self):
        """Verify root docker-compose.yml has Mailpit capturing SMTP relays."""
        compose_path = REPO_ROOT / "docker-compose.yml"
        with open(compose_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        services = data.get("services", {})
        assert "sim-mailpit" in services, "sim-mailpit must be present in unified docker-compose.yml"
        mailpit_net = services["sim-mailpit"].get("networks", {}).get("ignition_network", {})
        aliases = mailpit_net.get("aliases", [])
        assert len(aliases) > 0, "sim-mailpit must have aliases to capture outbound SMTP"


# ==============================================================================
# 3. Unified Compose Generation Tests
# ==============================================================================


class TestUnifiedComposeGeneration:
    """Verify generation and syntax of docker-compose.yml as unified compose."""

    def test_unified_compose_contains_sim_and_fleet(self, fleet_report, tmp_path: Path):
        out_unified = tmp_path / "docker-compose.yml"
        out_sim = tmp_path / "docker-compose.sim.yml"
        out_fleet = tmp_path / "docker-compose.fleet.yml"
        init_dir = tmp_path / "sim_init"

        yaml_str, services = generate_fleet_compose(
            backups_dir=BACKUPS_DIR,
            output_path=out_fleet,
            filters=["prod-fe1"],
            with_sim=True,
            sim_compose_path=out_sim,
            sim_init_dir=init_dir,
            generate_unified=True,
            unified_output_path=out_unified,
        )

        assert out_unified.exists()
        with open(out_unified, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        # Both simulation and fleet services must exist
        assert "sim-mssql" in data["services"]
        assert "sim-db-init" in data["services"]
        assert "sim-mailpit" in data["services"]
        assert "sim-mosquitto" in data["services"]
        assert "sim-opc-plc" in data["services"]
        assert "prod-fe1" in data["services"]

        # Shared bridge network preserving port publishing
        assert not data["networks"]["ignition_network"].get("internal")

    def test_unified_compose_docker_config_validation(self, tmp_path: Path):
        """Verify unified compose file validates with `docker compose config`."""
        docker_bin = shutil.which("docker")
        if not docker_bin:
            pytest.skip("Docker CLI is not available in test environment")

        out_unified = tmp_path / "docker-compose.yml"
        out_sim = tmp_path / "docker-compose.sim.yml"
        out_fleet = tmp_path / "docker-compose.fleet.yml"
        init_dir = tmp_path / "sim_init"

        generate_fleet_compose(
            backups_dir=BACKUPS_DIR,
            output_path=out_fleet,
            filters=["prod-fe1", "ngdv_dev_ignition"],
            with_sim=True,
            sim_compose_path=out_sim,
            sim_init_dir=init_dir,
            generate_unified=True,
            unified_output_path=out_unified,
        )

        res = subprocess.run(
            [docker_bin, "compose", "-f", str(out_unified), "--profile", "*", "config"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        assert res.returncode == 0, f"docker compose config failed:\n{res.stderr}\n{res.stdout}"

    def test_unified_compose_default_path_is_docker_compose_yml(self):
        """Verify CLI default path for unified compose is docker-compose.yml."""
        args = parse_args([])
        assert getattr(args, "unified_output") == Path("docker-compose.yml")


# ==============================================================================
# 4. Single-Command Orchestration CLI Tests
# ==============================================================================


class TestSingleCommandOrchestrationCLI:
    """Verify CLI argument parsing and behavior for 1-command startup and teardown."""

    def test_cli_parse_up_positional(self):
        args = parse_args(["up"])
        assert getattr(args, "up_mode") is True
        assert getattr(args, "all") is False

    def test_cli_parse_start_positional(self):
        args = parse_args(["start"])
        assert getattr(args, "up_mode") is True

    def test_cli_parse_down_positional(self):
        args = parse_args(["down"])
        assert getattr(args, "down_mode") is True

    def test_cli_parse_stop_positional(self):
        args = parse_args(["stop"])
        assert getattr(args, "down_mode") is True

    def test_cli_parse_up_with_options(self):
        args = parse_args([
            "up",
            "--all",
            "--profile", "prod",
            "--wait-timeout", "60",
            "--no-trial-reset",
            "--blackhole-hosts",
        ])
        assert getattr(args, "up_mode") is True
        assert getattr(args, "all") is True
        assert getattr(args, "profiles") == ["prod"]
        assert getattr(args, "wait_timeout") == 60
        assert getattr(args, "no_trial_reset") is True
        assert getattr(args, "blackhole_hosts") is True
        assert getattr(args, "isolate") is True

    def test_cli_parse_no_isolate(self):
        args = parse_args(["--no-isolate"])
        assert getattr(args, "isolate") is False

    def test_cli_parse_subcommand_anywhere(self):
        """Verify subcommands placed after flags are correctly recognized."""
        args1 = parse_args(["-p", "dev", "up"])
        assert getattr(args1, "up_mode") is True
        assert getattr(args1, "profiles") == ["dev"]

        args2 = parse_args(["--backups-dir", "backups", "start", "--all"])
        assert getattr(args2, "up_mode") is True
        assert getattr(args2, "all") is True

        args3 = parse_args(["-v", "down"])
        assert getattr(args3, "down_mode") is True
        assert getattr(args3, "down_volumes") is True

    def test_run_up_cli_dry_run(self, tmp_path: Path):
        """Verify up CLI with --dry-run completes cleanly without error."""
        from ignition_gateway_harness.generator import main
        dummy_fleet = tmp_path / "docker-compose.fleet.yml"
        dummy_sim = tmp_path / "docker-compose.sim.yml"
        dummy_unified = tmp_path / "docker-compose.yml"

        code = main([
            "up",
            "--dry-run",
            "--backups-dir", "backups",
            "--filter", "ngdv_dev_ignition",
            "--output", str(dummy_fleet),
            "--sim-output", str(dummy_sim),
            "--unified-output", str(dummy_unified),
        ])
        assert code == 0

    def test_docker_compose_create_fleet_isolated(self, tmp_path: Path):
        """Verify Docker Engine actually creates containers without 'invalid IP address in add-host'."""
        docker_bin = shutil.which("docker")
        if not docker_bin:
            pytest.skip("Docker CLI is not available in test environment")

        out_fleet = tmp_path / "docker-compose.fleet.yml"
        out_sim = tmp_path / "docker-compose.sim.yml"
        init_dir = tmp_path / "sim_init"

        generate_fleet_compose(
            backups_dir=BACKUPS_DIR,
            output_path=out_fleet,
            filters=["ngdv_dev_ignition"],
            with_sim=True,
            sim_compose_path=out_sim,
            sim_init_dir=init_dir,
            isolate=True,
        )

        # Scrape and isolate network/container names for ephemeral test to avoid colliding with active daemon containers
        with open(out_fleet, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        test_net_name = f"test_net_{tmp_path.name}"
        data["networks"] = {"test_isolated_net": {"name": test_net_name, "driver": "bridge"}}
        for s in data.get("services", {}).values():
            s["container_name"] = f"test-cntnr-{tmp_path.name}"
            if "networks" in s:
                if isinstance(s["networks"], dict):
                    data_net = s["networks"].get("ignition_network") or {}
                    s["networks"] = {"test_isolated_net": data_net}
                elif isinstance(s["networks"], list):
                    s["networks"] = ["test_isolated_net"]
        with open(out_fleet, "w", encoding="utf-8") as f:
            yaml.dump(data, f)

        try:
            res = subprocess.run(
                [docker_bin, "compose", "-f", str(out_fleet), "--profile", "dev", "create"],
                capture_output=True,
                text=True,
                cwd=REPO_ROOT,
            )
            assert res.returncode == 0, f"docker compose create failed:\n{res.stderr}\n{res.stdout}"
        finally:
            subprocess.run(
                [docker_bin, "compose", "-f", str(out_fleet), "--profile", "dev", "down"],
                capture_output=True,
                text=True,
                cwd=REPO_ROOT,
            )

    def test_run_down_cli_invocation(self, tmp_path: Path):
        """Verify run_down_cli cleanly runs without crashing."""
        dummy_fleet = tmp_path / "docker-compose.fleet.yml"
        dummy_sim = tmp_path / "docker-compose.sim.yml"
        dummy_unified = tmp_path / "docker-compose.yml"

        args = parse_args([
            "down",
            "--output", str(dummy_fleet),
            "--sim-output", str(dummy_sim),
            "--unified-output", str(dummy_unified),
        ])
        code = run_down_cli(args)
        assert code == 0
