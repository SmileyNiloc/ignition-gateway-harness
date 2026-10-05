"""Pure in-memory backup inspection and SQLite config.idb parser.

Inspects .gwbk archives directly in memory using SQLite deserialize,
discovering database connections, users, OPC endpoints, redundancy,
and network topology to produce a strongly-typed GatewaySpec.
"""

from dataclasses import dataclass, field
import io
import logging
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any, Dict, List, Optional, Set, Tuple
import xml.etree.ElementTree as ET
import zipfile

from ignition_gateway_harness.discovery import clean_identifier, derive_service_identity
from ignition_gateway_harness.models import GatewayServiceConfig

logger = logging.getLogger(__name__)


# ==============================================================================
# Discovered Resource Data Models
# ==============================================================================


@dataclass
class DatabaseConnection:
    """Discovered database connection from DATASOURCES table."""

    name: str
    connect_url: str
    driver: Optional[str] = None
    username: Optional[str] = None
    db_type: str = "unknown"
    host: Optional[str] = None
    port: Optional[int] = None
    database: Optional[str] = None
    instance: Optional[str] = None
    enabled: bool = True


@dataclass
class RedundancyConfig:
    """Discovered redundancy configuration from redundancy.xml / config.idb."""

    node_role: str = "Independent"  # Master, Backup, Independent
    peer_host: Optional[str] = None
    peer_port: int = 8060
    system_state_uid: Optional[str] = None
    ssl_enabled: bool = True
    active_history_level: Optional[str] = None
    standby_activity_level: Optional[str] = None
    sync_timeout_secs: int = 60


@dataclass
class GanOutgoingConnection:
    """Discovered Gateway Area Network (GAN) outgoing link from WSCONNECTIONSETTINGS."""

    host: str
    port: int = 8060
    ssl: bool = True
    description: Optional[str] = None
    target_service_hint: Optional[str] = None
    enabled: bool = True


@dataclass
class GanTagProvider:
    """Discovered remote tag provider routed across GAN from GANTAGPROVIDERSETTINGS."""

    name: Optional[str] = None
    remote_gateway_name: Optional[str] = None
    remote_provider_name: Optional[str] = None
    history_provider_name: Optional[str] = None
    alarm_status_enabled: bool = False


@dataclass
class OpcServerConnection:
    """Discovered OPC-UA or legacy OPC connection from OPCSERVERS / OPCUACONNECTIONSETTINGS."""

    name: str
    endpoint_url: Optional[str] = None
    discovery_url: Optional[str] = None
    security_policy: Optional[str] = None
    security_mode: Optional[str] = None
    server_type: Optional[str] = None
    enabled: bool = True


@dataclass
class DeviceConnection:
    """Discovered PLC or field device connection from DEVICES and driver tables."""

    name: str
    driver_type: str
    hostname: Optional[str] = None
    port: Optional[int] = None
    slot_number: Optional[int] = None
    enabled: bool = True


@dataclass
class SmtpProfile:
    """Discovered SMTP email profile from CLASSICSMTPEMAILPROFILES / SMTPSETTINGS."""

    name: Optional[str] = None
    hostname: Optional[str] = None
    port: int = 25
    use_ssl: bool = False
    use_tls: bool = False
    username: Optional[str] = None


@dataclass
class MqttConnection:
    """Discovered MQTT broker link from Cirrus Link module tables."""

    name: Optional[str] = None
    url: Optional[str] = None
    record_type: str = "engine"  # engine, transmission, distributor
    enabled: bool = True


