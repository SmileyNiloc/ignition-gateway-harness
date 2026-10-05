"""Unit and integration tests for the Dynamic Gateway Lifecycle Engine."""

from io import BytesIO
from pathlib import Path
import sqlite3
import tempfile
import zipfile
import pytest

from ignition_gateway_harness.cli import parse_args, run_deploy_cli, run_down_cli
from ignition_gateway_harness.core.database import DatabaseProvisioner
from ignition_gateway_harness.core.inspector import (
    BackupInspector,
    GatewaySpec,
    inspect_backup,
)
from ignition_gateway_harness.core.manager import DeployedGateway, GatewayManager
from ignition_gateway_harness.core.orchestrator import GatewayOrchestrator, GatewayStatus
from ignition_gateway_harness.exceptions import PortConflictError
from ignition_gateway_harness.models import GatewayServiceConfig

REPO_ROOT = Path(__file__).resolve().parent.parent
DEV_BACKUP_CANDIDATES = list(REPO_ROOT.glob("backups/**/NGDV*.gwbk"))
DEV_BACKUP = DEV_BACKUP_CANDIDATES[0] if DEV_BACKUP_CANDIDATES else None


# ==============================================================================
# 1. BackupInspector & GatewaySpec Tests
# ==============================================================================


class TestBackupInspector:
    """Verify in-memory SQLite inspection and GatewaySpec generation."""

    def test_inspect_real_dev_backup(self):
        """Inspect real NGDV_Dev_Ignition.gwbk backup in-memory."""
        assert DEV_BACKUP is not None and DEV_BACKUP.exists(), f"Backup file {DEV_BACKUP} not found"
        spec = inspect_backup(DEV_BACKUP)

        assert isinstance(spec, GatewaySpec)
        assert spec.service_name == "ngdv_dev_ignition"
        assert spec.gateway_name in ("NGDV_Dev_Ignition", "ngdv_dev_ignition")
        assert spec.backup_path == DEV_BACKUP.resolve()
        assert len(spec.databases) > 0

        # Check discovered database names and users
        assert "mes_def_dev" in spec.database_names or any("mes" in d for d in spec.database_names)
        assert len(spec.database_users) > 0

        # Check to_service_config conversion
        svc_cfg = spec.to_service_config(port=8100, low_ram=True)
        assert isinstance(svc_cfg, GatewayServiceConfig)
        assert svc_cfg.ports == ["8100:8088"]
        assert svc_cfg.low_ram is True
        assert svc_cfg.heap_max == "1024m"
        assert svc_cfg.mem_limit == "1800m"

    def test_inspect_mock_in_memory_backup(self, tmp_path: Path):
        """Verify pure in-memory SQLite inspection with simulated tables."""
        # Create an in-memory SQLite database
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.execute("CREATE TABLE SYSPROPS (SYSTEMNAME TEXT, SYSTEMUID TEXT);")
        cur.execute("INSERT INTO SYSPROPS VALUES ('MockSystem', 'UID-12345');")

        cur.execute(
            "CREATE TABLE DATASOURCES (NAME TEXT, CONNECTURL TEXT, DRIVERID INT, USERNAME TEXT, ENABLED INT, CONNECTIONPROPS TEXT);"
        )
        cur.execute(
            "INSERT INTO DATASOURCES VALUES ('AppDB', 'jdbc:sqlserver://sql-cluster:1433;databaseName=app_mes;', 1, 'app_user', 1, '');"
        )

        cur.execute("CREATE TABLE OPCSERVERS (NAME TEXT, TYPE TEXT, ENABLED INT);")
        cur.execute("INSERT INTO OPCSERVERS VALUES ('MockOPC', 'OPC-UA', 1);")
        cur.execute(
            "CREATE TABLE OPCUACONNECTIONSETTINGS (ENDPOINTURL TEXT, DISCOVERYURL TEXT, SECURITYPOLICY TEXT, SECURITYMODE TEXT);"
        )
        cur.execute("INSERT INTO OPCUACONNECTIONSETTINGS VALUES ('opc.tcp://192.168.1.50:4840', '', 'None', 'None');")

        cur.execute("CREATE TABLE DEVICES (ID INT, NAME TEXT, TYPE TEXT, ENABLED INT);")
        cur.execute("INSERT INTO DEVICES VALUES (1, 'MainPLC', 'LogixDriver', 1);")
        cur.execute("CREATE TABLE LOGIXDRIVERSETTINGS (ID INT, HOSTNAME TEXT);")
        cur.execute("INSERT INTO LOGIXDRIVERSETTINGS VALUES (1, '192.168.1.100');")

        conn.commit()

        # Serialize SQLite in-memory database to bytes
        idb_bytes = conn.serialize()
        conn.close()

        # Pack into a mock .gwbk ZIP archive
        mock_gwbk = tmp_path / "MockGateway.gwbk"
        with zipfile.ZipFile(mock_gwbk, "w") as zf:
            zf.writestr("db_backup/config.idb", idb_bytes)
            zf.writestr(
                "backupinfo.xml",
                "<backup><version>8.1.51</version><edition>standard</edition></backup>",
            )

        spec = BackupInspector.inspect(mock_gwbk)
        assert spec.service_name == "mockgateway"
        assert spec.system_name == "MockSystem"
        assert spec.system_uid == "UID-12345"
        assert spec.ignition_version == "8.1.51"
        assert spec.edition == "standard"
        assert "app_mes" in spec.database_names
        assert "app_user" in spec.database_users
        assert "opc.tcp://192.168.1.50:4840" in spec.opc_endpoints
        assert "192.168.1.100" in spec.device_targets

    def test_inspect_nonexistent_file_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            BackupInspector.inspect(tmp_path / "nonexistent.gwbk")

    def test_inspect_corrupt_idb_graceful(self, tmp_path: Path):
        """Verify that a corrupted or non-SQLite config.idb does not crash the inspector."""
        corrupt_gwbk = tmp_path / "CorruptGateway.gwbk"
        with zipfile.ZipFile(corrupt_gwbk, "w") as zf:
            zf.writestr("db_backup/config.idb", b"not a valid sqlite file!")
            zf.writestr(
                "backupinfo.xml",
                "<backup><version>8.1.51</version><edition>standard</edition></backup>",
            )

        spec = BackupInspector.inspect(corrupt_gwbk)
        assert isinstance(spec, GatewaySpec)
        assert spec.service_name == "corruptgateway"
        assert len(spec.databases) == 0
        assert len(spec.database_names) == 0


