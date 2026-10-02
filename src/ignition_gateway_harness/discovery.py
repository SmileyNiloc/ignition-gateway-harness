"""Backup discovery and default metadata derivation."""

import fnmatch
from pathlib import Path
import re
from typing import List, Optional, Set, Tuple

from ignition_gateway_harness.exceptions import BackupDiscoveryError


def find_all_gwbk_files(backups_dir: Path | str) -> List[Path]:
    """Discover all .gwbk files recursively within backups_dir."""
    path = Path(backups_dir).resolve()
    if not path.exists():
        raise BackupDiscoveryError(f"Backups directory does not exist: {path}")
    if not path.is_dir():
        raise BackupDiscoveryError(f"Backups path is not a directory: {path}")

    return sorted([p.resolve() for p in path.rglob("*.gwbk") if p.is_file()], key=lambda p: str(p))


def clean_identifier(name: str) -> str:
    """Strip common backup date/timestamp suffixes from folder or file stems."""
    # Strip suffixes like _2026-10-01, _10-01-2026, -10-01-2026, or _20261001-1038
    cleaned = re.sub(r"[-_]\d{4}[-_]\d{2}[-_]\d{2}(?:[-_]\d+)?$", "", name)
    cleaned = re.sub(r"[-_]\d{2}[-_]\d{2}[-_]\d{4}$", "", cleaned)
    cleaned = re.sub(r"[-_]backup[-_]\d{8}(?:[-_]\d+)?$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[-_]\d{8}(?:[-_]\d+)?$", "", cleaned)
    return cleaned if cleaned else name


def sanitize_service_name(raw_name: str) -> str:
    """Convert raw name to a valid lowercase Docker Compose service name."""
    sanitized = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw_name)
    sanitized = re.sub(r"-+", "-", sanitized).strip("-").lower()
    return sanitized if sanitized else "gateway"


def derive_service_identity(
    backup_path: Path, backups_dir: Path, existing_service_names: Optional[Set[str]] = None
) -> Tuple[str, str, List[str]]:
    """Derive (service_name, system_name, default_profiles) from backup path."""
    backups_dir_resolved = backups_dir.resolve()
    parent = backup_path.parent

    generic_backup_names = {"backup.gwbk", "gateway.gwbk", "restore.gwbk", "ignition.gwbk"}
    if backup_path.name.lower() in generic_backup_names and parent != backups_dir_resolved and parent.name:
        raw_name = parent.name
    else:
        raw_name = backup_path.stem

    cleaned_name = clean_identifier(raw_name)
    system_name = cleaned_name
    service_name = sanitize_service_name(cleaned_name)

    # Collision avoidance
    if existing_service_names is not None:
        if service_name in existing_service_names:
            # Fall back to using the full raw name
            fallback = sanitize_service_name(raw_name)
            if fallback not in existing_service_names:
                service_name = fallback
            else:
                idx = 2
                candidate = f"{service_name}-{idx}"
                while candidate in existing_service_names:
                    idx += 1
                    candidate = f"{service_name}-{idx}"
                service_name = candidate

    # Derive default profiles based on naming conventions
    upper_name = raw_name.upper()
    profiles: List[str] = []
    if "PROD" in upper_name:
        profiles.append("prod")
    elif "TEST" in upper_name:
        profiles.append("test")
    elif "DEV" in upper_name:
        profiles.append("dev")
    else:
        profiles.append("default")

    return service_name, system_name, profiles


def find_gateway_yaml(backup_path: Path) -> Optional[Path]:
    """Find the gateway.yaml override file associated with a backup."""
    candidates = [
        backup_path.with_name(f"{backup_path.stem}.gateway.yaml"),
        backup_path.with_name(f"{backup_path.stem}.gateway.yml"),
        backup_path.parent / "gateway.yaml",
        backup_path.parent / "gateway.yml",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def matches_filter(candidate_strings: List[str], filter_pattern: str) -> bool:
    """Check if any candidate string matches the filter pattern (glob, regex, or substring)."""
    pattern_lower = filter_pattern.strip().lower()
    for s in candidate_strings:
        s_lower = s.lower()
        if pattern_lower in s_lower:
            return True
        if fnmatch.fnmatch(s_lower, pattern_lower):
            return True
        try:
            if re.search(pattern_lower, s_lower):
                return True
        except re.error:
            pass
    return False


def matches_profiles(service_profiles: List[str], requested_profiles: List[str]) -> bool:
    """Check if service has at least one profile matching requested profiles (case-insensitive)."""
    if not requested_profiles:
        return True
    req_set = {p.strip().lower() for p in requested_profiles if p.strip()}
    svc_set = {p.strip().lower() for p in service_profiles if p.strip()}
    return bool(req_set.intersection(svc_set))