@dataclass
class GatewaySpec:
    """Strongly-typed specification and dependency manifest for an Ignition gateway backup."""

    backup_path: Path
    service_name: str
    gateway_name: str
    system_name: Optional[str] = None
    system_uid: Optional[str] = None
    ignition_version: Optional[str] = None
    databases: List[DatabaseConnection] = field(default_factory=list)
    database_names: Set[str] = field(default_factory=set)
    database_users: Set[str] = field(default_factory=set)
    database_hosts: Set[str] = field(default_factory=set)
    redundancy: RedundancyConfig = field(default_factory=RedundancyConfig)
    gan_outgoing: List[GanOutgoingConnection] = field(default_factory=list)
    gan_remote_providers: List[GanTagProvider] = field(default_factory=list)
    opc_servers: List[OpcServerConnection] = field(default_factory=list)
    opc_endpoints: Set[str] = field(default_factory=set)
    devices: List[DeviceConnection] = field(default_factory=list)
    device_targets: Set[str] = field(default_factory=set)
    smtp_profiles: List[SmtpProfile] = field(default_factory=list)
    smtp_servers: Set[str] = field(default_factory=set)
    mqtt_connections: List[MqttConnection] = field(default_factory=list)
    mqtt_brokers: Set[str] = field(default_factory=set)
    hostname_aliases: List[str] = field(default_factory=list)
    edition: str = "standard"
    suggested_port: Optional[int] = None

    def to_service_config(
        self,
        port: int = 8100,
        low_ram: bool = True,
        restore: bool = True,
        profiles: Optional[List[str]] = None,
    ) -> GatewayServiceConfig:
        """Create a GatewayServiceConfig representation from this spec."""
        cfg = GatewayServiceConfig(
            backup_path=self.backup_path,
            service_name=self.service_name,
            system_name=self.system_name or self.service_name,
            hostname=self.service_name,
            ports=[f"{port}:8088"],
            low_ram=low_ram,
            restore=restore,
            profiles=profiles or ["dev"],
            gan_aliases=list(self.hostname_aliases),
        )
        if low_ram:
            cfg.heap_max = "1024m"
            cfg.mem_limit = "1800m"
        return cfg


# ==============================================================================
# Helper Parsers
# ==============================================================================


def parse_jdbc_url(url: str) -> Tuple[str, Optional[str], Optional[int], Optional[str], Optional[str]]:
    """Parse a JDBC URL into (db_type, host, port, database, instance)."""
    if not url or not url.startswith("jdbc:"):
        return "unknown", None, None, None, None

    remainder = url[len("jdbc:"):]

    if remainder.startswith("sqlserver://"):
        body = remainder[len("sqlserver://"):]
        host_port = body.split(";")[0]
        instance = None
        host = host_port
        port = None

        if "\\" in host_port:
            parts = host_port.split("\\", 1)
            host = parts[0]
            instance = parts[1]
        elif ":" in host_port:
            parts = host_port.split(":", 1)
            host = parts[0]
            try:
                port = int(parts[1])
            except ValueError:
                port = None

        db_name = None
        params = body.split(";")[1:]
        for p in params:
            if "=" in p:
                k, v = p.split("=", 1)
                k_clean = k.strip().lower()
                if k_clean in ("databasename", "database"):
                    db_name = v.strip()
                elif k_clean == "instancename":
                    instance = v.strip()

        return "mssql", host or None, port or 1433, db_name, instance

    if remainder.startswith("postgresql://"):
        body = remainder[len("postgresql://"):]
        m = re.match(r"^([^:/]+)(?::(\d+))?(?:/(.*))?$", body)
        if m:
            host = m.group(1)
            port = int(m.group(2)) if m.group(2) else 5432
            db_name = m.group(3).split("?")[0] if m.group(3) else None
            return "postgresql", host, port, db_name, None
        return "postgresql", None, 5432, None, None

    if remainder.startswith("mysql://") or remainder.startswith("mariadb://"):
        prefix_len = len("mysql://") if remainder.startswith("mysql://") else len("mariadb://")
        body = remainder[prefix_len:]
        m = re.match(r"^([^:/]+)(?::(\d+))?(?:/(.*))?$", body)
        if m:
            host = m.group(1)
            port = int(m.group(2)) if m.group(2) else 3306
            db_name = m.group(3).split("?")[0] if m.group(3) else None
            return "mysql", host, port, db_name, None
        return "mysql", None, 3306, None, None

    if remainder.startswith("oracle:"):
        body = remainder[len("oracle:"):]
        m = re.match(r".*@//?([^:/]+)(?::(\d+))?/(.+)", body)
        if m:
            host = m.group(1)
            port = int(m.group(2)) if m.group(2) else 1521
            db_name = m.group(3)
            return "oracle", host, port, db_name, None
        m2 = re.match(r".*@([^:/]+)(?::(\d+))?:(.+)", body)
        if m2:
            host = m2.group(1)
            port = int(m2.group(2)) if m2.group(2) else 1521
            db_name = m2.group(3)
            return "oracle", host, port, db_name, None
        return "oracle", None, 1521, None, None

    if remainder.startswith("sqlite:"):
        path = remainder[len("sqlite:"):]
        return "sqlite", "localhost", None, path, None

    return "generic", None, None, None, None