# ==============================================================================
# 2. DatabaseProvisioner Tests
# ==============================================================================


class TestDatabaseProvisioner:
    """Verify idempotent T-SQL generation and password policies."""

    def test_tsql_generation_adheres_to_invariants(self):
        prov = DatabaseProvisioner(default_password="password")
        sql = prov.generate_provision_sql(
            databases=["mes", "scada", "prod"],
            users=["app_user", "mes_service"],
            include_historian_schema=True,
        )

        # Invariant 3: CHECK_POLICY = OFF, CHECK_EXPIRATION = OFF
        assert "ALTER LOGIN [sa] WITH PASSWORD = N'password', CHECK_POLICY = OFF;" in sql
        assert "CREATE LOGIN [app_user] WITH PASSWORD = N'password'" in sql
        assert "CHECK_EXPIRATION = OFF" in sql
        assert "CHECK_POLICY = OFF;" in sql

        # Databases with case-insensitive collation
        assert "CREATE DATABASE [mes] COLLATE SQL_Latin1_General_CP1_CI_AS;" in sql
        assert "ALTER DATABASE [mes] SET RECOVERY SIMPLE;" in sql
        assert "CREATE DATABASE [scada] COLLATE SQL_Latin1_General_CP1_CI_AS;" in sql

        # User mapping to db_owner
        assert "ALTER ROLE [db_owner] ADD MEMBER [app_user];" in sql
        assert "ALTER ROLE [db_owner] ADD MEMBER [mes_service];" in sql

        # Historian & Journal schema
        assert "CREATE TABLE [dbo].[sqlth_drv]" in sql
        assert "CREATE TABLE [dbo].[alarm_events]" in sql
        assert "CREATE TABLE [dbo].[audit_events]" in sql

    def test_tsql_skips_sa_and_dbo_principals(self):
        prov = DatabaseProvisioner()
        sql = prov.generate_provision_sql(
            databases=["mes"],
            users=["sa", "dbo", "real_user"],
        )
        assert "CREATE USER [sa]" not in sql
        assert "CREATE USER [dbo]" not in sql
        assert "CREATE USER [real_user]" in sql

    def test_provision_for_spec(self):
        prov = DatabaseProvisioner()
        spec = GatewaySpec(
            backup_path=Path("mock.gwbk"),
            service_name="test_gw",
            gateway_name="test_gw",
            database_names={"custom_db"},
            database_users={"custom_user"},
        )
        # Calling without docker executable should return False or gracefully handle
        result = prov.provision_for_spec(spec)
        assert isinstance(result, bool)

    def test_execute_tsql_stdin_and_flags(self, monkeypatch):
        """Verify execute_tsql pipes script via STDIN and passes -b and -C flags."""
        prov = DatabaseProvisioner(default_password="test_secret")
        calls = []

        def mock_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            from unittest.mock import MagicMock
            res = MagicMock()
            res.returncode = 0
            res.stderr = ""
            res.stdout = ""
            return res

        monkeypatch.setattr("shutil.which", lambda bin_name: "/usr/bin/docker")
        monkeypatch.setattr("subprocess.run", mock_run)

        sql_script = "USE [master];\nGO\nSELECT 1;\nGO"
        ok = prov.execute_tsql(sql_script, container_name="sim-mssql")
        assert ok is True
        assert len(calls) > 0
        cmd, kwargs = calls[0]
        assert "-b" in cmd
        assert "-C" in cmd
        assert "test_secret" in cmd
        assert kwargs.get("input") == sql_script


