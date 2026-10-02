"""Unit and integration tests for Peripheral Simulation Generator."""

import asyncio
from pathlib import Path
import struct
import tempfile
import pytest
import yaml

from ignition_gateway_harness.backup_analyzer import analyze_fleet
from ignition_gateway_harness.generator import generate_fleet_compose
from ignition_gateway_harness.models import GatewayServiceConfig
from ignition_gateway_harness.sim_generator import (
    build_sim_compose_dict,
    enrich_fleet_with_discovered_aliases,
    generate_init_sql,
    generate_mock_opc_script,
    generate_mosquitto_conf,
    write_simulation_stack,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKUPS_DIR = REPO_ROOT / "backups"


@pytest.fixture(scope="module")
def fleet_report():
    return analyze_fleet(BACKUPS_DIR)


class TestInitSqlGeneration:
    """Verify PostgreSQL / TimescaleDB DDL and database generation."""

    def test_init_sql_contains_all_discovered_databases(self, fleet_report):
        sql = generate_init_sql(fleet_report)
        for db in ["mes", "scada", "prod", "scada_tagio1", "scada_tagio2", "scada_head", "mes_dev"]:
            assert f"CREATE DATABASE {db}" in sql

    def test_init_sql_creates_discovered_users(self, fleet_report):
        sql = generate_init_sql(fleet_report)
        assert "defsvcmesdev" in sql
        assert "defsvcscadadev" in sql
        assert "defsvcprdheadprd" in sql
        assert "SUPERUSER" in sql

    def test_init_sql_historian_and_alarm_tables(self, fleet_report):
        sql = generate_init_sql(fleet_report)
        assert "CREATE TABLE IF NOT EXISTS sqlth_drv" in sql
        assert "CREATE TABLE IF NOT EXISTS sqlth_tables" in sql
        assert "CREATE TABLE IF NOT EXISTS sqlth_te" in sql
        assert "CREATE TABLE IF NOT EXISTS alarm_events" in sql
        assert "CREATE TABLE IF NOT EXISTS alarm_event_data" in sql


class TestMosquittoConf:
    """Verify Mosquitto configuration."""

    def test_mosquitto_conf_ports_and_anonymous(self):
        conf = generate_mosquitto_conf()
        assert "listener 1883 0.0.0.0" in conf
        assert "listener 1885 0.0.0.0" in conf
        assert "allow_anonymous true" in conf


def run_coro(coro):
    """Run an async coroutine, isolating from any existing thread event loop."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


class TestMockOpcProtocolHandler:
    """Verify the mock OPC-UA and CIP server script protocol logic."""

    def test_opc_hel_ack_handshake(self):
        """Simulate sending OPC-UA binary HEL packet and assert ACK response."""
        async def _run():
            script_code = generate_mock_opc_script()
            local_scope = {}
            exec(script_code, local_scope)

            handle_opc = local_scope["handle_opc_client"]

            server = await asyncio.start_server(
                lambda r, w: handle_opc(r, w, 4840),
                "127.0.0.1",
                0,
            )
            port = server.sockets[0].getsockname()[1]

            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            hel_packet = struct.pack("<4sIIIIII", b"HELF", 32, 0, 65535, 65535, 1048576, 100) + b"\x00" * 4
            writer.write(hel_packet)
            await writer.drain()

            ack_resp = await reader.read(28)
            assert len(ack_resp) == 28
            assert ack_resp[:4] == b"ACKF"

            writer.close()
            await writer.wait_closed()
            server.close()
            await server.wait_closed()

        run_coro(_run())

    def test_cip_register_session_handshake(self):
        """Simulate sending Rockwell CIP RegisterSession encapsulation command."""
        async def _run():
            script_code = generate_mock_opc_script()
            local_scope = {}
            exec(script_code, local_scope)

            handle_cip = local_scope["handle_cip_client"]

            server = await asyncio.start_server(handle_cip, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]

            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            cip_req = struct.pack("<HHII8sI", 0x0065, 4, 0, 0, b"12345678", 0) + struct.pack("<HH", 1, 0)
            writer.write(cip_req)
            await writer.drain()

            cip_resp = await reader.read(28)
            assert len(cip_resp) >= 24
            cmd, length, session, status, context, options = struct.unpack("<HHII8sI", cip_resp[:24])
            assert cmd == 0x0065
            assert status == 0
            assert session == 1
            assert context == b"12345678"

            writer.close()
            await writer.wait_closed()
            server.close()
            await server.wait_closed()

        run_coro(_run())


class TestSimComposeGeneration:
    """Verify docker-compose.sim.yml structure and network aliases."""

    def test_sim_compose_services_and_aliases(self, fleet_report, tmp_path: Path):
        init_dir = tmp_path / "sim_init"
        compose_path = tmp_path / "docker-compose.sim.yml"
        sim_dict = build_sim_compose_dict(fleet_report, init_dir, compose_path)

        services = sim_dict["services"]
        assert "sim-timescaledb" in services
        assert "sim-mailpit" in services
        assert "sim-mosquitto" in services
        assert "sim-opc-plc" in services

        # Check DB network aliases
        db_aliases = services["sim-timescaledb"]["networks"]["ignition_network"]["aliases"]
        assert "timescaledb" in db_aliases
        assert "postgres" in db_aliases
        assert any("db-dev-defignition-primary" in a for a in db_aliases)
        assert any("db-prd-defignition-primary" in a for a in db_aliases)

        # Check SMTP network aliases
        smtp_aliases = services["sim-mailpit"]["networks"]["ignition_network"]["aliases"]
        assert "mailpit" in smtp_aliases
        assert "smtp.office365.com" in smtp_aliases

        # Check Mosquitto network aliases
        mqtt_aliases = services["sim-mosquitto"]["networks"]["ignition_network"]["aliases"]
        assert "mosquitto" in mqtt_aliases
        assert any("nadef" in a for a in mqtt_aliases)

        # Check OPC aliases
        opc_aliases = services["sim-opc-plc"]["networks"]["ignition_network"]["aliases"]
        assert "opc-mock" in opc_aliases
        assert any("10.163." in a for a in opc_aliases)

        # Check network isolation (internal: true)
        assert sim_dict["networks"]["ignition_network"].get("internal") is True

    def test_write_simulation_stack(self, fleet_report, tmp_path: Path):
        compose_path = tmp_path / "docker-compose.sim.yml"
        init_dir = tmp_path / "sim_init"
        out_path, files = write_simulation_stack(fleet_report, compose_path, init_dir)

        assert out_path.exists()
        assert len(files) == 4
        assert (init_dir / "init-databases.sql").exists()
        assert (init_dir / "mosquitto.conf").exists()
        assert (init_dir / "mock_opc_server.py").exists()

        # Parse written YAML
        with open(out_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert "services" in data
        assert "sim-timescaledb" in data["services"]


class TestFleetAliasEnrichment:
    """Verify fleet services get equipped with their production hostnames."""

    def test_enrich_service_configs(self, fleet_report):
        dummy_svc = GatewayServiceConfig(
            backup_path=Path("dummy.gwbk"),
            service_name="prod-scada_master",
        )
        assert dummy_svc.gan_aliases == []

        enrich_fleet_with_discovered_aliases([dummy_svc], fleet_report)
        assert "nadefscdapw06" in dummy_svc.gan_aliases
        assert "nadefscdapw06.oshkoshglobal.com" in dummy_svc.gan_aliases

    def test_generate_fleet_compose_with_sim(self, tmp_path: Path):
        out_fleet = tmp_path / "docker-compose.fleet.yml"
        out_sim = tmp_path / "docker-compose.sim.yml"
        init_dir = tmp_path / "sim_init"

        yaml_content, services = generate_fleet_compose(
            backups_dir=BACKUPS_DIR,
            output_path=out_fleet,
            filters=["prod-scada_master", "prod-scada_backup"],
            with_sim=True,
            sim_compose_path=out_sim,
            sim_init_dir=init_dir,
        )

        assert out_fleet.exists()
        assert out_sim.exists()
        assert (init_dir / "init-databases.sql").exists()

        # Verify fleet services have aliases in rendered compose
        fleet_dict = yaml.safe_load(yaml_content)
        scada_master = fleet_dict["services"]["prod-scada_master"]
        aliases = scada_master["networks"]["ignition_network"]["aliases"]
        assert "nadefscdapw06" in aliases