def find_idb_filename(zf: zipfile.ZipFile) -> Optional[str]:
    """Find the SQLite configuration IDB file inside the backup archive."""
    candidates = [
        "db_backup/config.idb",
        "db_backup_sqlite.idb",
        "config.idb",
        "data/db/config.idb",
        "db/config.idb",
    ]
    namelist = zf.namelist()
    for cand in candidates:
        if cand in namelist:
            return cand
    for name in namelist:
        if name.endswith(".idb") and "log" not in name.lower():
            return name
    return None


def parse_redundancy_xml(xml_content: str) -> RedundancyConfig:
    """Parse Java XML properties format from redundancy.xml."""
    cfg = RedundancyConfig()
    try:
        root = ET.fromstring(xml_content)
        entries = {e.get("key"): e.text for e in root.findall("entry") if e.get("key")}
        if "redundancy.noderole" in entries:
            cfg.node_role = entries["redundancy.noderole"] or "Independent"
        if "redundancy.gan.host" in entries and entries["redundancy.gan.host"]:
            cfg.peer_host = entries["redundancy.gan.host"].strip()
        if "redundancy.gan.port" in entries and entries["redundancy.gan.port"]:
            try:
                cfg.peer_port = int(entries["redundancy.gan.port"])
            except ValueError:
                cfg.peer_port = 8060
        if "redundancy.systemstateuid" in entries:
            cfg.system_state_uid = entries["redundancy.systemstateuid"]
        if "redundancy.gan.enableSsl" in entries:
            cfg.ssl_enabled = entries["redundancy.gan.enableSsl"].lower() == "true"
        if "redundancy.activehistorylevel" in entries:
            cfg.active_history_level = entries["redundancy.activehistorylevel"]
        if "redundancy.standbyactivitylevel" in entries:
            cfg.standby_activity_level = entries["redundancy.standbyactivitylevel"]
        if "redundancy.sync.timeoutSecs" in entries:
            try:
                cfg.sync_timeout_secs = int(entries["redundancy.sync.timeoutSecs"])
            except ValueError:
                cfg.sync_timeout_secs = 60
    except Exception as exc:
        logger.debug("Could not parse redundancy.xml: %s", exc)
    return cfg