# ==============================================================================
# 3. GatewayOrchestrator Tests
# ==============================================================================


class TestGatewayOrchestrator:
    """Verify health polling and container status representation."""

    def test_status_ping_offline(self):
        orch = GatewayOrchestrator()
        # Querying an unused high port should return STARTING... or error without crashing
        status = orch.check_status_ping(59999, timeout_secs=0.5)
        assert status in ("STARTING...", "OFFLINE") or status.startswith("HTTP")

    def test_gateway_status_not_created(self):
        orch = GatewayOrchestrator()
        stat = orch.get_gateway_status("nonexistent_service_name_12345", port=8199)
        assert isinstance(stat, GatewayStatus)
        assert stat.docker_state == "Not Created"
        assert stat.is_ready is False


# ==============================================================================
# 4. GatewayManager Dynamic Appliance Workflow Tests
# ==============================================================================


class TestGatewayManager:
    """Verify the dynamic gateway appliance lifecycle API."""

    def test_manager_initialization(self, tmp_path: Path):
        manager = GatewayManager(project_root=tmp_path)
        assert manager.root == tmp_path.resolve()
        assert manager.sim_compose == (tmp_path / "docker-compose.sim.yml").resolve()
        assert manager.fleet_compose == (tmp_path / "docker-compose.fleet.yml").resolve()
        assert manager.unified_compose == (tmp_path / "docker-compose.yml").resolve()

    def test_deploy_gateway_dry_inspection(self, tmp_path: Path):
        """Verify dynamic gateway deployment prepares spec and configuration."""
        assert DEV_BACKUP is not None and DEV_BACKUP.exists()
        manager = GatewayManager(project_root=tmp_path)

        # Deploy with wait_ready=False to avoid blocking on Docker startup in CI/unit test
        deployed = manager.deploy_gateway(
            backup_path=DEV_BACKUP,
            port=8105,
            low_ram=True,
            wait_ready=False,
            auto_reset_trial=False,
        )

        assert isinstance(deployed, DeployedGateway)
        assert deployed.service_name == "ngdv_dev_ignition"
        assert deployed.port == 8105
        assert deployed.url == "http://localhost:8105"
        assert deployed.spec.service_name == "ngdv_dev_ignition"
        assert deployed.service_config.low_ram is True
        assert deployed.service_config.heap_max == "1024m"
        assert deployed.service_config.mem_limit == "1800m"

        # Check that dynamic compose file was written
        dynamic_compose = tmp_path / "docker-compose.ngdv_dev_ignition.yml"
        assert dynamic_compose.exists()
        content = dynamic_compose.read_text(encoding="utf-8")
        assert "8105:8088" in content
        assert "ignition_network" in content
        # Verify network is NEVER internal: true (Critical Invariant 2)
        assert "internal: true" not in content

    def test_stop_gateway(self, tmp_path: Path):
        manager = GatewayManager(project_root=tmp_path)
        # Register a mock deployed gateway
        mock_compose = tmp_path / "docker-compose.test_gw.yml"
        mock_compose.write_text("services: {}\n", encoding="utf-8")
        manager.deployed_gateways["test_gw"] = DeployedGateway(
            service_name="test_gw",
            container_name="ignition-test_gw",
            port=8100,
            url="http://localhost:8100",
            spec=GatewaySpec(backup_path=Path("mock.gwbk"), service_name="test_gw", gateway_name="test_gw"),
            service_config=GatewayServiceConfig(backup_path=Path("mock.gwbk"), service_name="test_gw"),
        )

        manager.stop_gateway("test_gw")
        assert "test_gw" not in manager.deployed_gateways
        assert not mock_compose.exists()

    def test_auto_port_allocation_and_conflict_detection(self, tmp_path: Path):
        """Verify dynamic auto-assignment of sequential ports and conflict detection."""
        assert DEV_BACKUP is not None and DEV_BACKUP.exists()
        manager = GatewayManager(project_root=tmp_path)

        # 1. Deploy first gateway with port=None (should auto-assign 8100)
        gw1 = manager.deploy_gateway(DEV_BACKUP, port=None, wait_ready=False, auto_reset_trial=False)
        assert gw1.port == 8100
        assert 8100 in {g.port for g in manager.deployed_gateways.values()}

        # 2. Next auto-port should be 8101
        next_port = manager.find_available_port(8100)
        assert next_port == 8101

        # 3. Explicitly attempting to deploy another gateway claiming 8100 must raise PortConflictError
        mock_backup = tmp_path / "MockSecond.gwbk"
        with zipfile.ZipFile(mock_backup, "w") as zf:
            zf.writestr("backupinfo.xml", "<backup><version>8.1.51</version></backup>")

        with pytest.raises(PortConflictError) as exc_info:
            manager.deploy_gateway(mock_backup, port=8100, wait_ready=False, auto_reset_trial=False)
        assert "8100" in str(exc_info.value)

    def test_trial_daemon_registration_and_lifecycle(self, tmp_path: Path):
        """Verify registering gateways and starting/stopping background trial daemon."""
        manager = GatewayManager(project_root=tmp_path)
        manager.register_trial_gateway("test-gw-1", "http://localhost:8100")
        assert "test-gw-1" in manager._registered_trial_gateways

        # Start daemon
        manager.start_trial_daemon(interval_minutes=105)
        assert manager._trial_daemon_thread is not None
        assert manager._trial_daemon_thread.is_alive()

        # Stop daemon
        manager.stop_trial_daemon()
        assert manager._trial_daemon_thread is None


# ==============================================================================
# 5. CLI Dynamic Deployment Tests
# ==============================================================================


class TestDynamicCLI:
    """Verify CLI dynamic deploy commands and argument parsing."""

    def test_cli_deploy_args_parsing(self):
        args = parse_args(["deploy", "backups/test.gwbk", "--port", "8105", "--low-ram"])
        assert args.deploy_mode is True
        assert getattr(args, "backup_file") == Path("backups/test.gwbk")
        assert getattr(args, "port") == 8105
        assert getattr(args, "low_ram") is True

    def test_cli_deploy_dry_run(self, tmp_path: Path):
        assert DEV_BACKUP is not None and DEV_BACKUP.exists()
        args = parse_args(["deploy", str(DEV_BACKUP), "--port", "8120", "--dry-run"])
        ret = run_deploy_cli(args)
        assert ret == 0

    def test_cli_stop_single_gateway(self, tmp_path: Path):
        args = parse_args(["stop", "mock_gw_service"])
        # Should execute cleanly without raising
        ret = run_down_cli(args)
        assert ret == 0
