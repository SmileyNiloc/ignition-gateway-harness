"""Unit and integration tests for Source of Truth Backup Analyzer."""

import io
from pathlib import Path
import sqlite3
import tempfile
import zipfile
import pytest

from ignition_gateway_harness.backup_analyzer import (
    DatabaseConnection,
    FleetAnalysisReport,
    GanOutgoingConnection,
    GatewayBackupAnalysis,
    OpcServerConnection,
    RedundancyConfig,
    analyze_backup,
    analyze_fleet,
    find_idb_filename,
    parse_jdbc_url,
    parse_redundancy_xml,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKUPS_DIR = REPO_ROOT / "backups"


# ==============================================================================
# 1. JDBC URL Parser Unit Tests
# ==============================================================================


class TestJdbcUrlParser:
    """Verify parsing of various JDBC URL formats used in Ignition."""

    def test_sqlserver_with_port(self):
        url = "jdbc:sqlserver://10.20.30.40:1433;databaseName=ProductionSCADA;encrypt=true;"
        db_type, host, port, db_name, instance = parse_jdbc_url(url)
        assert db_type == "sqlserver"
        assert host == "10.20.30.40"
        assert port == 1433
        assert db_name == "ProductionSCADA"
        assert instance is None

    def test_sqlserver_default_port(self):
        url = "jdbc:sqlserver://DB-DEV-DEFIgnition-PRIMARY.oshkoshglobal.com"
        db_type, host, port, db_name, instance = parse_jdbc_url(url)
        assert db_type == "sqlserver"
        assert host == "DB-DEV-DEFIgnition-PRIMARY.oshkoshglobal.com"
        assert port == 1433

    def test_sqlserver_named_instance(self):
        url = "jdbc:sqlserver://DB-PRD-DEFMESTAG2-PRIMARY\\MSSQLSERVER;encrypt=true;trustServerCertificate=true;"
        db_type, host, port, db_name, instance = parse_jdbc_url(url)
        assert db_type == "sqlserver"
        assert host == "DB-PRD-DEFMESTAG2-PRIMARY"
        assert instance == "MSSQLSERVER"
        assert port == 1433

    def test_postgresql_url(self):
        url = "jdbc:postgresql://timescaledb:5432/historian?sslmode=disable"
        db_type, host, port, db_name, instance = parse_jdbc_url(url)
        assert db_type == "postgresql"
        assert host == "timescaledb"
        assert port == 5432
        assert db_name == "historian"

    def test_mysql_url(self):
        url = "jdbc:mysql://mysql-server:3306/ignition_alarms"
        db_type, host, port, db_name, instance = parse_jdbc_url(url)
        assert db_type == "mysql"
        assert host == "mysql-server"
        assert port == 3306
        assert db_name == "ignition_alarms"

    def test_sqlite_url(self):
        url = "jdbc:sqlite:C:/Program Files/Inductive Automation/Ignition/logs/system_logs.idb"
        db_type, host, port, db_name, instance = parse_jdbc_url(url)
        assert db_type == "sqlite"
        assert host == "localhost"
        assert db_name == "C:/Program Files/Inductive Automation/Ignition/logs/system_logs.idb"

    def test_sqlserver_property_based(self):
        url = "jdbc:sqlserver://;serverName=10.0.0.99;databaseName=CustomDB;port=14333"
        db_type, host, port, db_name, instance = parse_jdbc_url(url)
        assert db_type == "sqlserver"
        assert host == "10.0.0.99"
        assert port == 14333
        assert db_name == "CustomDB"

    def test_oracle_url(self):
        url = "jdbc:oracle:thin:@//ora-host:1521/PROD_SERVICE"
        db_type, host, port, db_name, instance = parse_jdbc_url(url)
        assert db_type == "oracle"
        assert host == "ora-host"
        assert port == 1521
        assert db_name == "PROD_SERVICE"

    def test_unknown_or_invalid_url(self):
        assert parse_jdbc_url("")[0] == "unknown"
        assert parse_jdbc_url("http://localhost:8088")[0] == "unknown"


# ==============================================================================
# 2. Redundancy XML Parser Tests
# ==============================================================================


class TestRedundancyXmlParser:
    """Verify parsing of redundancy.xml properties format."""

    def test_parse_master_redundancy(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE properties SYSTEM "http://java.sun.com/dtd/properties.dtd">
<properties>
<comment>Redundancy Settings</comment>
<entry key="redundancy.noderole">Master</entry>
<entry key="redundancy.gan.port">8060</entry>
<entry key="redundancy.gan.enableSsl">true</entry>
<entry key="redundancy.systemstateuid">169069b5-1208-4b49-9098-77e475d027c9</entry>
<entry key="redundancy.sync.timeoutSecs">60</entry>
</properties>
"""
        cfg = parse_redundancy_xml(xml)
        assert cfg.node_role == "Master"
        assert cfg.peer_port == 8060
        assert cfg.ssl_enabled is True
        assert cfg.system_state_uid == "169069b5-1208-4b49-9098-77e475d027c9"
        assert cfg.sync_timeout_secs == 60

    def test_parse_backup_redundancy_with_peer(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE properties SYSTEM "http://java.sun.com/dtd/properties.dtd">
<properties>
<entry key="redundancy.noderole">Backup</entry>
<entry key="redundancy.gan.host">nadefscdapw06</entry>
<entry key="redundancy.gan.port">8060</entry>
<entry key="redundancy.systemstateuid">169069b5-1208-4b49-9098-77e475d027c9</entry>
</properties>
"""
        cfg = parse_redundancy_xml(xml)
        assert cfg.node_role == "Backup"
        assert cfg.peer_host == "nadefscdapw06"
        assert cfg.peer_port == 8060
        assert cfg.system_state_uid == "169069b5-1208-4b49-9098-77e475d027c9"

    def test_malformed_xml_handling(self):
        cfg = parse_redundancy_xml("<invalid xml")
        assert cfg.node_role == "Independent"


# ==============================================================================
# 3. Real Backup Archive Deep Inspection Tests
# ==============================================================================


class TestRealBackupInspection:
    """Verify extraction of real backup archives from the backups directory."""

    @pytest.fixture(scope="module")
    def fleet_report(self) -> FleetAnalysisReport:
        return analyze_fleet(BACKUPS_DIR)


    def test_fleet_discovers_all_17_gateways(self, fleet_report: FleetAnalysisReport):
        assert len(fleet_report.gateways) == 17

    def test_discovered_database_connections(self, fleet_report: FleetAnalysisReport):
        # Must discover external MSSQL hosts
        assert len(fleet_report.database_hosts) >= 5
        assert any("oshkoshglobal.com" in h for h in fleet_report.database_hosts)

        # Discovered databases must include mes, scada, prod
        dbs = fleet_report.database_targets.get("sqlserver", set())
        assert "mes" in dbs
        assert "scada" in dbs
        assert "prod" in dbs

    def test_redundancy_pair_detection(self, fleet_report: FleetAnalysisReport):
        # 4 redundant pairs in production: SCADA, MESHead, TagIO1, TagIO2
        assert len(fleet_report.redundancy_pairs) == 4
        pairs = {p["master"]: p["backup"] for p in fleet_report.redundancy_pairs}
        assert pairs.get("prod-scada_master") == "prod-scada_backup"
        assert pairs.get("prod-tagio1_master") == "prod-tagio1_backup"
        assert pairs.get("prod-tagio2_master") == "prod-tagio2_backup"
        assert pairs.get("prod-meshead_master") == "prod-meshead_backup"

    def test_discovered_production_hostnames(self, fleet_report: FleetAnalysisReport):
        # Mappings discovered from EAM descriptions & redundancy peer hostnames
        mapping = fleet_report.discovered_host_to_service
        assert mapping.get("nadefscdapw01") == "prod-fe1"
        assert mapping.get("nadefscdapw02") == "prod-fe2"
        assert mapping.get("nadefscdapw03") == "prod-fe3"
        assert mapping.get("nadefscdapw04") == "prod-meshead_master"
        assert mapping.get("nadefscdapw05") == "prod-meshead_backup"
        assert mapping.get("nadefscdapw06") == "prod-scada_master"
        assert mapping.get("nadefscdapw07") == "prod-scada_backup"
        assert mapping.get("nadefscdapw08") == "prod-tagio1_master"
        assert mapping.get("nadefscdapw09") == "prod-tagio1_backup"
        assert mapping.get("nadefscdapw10") == "prod-tagio2_master"
        assert mapping.get("nadefscdapw11") == "prod-tagio2_backup"
        assert mapping.get("nadefscdaqw01") == "test-scada"
        assert mapping.get("nadefscdaqw02") == "test-tagio1"
        assert mapping.get("nadefscdaqw04") == "test-tagio2"
        assert mapping.get("nadefscdaqw05") == "test-meshead"
        assert mapping.get("nadefscdaqw06") == "test-fe"

    def test_opc_and_device_targets(self, fleet_report: FleetAnalysisReport):
        assert len(fleet_report.opc_endpoints) > 0
        assert any("49320" in ep for ep in fleet_report.opc_endpoints)
        assert len(fleet_report.device_targets) >= 10
        assert any(ip.startswith("10.163.") for ip in fleet_report.device_targets)

    def test_smtp_and_mqtt_brokers(self, fleet_report: FleetAnalysisReport):
        assert any("office365" in s or "oshkoshglobal" in s for s in fleet_report.smtp_servers)
        assert len(fleet_report.mqtt_brokers) > 0

    def test_report_export_methods(self, fleet_report: FleetAnalysisReport):
        d = fleet_report.to_dict()
        assert d["gateway_count"] == 17
        assert "database_targets" in d

        j = fleet_report.to_json()
        assert "prod-scada_master" in j

        md = fleet_report.to_markdown()
        assert "# Fleet Backup Analysis" in md
        assert "Redundancy Core Pairs" in md


# ==============================================================================
# 4. Synthetic & Edge Case Tests
# ==============================================================================


class TestSyntheticArchiveEdgeCases:
    """Verify behavior on synthetic, partial, or malformed zip archives."""

    def test_missing_backup_raises_error(self):
        with pytest.raises(FileNotFoundError):
            analyze_backup("non_existent_file.gwbk")

    def test_archive_without_idb(self, tmp_path: Path):
        archive_path = tmp_path / "noidb.gwbk"
        with zipfile.ZipFile(archive_path, "w") as zf:
            zf.writestr("backupinfo.xml", "<gateway-backup><version>8.1.51</version></gateway-backup>")
        analysis = analyze_backup(archive_path)
        assert analysis.ignition_version == "8.1.51"
        assert len(analysis.databases) == 0
        assert len(analysis.gan_outgoing) == 0

    def test_archive_with_synthetic_idb(self, tmp_path: Path):
        # Create a tiny sqlite db
        idb_path = tmp_path / "config.idb"
        conn = sqlite3.connect(idb_path)
        cur = conn.cursor()
        cur.execute("CREATE TABLE DATASOURCES (NAME TEXT, CONNECTURL TEXT, USERNAME TEXT, ENABLED INT);")
        cur.execute("INSERT INTO DATASOURCES VALUES ('test_ds', 'jdbc:postgresql://db:5432/testdb', 'usr', 1);")
        cur.execute("CREATE TABLE WSCONNECTIONSETTINGS (HOST TEXT, PORT INT, ENABLED INT, SSL INT, DESCRIPTION TEXT);")
        cur.execute("INSERT INTO WSCONNECTIONSETTINGS VALUES ('gw-peer', 8060, 1, 1, 'Link to peer');")
        conn.commit()
        conn.close()

        archive_path = tmp_path / "synthetic.gwbk"
        with zipfile.ZipFile(archive_path, "w") as zf:
            zf.write(idb_path, "db_backup/config.idb")

        analysis = analyze_backup(archive_path)
        assert len(analysis.databases) == 1
        assert analysis.databases[0].name == "test_ds"
        assert analysis.databases[0].db_type == "postgresql"
        assert analysis.databases[0].host == "db"
        assert len(analysis.gan_outgoing) == 1
        assert analysis.gan_outgoing[0].host == "gw-peer"

    def test_archive_with_synthetic_multi_drivers(self, tmp_path: Path):
        idb_path = tmp_path / "config.idb"
        conn = sqlite3.connect(idb_path)
        cur = conn.cursor()
        cur.execute("CREATE TABLE DEVICES (DEVICES_ID INT, NAME TEXT, DRIVERCLASSNAME TEXT, ENABLED INT);")
        cur.execute("INSERT INTO DEVICES VALUES (1, 'Modbus_1', 'com.inductiveautomation.modbus', 1);")
        cur.execute("INSERT INTO DEVICES VALUES (2, 'S7_1', 'com.inductiveautomation.s7', 1);")
        cur.execute("CREATE TABLE MODBUSTCPDRIVERSETTINGS (DEVICESETTINGSID INT, HOSTNAME TEXT, PORT INT);")
        cur.execute("INSERT INTO MODBUSTCPDRIVERSETTINGS VALUES (1, '192.168.1.100', 502);")
        cur.execute("CREATE TABLE S71500DRIVERSETTINGS (DEVICESETTINGSID INT, HOSTNAME TEXT, PORT INT);")
        cur.execute("INSERT INTO S71500DRIVERSETTINGS VALUES (2, '192.168.1.200', 102);")
        conn.commit()
        conn.close()

        archive_path = tmp_path / "synthetic_drivers.gwbk"
        with zipfile.ZipFile(archive_path, "w") as zf:
            zf.write(idb_path, "db_backup/config.idb")

        analysis = analyze_backup(archive_path)
        assert len(analysis.devices) == 2
        dev_map = {d.name: d for d in analysis.devices}
        assert dev_map["Modbus_1"].hostname == "192.168.1.100"
        assert dev_map["Modbus_1"].port == 502
        assert dev_map["Modbus_1"].driver_type == "Modbus TCP"
        assert dev_map["S7_1"].hostname == "192.168.1.200"
        assert dev_map["S7_1"].port == 102
        assert dev_map["S7_1"].driver_type == "Siemens S7-1500"
