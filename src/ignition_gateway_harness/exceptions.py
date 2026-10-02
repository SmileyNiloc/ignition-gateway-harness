"""Custom exceptions for ignition-gateway-harness fleet generator."""


class FleetGeneratorError(Exception):
    """Base exception for fleet generator errors."""


class PortConflictError(FleetGeneratorError):
    """Raised when two or more services have conflicting host ports."""


class ConfigurationError(FleetGeneratorError):
    """Raised when configuration or CLI options are invalid."""


class BackupDiscoveryError(FleetGeneratorError):
    """Raised when backup archives cannot be discovered or read."""
