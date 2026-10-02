"""Ignition Gateway Harness - Test and fleet automation toolkit."""

from ignition_gateway_harness.generator import generate_fleet_compose, main
from ignition_gateway_harness.models import GatewayServiceConfig
from ignition_gateway_harness.exceptions import (
    FleetGeneratorError,
    PortConflictError,
    ConfigurationError,
    BackupDiscoveryError,
)
from ignition_gateway_harness.backup_analyzer import (
    analyze_backup,
    analyze_fleet,
    FleetAnalysisReport,
    GatewayBackupAnalysis,
    DatabaseConnection,
    RedundancyConfig,
    GanOutgoingConnection,
    OpcServerConnection,
    DeviceConnection,
    SmtpProfile,
    MqttConnection,
)
from ignition_gateway_harness.compose_builder import (
    build_fleet_compose_dict,
    build_unified_compose_dict,
)
from ignition_gateway_harness.sim_generator import (
    build_sim_compose_dict,
    write_simulation_stack,
    enrich_fleet_with_discovered_aliases,
    enrich_fleet_with_isolation_hosts,
    generate_init_sql,
    generate_mosquitto_conf,
    generate_mock_opc_script,
)
from ignition_gateway_harness.trial_reset import (
    TrialResetAutomator,
    TrialResetResult,
    TrialStatus,
    resolve_gateway_targets,
)

__all__ = [
    "main",
    "generate_fleet_compose",
    "build_unified_compose_dict",
    "GatewayServiceConfig",
    "FleetGeneratorError",
    "PortConflictError",
    "ConfigurationError",
    "BackupDiscoveryError",
    "analyze_backup",
    "analyze_fleet",
    "FleetAnalysisReport",
    "GatewayBackupAnalysis",
    "DatabaseConnection",
    "RedundancyConfig",
    "GanOutgoingConnection",
    "OpcServerConnection",
    "DeviceConnection",
    "SmtpProfile",
    "MqttConnection",
    "build_sim_compose_dict",
    "write_simulation_stack",
    "enrich_fleet_with_discovered_aliases",
    "enrich_fleet_with_isolation_hosts",
    "generate_init_sql",
    "generate_mosquitto_conf",
    "generate_mock_opc_script",
    "TrialResetAutomator",
    "TrialResetResult",
    "TrialStatus",
    "resolve_gateway_targets",
]

