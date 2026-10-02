"""Source of Truth Backup Analyzer for Ignition Gateway backups (.gwbk).

Extracts and aggregates configuration from embedded SQLite configuration databases
(config.idb) and configuration XML files (redundancy.xml, gateway.xml, backupinfo.xml).
Enables the test harness to treat gateway backups as the authoritative source of truth,
automatically discovering required database connections, redundancy partners, GAN routes,
OPC-UA endpoints, field devices, MQTT brokers, and SMTP profiles.
"""

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any, Dict, List, Optional, Set, Tuple
import xml.etree.ElementTree as ET
import zipfile

from ignition_gateway_harness.discovery import clean_identifier, derive_service_identity


# ==============================================================================
# Data Models for Discovered Configuration
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
class GatewayBackupAnalysis:
    """Complete configuration analysis for a single gateway backup archive."""

    backup_path: Path
    service_name: str
    gateway_name: str
    system_name: Optional[str] = None
    system_uid: Optional[str] = None
    ignition_version: Optional[str] = None
    databases: List[DatabaseConnection] = field(default_factory=list)
    redundancy: RedundancyConfig = field(default_factory=RedundancyConfig)
    gan_outgoing: List[GanOutgoingConnection] = field(default_factory=list)
    gan_remote_providers: List[GanTagProvider] = field(default_factory=list)
    opc_servers: List[OpcServerConnection] = field(default_factory=list)
    devices: List[DeviceConnection] = field(default_factory=list)
    smtp_profiles: List[SmtpProfile] = field(default_factory=list)
    mqtt_connections: List[MqttConnection] = field(default_factory=list)
    hostname_aliases: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert analysis to JSON-serializable dictionary."""
        d = asdict(self)
        d["backup_path"] = str(self.backup_path)
        return d


@dataclass
class FleetAnalysisReport:
    """Aggregated analysis across all fleet gateway backups."""

    gateways: Dict[str, GatewayBackupAnalysis] = field(default_factory=dict)
    all_external_hosts: Set[str] = field(default_factory=set)
    database_targets: Dict[str, Set[str]] = field(default_factory=dict)  # db_type -> set of names
    database_hosts: Set[str] = field(default_factory=set)
    redundancy_pairs: List[Dict[str, Any]] = field(default_factory=list)
    gan_network_topology: Dict[str, List[str]] = field(default_factory=dict)
    discovered_host_to_service: Dict[str, str] = field(default_factory=dict)
    opc_endpoints: Set[str] = field(default_factory=set)
    device_targets: Set[str] = field(default_factory=set)
    mqtt_brokers: Set[str] = field(default_factory=set)
    smtp_servers: Set[str] = field(default_factory=set)

    def get_discovered_corporate_hosts(self) -> Set[str]:
        """Aggregate all discovered corporate and external hostnames across all backups.

        Includes databases, SMTP mail relays, MQTT brokers, OPC servers, PLCs, and redundancy peers.
        Automatically includes both short hostname and FQDN (.oshkoshglobal.com) variants.
        """
        hosts: Set[str] = set()
        hosts.update(h.lower() for h in self.all_external_hosts if h)
        hosts.update(h.lower() for h in self.database_hosts if h)
        hosts.update(h.lower() for h in self.smtp_servers if h)
        hosts.update(h.lower() for h in self.mqtt_brokers if h)

        for ep in self.opc_endpoints:
            m = re.match(r"opc\.tcp://([^:/]+)", ep)
            if m and m.group(1).lower() not in ("localhost", "127.0.0.1"):
                hosts.add(m.group(1).lower())

        for dev in self.device_targets:
            if dev.lower() not in ("localhost", "127.0.0.1"):
                hosts.add(dev.lower())

        for pair in self.redundancy_pairs:
            ph = pair.get("peer_host")
            if ph and ph.lower() not in ("localhost", "127.0.0.1"):
                hosts.add(ph.lower())

        for host in self.discovered_host_to_service:
            if host.lower() not in ("localhost", "127.0.0.1"):
                hosts.add(host.lower())

        # Expand short names to corporate FQDN and FQDN to short names
        expanded: Set[str] = set()
        for h in hosts:
            expanded.add(h)
            if "." in h and not re.match(r"^\d+\.\d+\.\d+\.\d+$", h):
                short = h.split(".")[0]
                if short:
                    expanded.add(short)
            elif "." not in h and not re.match(r"^\d+\.\d+\.\d+\.\d+$", h):
                expanded.add(f"{h}.oshkoshglobal.com")

        # Exclude loopback / local aliases
        expanded.discard("localhost")
        expanded.discard("127.0.0.1")
        expanded.discard("0.0.0.0")
        return expanded

    def get_blackhole_extra_hosts(self, ip: str = "127.0.0.1") -> List[str]:
        """Return list of 'hostname:ip' entries blackholing all discovered external hosts."""
        return [f"{h}:{ip}" for h in sorted(self.get_discovered_corporate_hosts())]

    def get_mock_redirection_extra_hosts(
        self,
        smtp_target: str = "sim-mailpit",
        db_target: str = "sim-timescaledb",
        mqtt_target: str = "sim-mosquitto",
        opc_target: str = "sim-opc-plc",
        unmocked_ip: str = "127.0.0.1",
    ) -> List[str]:
        """Return extra_hosts mapping corporate hostnames to mock containers or blackhole IP."""
        entries: Dict[str, str] = {}
        # 1. SMTP hosts -> mailpit
        for s in self.smtp_servers:
            entries[s.lower()] = smtp_target
            if "." in s:
                entries[s.split(".")[0].lower()] = smtp_target

        # 2. Database hosts -> timescaledb/postgres
        for d in self.database_hosts:
            entries[d.lower()] = db_target
            if "." in d:
                entries[d.split(".")[0].lower()] = db_target

        # 3. MQTT brokers -> mosquitto
        for m in self.mqtt_brokers:
            entries[m.lower()] = mqtt_target
            if "." in m:
                entries[m.split(".")[0].lower()] = mqtt_target

        # 4. OPC / PLC -> opc mock
        for ep in self.opc_endpoints:
            match = re.match(r"opc\.tcp://([^:/]+)", ep)
            if match and match.group(1).lower() not in ("localhost", "127.0.0.1"):
                h = match.group(1).lower()
                entries[h] = opc_target
                if "." in h and not re.match(r"^\d+\.\d+\.\d+\.\d+$", h):
                    entries[h.split(".")[0]] = opc_target

        for dev in self.device_targets:
            if dev.lower() not in ("localhost", "127.0.0.1"):
                entries[dev.lower()] = opc_target

        # 5. Redundancy peers -> respective gateway services
        for host, svc in self.discovered_host_to_service.items():
            entries[host.lower()] = svc
            if "." not in host:
                entries[f"{host.lower()}.oshkoshglobal.com"] = svc

        # 6. Any other external host blackholed
        for h in self.get_discovered_corporate_hosts():
            if h not in entries:
                entries[h] = unmocked_ip

        return [f"{h}:{target}" for h, target in sorted(entries.items())]

    def to_dict(self) -> Dict[str, Any]:
        """Convert fleet analysis to dictionary."""
        return {
            "gateway_count": len(self.gateways),
            "gateways": {k: v.to_dict() for k, v in self.gateways.items()},
            "all_external_hosts": sorted(list(self.all_external_hosts)),
            "database_targets": {k: sorted(list(v)) for k, v in self.database_targets.items()},
            "database_hosts": sorted(list(self.database_hosts)),
            "redundancy_pairs": self.redundancy_pairs,
            "gan_network_topology": self.gan_network_topology,
            "discovered_host_to_service": self.discovered_host_to_service,
            "opc_endpoints": sorted(list(self.opc_endpoints)),
            "device_targets": sorted(list(self.device_targets)),
            "mqtt_brokers": sorted(list(self.mqtt_brokers)),
            "smtp_servers": sorted(list(self.smtp_servers)),
        }

    def to_json(self, indent: int = 2) -> str:
        """Export report as formatted JSON string."""
        return json.dumps(self.to_dict(), indent=indent)

    def to_markdown(self) -> str:
        """Generate comprehensive markdown summary of fleet dependencies."""
        lines = [
            "# Fleet Backup Analysis & Source-of-Truth Dependency Report",
            f"**Total Gateways Inspected**: {len(self.gateways)}",
            "",
            "## 1. External Database Connections Expected by Backups",
            "| Gateway | Connection Name | Type | Host | Port | Database | User |",
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
        ]
        for gw_name, analysis in sorted(self.gateways.items()):
            for db in analysis.databases:
                lines.append(
                    f"| {gw_name} | {db.name} | {db.db_type} | {db.host or '-'} | "
                    f"{db.port or '-'} | {db.database or '-'} | {db.username or '-'} |"
                )

        lines.extend([
            "",
            "## 2. Redundancy Core Pairs",
            "| Master Node | Backup Node | Peer Host | Shared System UID |",
            "| :--- | :--- | :--- | :--- |",
        ])
        for pair in self.redundancy_pairs:
            lines.append(
                f"| {pair.get('master', '-')} | {pair.get('backup', '-')} | "
                f"{pair.get('peer_host', '-')} | {pair.get('system_uid', '-')} |"
            )

        lines.extend([
            "",
            "## 3. Discovered Production Hostname to Harness Service Mappings",
            "| Production Hostname | Docker Fleet Service | Discovered Via |",
            "| :--- | :--- | :--- |",
        ])
        for host, svc in sorted(self.discovered_host_to_service.items()):
            lines.append(f"| `{host}` | `{svc}` | GAN Description / Redundancy Peer |")

        lines.extend([
            "",
            "## 4. Discovered OPC-UA & Field Devices",
            f"- **Unique OPC Endpoints**: {len(self.opc_endpoints)}",
        ])
        for ep in sorted(self.opc_endpoints):
            lines.append(f"  - `{ep}`")

        lines.append(f"- **Unique PLC Device IPs (CIP/Logix)**: {len(self.device_targets)}")
        for dev in sorted(self.device_targets):
            lines.append(f"  - `{dev}`")

        lines.extend([
            "",
            "## 5. External Peripherals (SMTP & MQTT)",
            "- **SMTP Servers**:",
        ])
        for smtp in sorted(self.smtp_servers):
            lines.append(f"  - `{smtp}`")
        lines.append("- **MQTT Brokers**:")
        for mqtt in sorted(self.mqtt_brokers):
            lines.append(f"  - `{mqtt}`")

        return "\n".join(lines)


# ==============================================================================
# Helper Functions & Parsers
# ==============================================================================


def parse_jdbc_url(url: str) -> Tuple[str, Optional[str], Optional[int], Optional[str], Optional[str]]:
    """
    Parse a JDBC URL into (db_type, host, port, database, instance).

    Supports:
    - SQL Server: jdbc:sqlserver://host[:port][\\instance];databaseName=dbname;...
    - PostgreSQL: jdbc:postgresql://host[:port]/database
    - MySQL/MariaDB: jdbc:mysql://host[:port]/database
    - Oracle: jdbc:oracle:thin:@host:port:sid or @//host:port/service
    - SQLite: jdbc:sqlite:filepath
    """
    if not url or not url.startswith("jdbc:"):
        return "unknown", None, None, None, None

    url_body = url[5:]  # strip 'jdbc:'
    subprotocol, _, rest = url_body.partition(":")

    subprotocol = subprotocol.lower()

    if subprotocol == "sqlite":
        return "sqlite", "localhost", None, rest.strip(), None

    if subprotocol in ("sqlserver", "microsoft:sqlserver"):
        # Format: //host[\instance][:port][;databaseName=dbname][;key=val...]
        clean_rest = rest.lstrip("/")
        # Extract host part before semicolon
        host_part, _, params = clean_rest.partition(";")
        instance = None
        port = None
        host = host_part

        if "\\" in host_part:
            host, instance = host_part.split("\\", 1)
            if ":" in instance:
                instance, port_str = instance.split(":", 1)
                try:
                    port = int(port_str)
                except ValueError:
                    port = None
        elif ":" in host_part:
            host, port_str = host_part.split(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                port = None

        if port is None:
            port = 1433

        # Parse parameters like databaseName=XYZ, database=XYZ, or serverName=XYZ
        database = None
        if params:
            for param in params.split(";"):
                if "=" in param:
                    k, v = param.split("=", 1)
                    k_lower = k.strip().lower().replace(" ", "")
                    if k_lower in ("databasename", "database", "initialcatalog"):
                        database = v.strip()
                    elif k_lower in ("servername", "server", "host") and not host:
                        host = v.strip()
                    elif k_lower in ("portnumber", "port") and port == 1433:
                        try:
                            port = int(v.strip())
                        except ValueError:
                            pass
                    elif k_lower in ("instancename", "instance") and not instance:
                        instance = v.strip()

        return "sqlserver", host.strip() if host else None, port, database, instance

    if subprotocol in ("postgresql", "postgres"):
        # Format: //host[:port]/database[?params]
        clean_rest = rest.lstrip("/")
        host_port, _, db_part = clean_rest.partition("/")
        db_name, _, _ = db_part.partition("?")
        host = host_port
        port = 5432
        if ":" in host_port:
            host, port_str = host_port.split(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                port = 5432
        return "postgresql", host.strip(), port, db_name.strip() if db_name else None, None

    if subprotocol in ("mysql", "mariadb"):
        clean_rest = rest.lstrip("/")
        host_port, _, db_part = clean_rest.partition("/")
        db_name, _, _ = db_part.partition("?")
        host = host_port
        port = 3306
        if ":" in host_port:
            host, port_str = host_port.split(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                port = 3306
        return subprotocol, host.strip(), port, db_name.strip() if db_name else None, None

    if subprotocol == "oracle":
        # Format: jdbc:oracle:thin:@[//]host[:port]/service or @host:port:sid
        clean = rest.lstrip(":")
        if clean.lower().startswith("thin:"):
            clean = clean[5:]
        elif clean.lower().startswith("oci:"):
            clean = clean[4:]
        clean = clean.lstrip("@").lstrip("/")
        if "/" in clean:
            host_port, _, svc = clean.partition("/")
            host = host_port
            port = 1521
            if ":" in host_port:
                host, port_str = host_port.split(":", 1)
                try:
                    port = int(port_str)
                except ValueError:
                    port = 1521
            return "oracle", host.strip(), port, svc.strip() if svc else None, None
        elif ":" in clean:
            parts = clean.split(":")
            if len(parts) >= 3:
                return "oracle", parts[0].strip(), int(parts[1]) if parts[1].isdigit() else 1521, parts[2].strip(), None
            elif len(parts) == 2:
                return "oracle", parts[0].strip(), 1521, parts[1].strip(), None
        return "oracle", clean.strip() if clean else None, 1521, None, None

    # Generic fallback
    return subprotocol, None, None, None, None


def find_idb_filename(zf: zipfile.ZipFile) -> Optional[str]:
    """Find the internal SQLite configuration database path inside a .gwbk archive."""
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
    except Exception:
        pass
    return cfg


def extract_idb_tables_data(idb_path: str) -> Dict[str, List[Dict[str, Any]]]:
    """Read all tables from extracted SQLite idb file into memory dictionaries."""
    conn = sqlite3.connect(idb_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    result: Dict[str, List[Dict[str, Any]]] = {}
    try:
        tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
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
        for t in target_tables:
            if t in tables:
                try:
                    cur.execute(f"SELECT * FROM {t}")
                    rows = cur.fetchall()
                    result[t] = [dict(r) for r in rows]
                except Exception:
                    result[t] = []
    finally:
        conn.close()
    return result


# ==============================================================================
# Main Analyzer Functions
# ==============================================================================


def analyze_backup(backup_path: Path | str, backups_dir: Optional[Path | str] = None) -> GatewayBackupAnalysis:
    """
    Perform deep inspection of a .gwbk backup archive.

    Extracts configuration from db_backup/config.idb, redundancy.xml, and backupinfo.xml.
    """
    bpath = Path(backup_path).resolve()
    if not bpath.exists() or not bpath.is_file():
        raise FileNotFoundError(f"Backup file not found: {bpath}")

    bdir = Path(backups_dir).resolve() if backups_dir else bpath.parent
    svc_name, sys_name_default, _ = derive_service_identity(bpath, bdir)

    analysis = GatewayBackupAnalysis(
        backup_path=bpath,
        service_name=svc_name,
        gateway_name=clean_identifier(bpath.parent.name if bpath.name.lower() in ("backup.gwbk", "gateway.gwbk") else bpath.stem),
        system_name=sys_name_default,
    )

    with zipfile.ZipFile(bpath, "r") as zf:
        # 1. Parse backupinfo.xml for version
        if "backupinfo.xml" in zf.namelist():
            try:
                root = ET.fromstring(zf.read("backupinfo.xml").decode("utf-8", errors="replace"))
                analysis.ignition_version = root.findtext("version")
            except Exception:
                pass

        # 2. Parse redundancy.xml
        if "redundancy.xml" in zf.namelist():
            xml_text = zf.read("redundancy.xml").decode("utf-8", errors="replace")
            analysis.redundancy = parse_redundancy_xml(xml_text)

        # 3. Locate and extract config.idb
        idb_name = find_idb_filename(zf)
        if idb_name:
            idb_bytes = zf.read(idb_name)
            with tempfile.NamedTemporaryFile(suffix=".idb", delete=False) as tmp:
                tmp.write(idb_bytes)
                tmp_path = tmp.name

            try:
                idb_data = extract_idb_tables_data(tmp_path)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

            # Process SYSPROPS
            sysprops = idb_data.get("SYSPROPS", [])
            if sysprops:
                sp = sysprops[0]
                if sp.get("SYSTEMNAME"):
                    analysis.system_name = sp.get("SYSTEMNAME")
                if sp.get("SYSTEMUID"):
                    analysis.system_uid = sp.get("SYSTEMUID")

            # Process DATASOURCES
            for row in idb_data.get("DATASOURCES", []):
                url = row.get("CONNECTURL", "")
                db_type, host, port, db_name, instance = parse_jdbc_url(url)
                if not db_name and row.get("NAME"):
                    # Common convention in Ignition: datasource name matches database
                    db_name = row.get("NAME").lower()

                analysis.databases.append(
                    DatabaseConnection(
                        name=row.get("NAME", "unnamed"),
                        connect_url=url,
                        driver=str(row.get("DRIVERID")) if row.get("DRIVERID") is not None else None,
                        username=row.get("USERNAME"),
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
                    # Try to infer target service hint from description (e.g. "NGDV_Front_End_1" -> "prod-fe1")
                    hint = None
                    if desc:
                        m = re.search(r"to\s+([a-zA-Z0-9_-]+)", desc, re.IGNORECASE)
                        if m:
                            hint = clean_identifier(m.group(1))

                    analysis.gan_outgoing.append(
                        GanOutgoingConnection(
                            host=host.strip(),
                            port=int(row.get("PORT", 8060)),
                            ssl=bool(row.get("SSL", 1)),
                            description=desc,
                            target_service_hint=hint,
                            enabled=bool(row.get("ENABLED", 1)),
                        )
                    )

            # Process GANTAGPROVIDERSETTINGS
            for row in idb_data.get("GANTAGPROVIDERSETTINGS", []):
                analysis.gan_remote_providers.append(
                    GanTagProvider(
                        name=str(row.get("PROFILEID")),
                        remote_gateway_name=row.get("SERVERNAME"),
                        remote_provider_name=row.get("PROVIDERNAME"),
                        history_provider_name=row.get("HISTORYPROVIDERNAME"),
                        alarm_status_enabled=bool(row.get("ALARMSTATUSENABLED", 0)),
                    )
                )

            # Process OPCSERVERS & OPCUACONNECTIONSETTINGS
            opc_names: Dict[int, str] = {}
            for row in idb_data.get("OPCSERVERS", []):
                server_id = row.get("OPCSERVERS_ID")
                s_name = row.get("NAME", f"OPC_{server_id}")
                s_type = row.get("TYPE")
                if server_id is not None:
                    opc_names[server_id] = s_name
                analysis.opc_servers.append(
                    OpcServerConnection(
                        name=s_name,
                        server_type=s_type,
                    )
                )

            # Correlate OPCUACONNECTIONSETTINGS
            for t in ["OPCUACONNECTIONSETTINGS", "OPCUACONNECTIONSETTINGS2"]:
                for row in idb_data.get(t, []):
                    sid = row.get("SERVERSETTINGSID")
                    s_name = opc_names.get(sid, f"OPC_UA_{sid}")
                    ep_url = row.get("ENDPOINTURL")
                    disc_url = row.get("DISCOVERYURL")
                    sec_policy = row.get("SECURITYPOLICY")
                    sec_mode = row.get("SECURITYMODE")
                    enabled = bool(row.get("ENABLED", 1))

                    # Update existing record or append
                    matched = False
                    for opc in analysis.opc_servers:
                        if opc.name == s_name:
                            opc.endpoint_url = ep_url
                            opc.discovery_url = disc_url
                            opc.security_policy = sec_policy
                            opc.security_mode = sec_mode
                            opc.enabled = enabled
                            matched = True
                            break
                    if not matched:
                        analysis.opc_servers.append(
                            OpcServerConnection(
                                name=s_name,
                                endpoint_url=ep_url,
                                discovery_url=disc_url,
                                security_policy=sec_policy,
                                security_mode=sec_mode,
                                enabled=enabled,
                            )
                        )

            # Process DEVICES & Driver Settings
            device_names: Dict[int, str] = {}
            for row in idb_data.get("DEVICES", []):
                did = row.get("DEVICES_ID")
                dname = row.get("NAME", f"Device_{did}")
                dclass = row.get("DRIVERCLASSNAME", "generic")
                if did is not None:
                    device_names[did] = dname
                analysis.devices.append(
                    DeviceConnection(
                        name=dname,
                        driver_type=dclass,
                        enabled=bool(row.get("ENABLED", 1)),
                    )
                )

            # Correlate driver tables (Logix, Modbus, S7, TCP)
            driver_table_specs = [
                ("LOGIXDRIVERSETTINGS", "ControlLogix / CompactLogix (EtherNet/IP)", 44818),
                ("CONTROLLOGIXDRIVERSETTINGS", "ControlLogix (EtherNet/IP)", 44818),
                ("COMPACTLOGIXDRIVERSETTINGS", "CompactLogix (EtherNet/IP)", 44818),
                ("MICROLOGIXDRIVERSETTINGS", "MicroLogix (EtherNet/IP)", 44818),
                ("SLCDRIVERSETTINGS", "SLC 500 (EtherNet/IP)", 44818),
                ("PLC5DRIVERSETTINGS", "PLC-5 (EtherNet/IP)", 44818),
                ("MODBUSTCPDRIVERSETTINGS", "Modbus TCP", 502),
                ("S71500DRIVERSETTINGS", "Siemens S7-1500", 102),
                ("S71200DRIVERSETTINGS", "Siemens S7-1200", 102),
                ("S7300DRIVERSETTINGS", "Siemens S7-300", 102),
                ("S7400DRIVERSETTINGS", "Siemens S7-400", 102),
                ("TCPDRIVERSETTINGS", "TCP Driver", None),
            ]
            for tbl, d_desc, def_port in driver_table_specs:
                for row in idb_data.get(tbl, []):
                    did = row.get("DEVICESETTINGSID") if row.get("DEVICESETTINGSID") is not None else row.get("DEVICES_ID")
                    host = row.get("HOSTNAME") or row.get("HOST")
                    port = row.get("PORT", def_port)
                    slot = row.get("SLOTNUMBER", 0)
                    dname = device_names.get(did, f"{tbl}_{did}")
                    matched = False
                    for d in analysis.devices:
                        if d.name == dname:
                            d.hostname = host
                            d.port = int(port) if port is not None else def_port
                            d.slot_number = slot
                            d.driver_type = d_desc
                            matched = True
                            break
                    if not matched and host:
                        analysis.devices.append(
                            DeviceConnection(
                                name=dname,
                                driver_type=d_desc,
                                hostname=host,
                                port=int(port) if port is not None else def_port,
                                slot_number=slot,
                            )
                        )

            # Process SMTP
            for t in ["CLASSICSMTPEMAILPROFILES", "SMTPSETTINGS"]:
                for row in idb_data.get(t, []):
                    host = row.get("HOSTNAME")
                    if host:
                        port = row.get("PORT", 25)
                        analysis.smtp_profiles.append(
                            SmtpProfile(
                                name=row.get("NAME"),
                                hostname=host.strip(),
                                port=int(port) if port is not None else 25,
                                use_ssl=bool(row.get("USESSLPORT", 0) or row.get("SSLENABLED", 0)),
                                use_tls=bool(row.get("STARTTLSENABLED", 0)),
                                username=row.get("USERNAME"),
                            )
                        )

            # Process MQTT
            for row in idb_data.get("ENGINESERVERRECORD", []):
                url = row.get("URL")
                if url:
                    analysis.mqtt_connections.append(
                        MqttConnection(
                            name=row.get("NAME"),
                            url=url.strip(),
                            record_type="engine",
                            enabled=bool(row.get("ENABLED", 1)),
                        )
                    )
            for row in idb_data.get("TRANSMISSIONSERVERRECORD", []):
                url = row.get("URL")
                if url:
                    analysis.mqtt_connections.append(
                        MqttConnection(
                            name=row.get("NAME"),
                            url=url.strip(),
                            record_type="transmission",
                            enabled=bool(row.get("MQTTSERVERCONNECTIONENABLED", 1)),
                        )
                    )

    return analysis


def analyze_fleet(
    backups_dir: Path | str,
    filters: Optional[List[str]] = None,
) -> FleetAnalysisReport:
    """
    Scan backups directory, inspect all .gwbk files, and build a unified FleetAnalysisReport.

    Correlates redundancy pairs, discovers production hostnames from GAN descriptions,
    and maps out all external dependencies.
    """
    bdir = Path(backups_dir).resolve()
    from ignition_gateway_harness.discovery import find_all_gwbk_files, matches_filter

    all_backups = find_all_gwbk_files(bdir)
    report = FleetAnalysisReport()

    for bpath in all_backups:
        # Check filters
        if filters:
            rel_name = bpath.name
            parent_name = bpath.parent.name
            stem = bpath.stem
            matched = any(
                matches_filter([rel_name, parent_name, stem, str(bpath)], f)
                for f in filters
            )
            if not matched:
                continue

        analysis = analyze_backup(bpath, backups_dir=bdir)
        report.gateways[analysis.service_name] = analysis

        # Aggregate databases
        for db in analysis.databases:
            if db.db_type != "unknown" and db.db_type != "sqlite":
                if db.database:
                    report.database_targets.setdefault(db.db_type, set()).add(db.database.lower())
                if db.host and db.host != "localhost" and db.host != "127.0.0.1":
                    report.database_hosts.add(db.host.lower())
                    report.all_external_hosts.add(db.host.lower())

        # Aggregate OPC endpoints
        for opc in analysis.opc_servers:
            if opc.endpoint_url:
                report.opc_endpoints.add(opc.endpoint_url)
                # extract host if standard opc.tcp://host:port
                m = re.match(r"opc\.tcp://([^:/]+)", opc.endpoint_url)
                if m and m.group(1) not in ("localhost", "127.0.0.1"):
                    report.all_external_hosts.add(m.group(1).lower())

        # Aggregate Devices
        for dev in analysis.devices:
            if dev.hostname and dev.hostname not in ("localhost", "127.0.0.1"):
                report.device_targets.add(dev.hostname)
                report.all_external_hosts.add(dev.hostname.lower())

        # Aggregate SMTP
        for smtp in analysis.smtp_profiles:
            if smtp.hostname and smtp.hostname not in ("localhost", "127.0.0.1"):
                report.smtp_servers.add(smtp.hostname.lower())
                report.all_external_hosts.add(smtp.hostname.lower())

        # Aggregate MQTT
        for mqtt in analysis.mqtt_connections:
            if mqtt.url:
                m = re.match(r"(?:tcp|ssl|ws|wss)://([^:/]+)", mqtt.url)
                if m and m.group(1) not in ("localhost", "127.0.0.1"):
                    report.mqtt_brokers.add(m.group(1).lower())
                    report.all_external_hosts.add(m.group(1).lower())

    # Canonical mapping helper from GAN description to service name
    def match_target_description(desc: str, host: str = "") -> Optional[str]:
        desc_upper = desc.upper()
        host_upper = host.upper()
        # Find portion after "TO "
        if " TO " in desc_upper:
            target = desc_upper.split(" TO ")[-1].strip()
        else:
            target = desc_upper

        # QA / Test tier convention: host contains QW or target contains TEST
        is_test = "QW" in host_upper or "TEST" in target

        if "FRONT_END_1" in target or "FE1" in target:
            return "prod-fe1"
        if "FRONT_END_2" in target or "FE2" in target:
            return "prod-fe2"
        if "FRONT_END_3" in target or "FE3" in target:
            return "prod-fe3"
        if "SCADA" in target:
            if is_test:
                return "test-scada"
            if "BACKUP" in target:
                return "prod-scada_backup"
            return "prod-scada_master"
        if "MES_HEAD" in target or "MESHEAD" in target or "MES" in target:
            if is_test:
                return "test-meshead"
            if "BACKUP" in target:
                return "prod-meshead_backup"
            return "prod-meshead_master"
        if "TAGIO1" in target or "TAG_IO_1" in target:
            if is_test:
                return "test-tagio1"
            if "BACKUP" in target:
                return "prod-tagio1_backup"
            return "prod-tagio1_master"
        if "TAGIO2" in target or "TAG_IO_2" in target:
            if is_test:
                return "test-tagio2"
            if "BACKUP" in target:
                return "prod-tagio2_backup"
            return "prod-tagio2_master"
        if "TEST_FRONT_END" in target or "TEST_FE" in target or (is_test and "FRONT" in target):
            return "test-fe"
        if "DEV" in target or "DW" in host_upper:
            return "ngdv_dev_ignition"
        return None

    for analysis in report.gateways.values():
        gan_targets = []
        for g in analysis.gan_outgoing:
            gan_targets.append(f"{g.host}:{g.port}")
            if g.description and g.host:
                host_lower = g.host.lower()
                matched_svc = match_target_description(g.description, g.host)
                if matched_svc and matched_svc in report.gateways:
                    report.discovered_host_to_service[host_lower] = matched_svc


        report.gan_network_topology[analysis.service_name] = gan_targets

    # Discover and correlate redundancy pairs
    masters: Dict[str, GatewayBackupAnalysis] = {}
    backups: Dict[str, GatewayBackupAnalysis] = {}

    for name, ga in report.gateways.items():
        name_lower = name.lower()
        if "_backup" in name_lower or "-backup" in name_lower:
            backups[name] = ga
        elif "_master" in name_lower or "-master" in name_lower:
            masters[name] = ga
        elif ga.redundancy.node_role.lower() == "backup":
            backups[name] = ga
        elif ga.redundancy.node_role.lower() == "master":
            masters[name] = ga


    for bname, b_ga in backups.items():
        # Match by shared UID or matching service prefix
        matched_master = None
        for mname, m_ga in masters.items():
            if (
                b_ga.redundancy.system_state_uid
                and m_ga.redundancy.system_state_uid
                and b_ga.redundancy.system_state_uid == m_ga.redundancy.system_state_uid
            ):
                matched_master = mname
                break
            # Match by prefix (e.g. prod-scada_backup matches prod-scada_master)
            b_prefix = bname.replace("_backup", "").replace("-backup", "")
            m_prefix = mname.replace("_master", "").replace("-master", "")
            if b_prefix == m_prefix:
                matched_master = mname
                break

        peer_host = b_ga.redundancy.peer_host
        if matched_master and peer_host:
            report.discovered_host_to_service[peer_host.lower()] = matched_master

        report.redundancy_pairs.append({
            "backup": bname,
            "master": matched_master or "unknown",
            "peer_host": peer_host,
            "system_uid": b_ga.redundancy.system_state_uid or b_ga.system_uid,
        })

    # Assign hostname aliases to each gateway analysis
    for host, svc in report.discovered_host_to_service.items():
        if svc in report.gateways:
            if host not in report.gateways[svc].hostname_aliases:
                report.gateways[svc].hostname_aliases.append(host)
                # Also add FQDN variant if simple host
                if "." not in host:
                    report.gateways[svc].hostname_aliases.append(f"{host}.oshkoshglobal.com")

    return report