def extract_idb_tables_in_memory(idb_bytes: bytes) -> Dict[str, List[Dict[str, Any]]]:
    """Read all tables from SQLite idb bytes into in-memory dictionaries.

    Uses sqlite3.Connection.deserialize() for pure zero-disk in-memory inspection.
    Falls back to temporary file if deserialization fails.
    """
    result: Dict[str, List[Dict[str, Any]]] = {}
    target_tables = [
        "DATASOURCES",
        "WSCONNECTIONSETTINGS",
        "WSINCOMINGCONNECTION",
        "GANTAGPROVIDERSETTINGS",
        "REMOTEHISTORIANSETTINGS",
        "OPCSERVERS",
        "OPCUACONNECTIONSETTINGS",
        "OPCUACONNECTIONSETTINGS2",
        "DEVICES",
        "LOGIXDRIVERSETTINGS",
        "CONTROLLOGIXDRIVERSETTINGS",
        "COMPACTLOGIXDRIVERSETTINGS",
        "MICROLOGIXDRIVERSETTINGS",
        "SLCDRIVERSETTINGS",
        "PLC5DRIVERSETTINGS",
        "S71500DRIVERSETTINGS",
        "S71200DRIVERSETTINGS",
        "S7300DRIVERSETTINGS",
        "S7400DRIVERSETTINGS",
        "MODBUSTCPDRIVERSETTINGS",
        "TCPDRIVERSETTINGS",
        "CLASSICSMTPEMAILPROFILES",
        "SMTPSETTINGS",
        "EMAILPROFILES",
        "ENGINESERVERRECORD",
        "TRANSMISSIONSERVERRECORD",
        "DISTRIBUTORGENERALRECORD",
        "IDP_ADAPTERS",
        "SYSPROPS",
    ]

    conn: Optional[sqlite3.Connection] = None
    tmp_path: Optional[str] = None

    try:
        conn = sqlite3.connect(":memory:")
        if hasattr(conn, "deserialize"):
            conn.deserialize(idb_bytes)
        else:
            conn.close()
            with tempfile.NamedTemporaryFile(suffix=".idb", delete=False) as tmp:
                tmp.write(idb_bytes)
                tmp_path = tmp.name
            conn = sqlite3.connect(tmp_path)
    except Exception as exc:
        logger.debug("In-memory SQLite deserialize fallback to tempfile: %s", exc)
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        with tempfile.NamedTemporaryFile(suffix=".idb", delete=False) as tmp:
            tmp.write(idb_bytes)
            tmp_path = tmp.name
        conn = sqlite3.connect(tmp_path)

    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]

        for t in target_tables:
            if t in tables:
                try:
                    cur.execute(f"SELECT * FROM {t}")
                    rows = cur.fetchall()
                    result[t] = [dict(r) for r in rows]
                except Exception as exc:
                    logger.debug("Failed querying table %s: %s", t, exc)
                    result[t] = []
    except (sqlite3.DatabaseError, sqlite3.Error, Exception) as exc:
        logger.debug("Failed opening or querying SQLite IDB archive: %s", exc)
        return {}
    finally:
        if conn:
            conn.close()
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError as exc:
                logger.debug("Failed unlinking temp IDB: %s", exc)

    return result


# ==============================================================================
# Core Inspector Class
# ==============================================================================


class BackupInspector:
    """Inspects Ignition gateway backup archives and extracts typed specifications."""

    @staticmethod
    def inspect(
        backup_path: Path | str,
        backups_dir: Optional[Path | str] = None,
    ) -> GatewaySpec:
        """Inspect a single .gwbk archive in-memory and return a strongly-typed GatewaySpec."""
        bpath = Path(backup_path).resolve()
        if not bpath.exists() or not bpath.is_file():
            raise FileNotFoundError(f"Backup file not found: {bpath}")

        bdir = Path(backups_dir).resolve() if backups_dir else bpath.parent
        svc_name, sys_name_default, _ = derive_service_identity(bpath, bdir)

        spec = GatewaySpec(
            backup_path=bpath,
            service_name=svc_name,
            gateway_name=clean_identifier(
                bpath.parent.name if bpath.name.lower() in ("backup.gwbk", "gateway.gwbk") else bpath.stem
            ),
            system_name=sys_name_default,
        )

        with zipfile.ZipFile(bpath, "r") as zf:
            # 1. Parse backupinfo.xml
            if "backupinfo.xml" in zf.namelist():
                try:
                    root = ET.fromstring(zf.read("backupinfo.xml").decode("utf-8", errors="replace"))
                    spec.ignition_version = root.findtext("version")
                    edition_text = root.findtext("edition")
                    if edition_text:
                        spec.edition = edition_text.lower()
                except Exception as exc:
                    logger.debug("Error parsing backupinfo.xml: %s", exc)

            # 2. Parse redundancy.xml
            if "redundancy.xml" in zf.namelist():
                xml_text = zf.read("redundancy.xml").decode("utf-8", errors="replace")
                spec.redundancy = parse_redundancy_xml(xml_text)

            # 3. Locate and inspect config.idb in memory
            idb_name = find_idb_filename(zf)
            if not idb_name:
                logger.warning("No config.idb found in backup: %s", bpath.name)
                return spec

            idb_bytes = zf.read(idb_name)
            idb_data = extract_idb_tables_in_memory(idb_bytes)

            # Process SYSPROPS
            sysprops = idb_data.get("SYSPROPS", [])
            if sysprops:
                sp = sysprops[0]
                if sp.get("SYSTEMNAME"):
                    spec.system_name = sp.get("SYSTEMNAME")
                if sp.get("SYSTEMUID"):
                    spec.system_uid = sp.get("SYSTEMUID")

            # Process DATASOURCES
            for row in idb_data.get("DATASOURCES", []):
                url = row.get("CONNECTURL", "")
                db_type, host, port, db_name, instance = parse_jdbc_url(url)

                conn_props = row.get("CONNECTIONPROPS", "") or ""
                if not db_name and conn_props:
                    for part in conn_props.split(";"):
                        if "=" in part:
                            k, v = part.split("=", 1)
                            if k.strip().lower() in ("databasename", "database", "initialcatalog"):
                                db_name = v.strip()
                                break

                if not db_name and row.get("NAME"):
                    db_name = row.get("NAME").lower()

                user = row.get("USERNAME")
                if user:
                    spec.database_users.add(user.strip())

                if db_name:
                    clean_name = db_name.lower().replace("-", "_").strip()
                    if clean_name and clean_name not in ("master", "tempdb", "model", "msdb"):
                        spec.database_names.add(clean_name)

                if host:
                    spec.database_hosts.add(host)

                spec.databases.append(
                    DatabaseConnection(
                        name=row.get("NAME", "unnamed"),
                        connect_url=url,
                        driver=str(row.get("DRIVERID")) if row.get("DRIVERID") is not None else None,
                        username=user,
                        db_type=db_type,
                        host=host,
                        port=port,
                        database=db_name,
                        instance=instance,
                        enabled=bool(row.get("ENABLED", 1)),
                    )
                )

            # Process WSCONNECTIONSETTINGS (GAN Outgoing)
            for row in idb_data.get("WSCONNECTIONSETTINGS", []):
                host = row.get("HOST")
                if host:
                    desc = row.get("DESCRIPTION")
                    spec.gan_outgoing.append(
                        GanOutgoingConnection(
                            host=host.strip(),
                            port=int(row.get("PORT", 8060)),
                            ssl=bool(row.get("SSL", 1)),
                            description=desc,
                            enabled=bool(row.get("ENABLED", 1)),
                        )
                    )

            # Process GANTAGPROVIDERSETTINGS
            for row in idb_data.get("GANTAGPROVIDERSETTINGS", []):
                spec.gan_remote_providers.append(
                    GanTagProvider(
                        name=row.get("NAME"),
                        remote_gateway_name=row.get("REMOTEGATEWAYNAME"),
                        remote_provider_name=row.get("REMOTEPROVIDERNAME"),
                        history_provider_name=row.get("HISTORYPROVIDERNAME"),
                        alarm_status_enabled=bool(row.get("ALARMSTATUSENABLED", 0)),
                    )
                )

            # Process OPCSERVERS & OPCUACONNECTIONSETTINGS
            for row in idb_data.get("OPCSERVERS", []):
                name = row.get("NAME", "OPC-UA")
                spec.opc_servers.append(
                    OpcServerConnection(
                        name=name,
                        server_type=row.get("TYPE"),
                        enabled=bool(row.get("ENABLED", 1)),
                    )
                )
            for row in idb_data.get("OPCUACONNECTIONSETTINGS", []) + idb_data.get("OPCUACONNECTIONSETTINGS2", []):
                ep = row.get("ENDPOINTURL")
                disc = row.get("DISCOVERYURL")
                if ep:
                    spec.opc_endpoints.add(ep)
                for s in spec.opc_servers:
                    if ep and not s.endpoint_url:
                        s.endpoint_url = ep
                        s.discovery_url = disc
                        s.security_policy = row.get("SECURITYPOLICY")
                        s.security_mode = row.get("SECURITYMODE")
                        break

            # Process DEVICES & Driver Settings
            driver_tables = {
                "LOGIXDRIVERSETTINGS": ("hostname", "host"),
                "CONTROLLOGIXDRIVERSETTINGS": ("hostname", "host"),
                "COMPACTLOGIXDRIVERSETTINGS": ("hostname", "host"),
                "MICROLOGIXDRIVERSETTINGS": ("hostname", "host"),
                "SLCDRIVERSETTINGS": ("hostname", "host"),
                "PLC5DRIVERSETTINGS": ("hostname", "host"),
                "S71500DRIVERSETTINGS": ("hostname", "host"),
                "S71200DRIVERSETTINGS": ("hostname", "host"),
                "S7300DRIVERSETTINGS": ("hostname", "host"),
                "S7400DRIVERSETTINGS": ("hostname", "host"),
                "MODBUSTCPDRIVERSETTINGS": ("hostname", "host"),
                "TCPDRIVERSETTINGS": ("hostname", "host"),
            }
            driver_host_map: Dict[int, str] = {}
            for t_name, fields_to_check in driver_tables.items():
                for row in idb_data.get(t_name, []):
                    rec_id = row.get(f"{t_name}_ID") or row.get("ID")
                    if rec_id is not None:
                        for f in fields_to_check:
                            val = row.get(f) or row.get(f.upper())
                            if val:
                                driver_host_map[rec_id] = str(val).strip()
                                break

            for row in idb_data.get("DEVICES", []):
                dev_id = row.get("DEVICES_ID") or row.get("ID")
                d_type = row.get("TYPE", "unknown")
                dev_host = driver_host_map.get(dev_id)
                if dev_host:
                    spec.device_targets.add(dev_host)
                spec.devices.append(
                    DeviceConnection(
                        name=row.get("NAME", "unnamed_device"),
                        driver_type=d_type,
                        hostname=dev_host,
                        enabled=bool(row.get("ENABLED", 1)),
                    )
                )

            # Process SMTP Profiles
            for row in idb_data.get("CLASSICSMTPEMAILPROFILES", []) + idb_data.get("SMTPSETTINGS", []) + idb_data.get("EMAILPROFILES", []):
                host = row.get("HOSTNAME") or row.get("HOST")
                if host:
                    spec.smtp_servers.add(host.strip())
                    spec.smtp_profiles.append(
                        SmtpProfile(
                            name=row.get("NAME"),
                            hostname=host.strip(),
                            port=int(row.get("PORT", 25)),
                            use_ssl=bool(row.get("USESSL", 0)),
                            use_tls=bool(row.get("STARTTLS", 0)),
                            username=row.get("USERNAME"),
                        )
                    )

            # Process MQTT
            for row in idb_data.get("ENGINESERVERRECORD", []) + idb_data.get("TRANSMISSIONSERVERRECORD", []) + idb_data.get("DISTRIBUTORGENERALRECORD", []):
                url = row.get("URL") or row.get("SERVERURL")
                if url:
                    spec.mqtt_connections.append(
                        MqttConnection(
                            name=row.get("NAME"),
                            url=url.strip(),
                            enabled=bool(row.get("ENABLED", 1)),
                        )
                    )
                    m = re.match(r"(?:tcp|ssl|mqtt|mqtts)://([^:/]+)", url)
                    if m:
                        spec.mqtt_brokers.add(m.group(1).strip())

            # Redundancy Aliases
            if spec.redundancy and spec.redundancy.peer_host:
                ph = spec.redundancy.peer_host.strip()
                if ph and ph.lower() not in ("localhost", "127.0.0.1"):
                    spec.hostname_aliases.append(ph)

        return spec


def inspect_backup(
    backup_path: Path | str,
    backups_dir: Optional[Path | str] = None,
) -> GatewaySpec:
    """Convenience function to inspect a backup archive and return a GatewaySpec."""
    return BackupInspector.inspect(backup_path, backups_dir=backups_dir)
