from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile
import pytest

# Locate the root backups directory relative to the repository
REPO_ROOT = Path(__file__).resolve().parent.parent
BACKUPS_DIR = REPO_ROOT / "backups"


def find_all_backups():
    """Discover all .gwbk files in the backups directory."""
    if not BACKUPS_DIR.exists():
        return []
    return sorted([p for p in BACKUPS_DIR.rglob("*.gwbk") if p.is_file()], key=lambda p: str(p))


def get_backup_id(path: Path) -> str:
    """Generate a readable test ID from the backup path."""
    if path.parent == BACKUPS_DIR:
        return path.name
    return f"{path.parent.name}/{path.name}"


ALL_BACKUPS = find_all_backups()


@pytest.fixture(scope="session")
def backup_manifests():
    """Map of backup paths to their parsed manifest.xml (if present)."""
    manifests = {}
    for backup_path in ALL_BACKUPS:
        manifest_file = backup_path.parent / "manifest.xml"
        if manifest_file.exists():
            try:
                tree = ET.parse(manifest_file)
                manifests[backup_path] = tree.getroot()
            except Exception:
                manifests[backup_path] = None
    return manifests


@pytest.mark.parametrize("backup_path", ALL_BACKUPS, ids=[get_backup_id(p) for p in ALL_BACKUPS])
class TestGatewayBackups:
    """Automated integrity, structure, and metadata verification for all .gwbk archives."""

    def test_archive_integrity(self, backup_path: Path):
        """Verify that the backup is a valid, uncorrupted ZIP archive and non-empty."""
        assert backup_path.exists(), f"Backup file {backup_path} does not exist"
        size_bytes = backup_path.stat().st_size
        assert size_bytes > 1_000_000, f"Backup {backup_path.name} is unusually small ({size_bytes} bytes)"

        with zipfile.ZipFile(backup_path, "r") as zf:
            corrupted_file = zf.testzip()
            assert corrupted_file is None, f"Archive {backup_path.name} has corrupted entry: {corrupted_file}"

    def test_backup_info_metadata(self, backup_path: Path):
        """Verify that backupinfo.xml exists and contains valid version and timestamp metadata."""
        with zipfile.ZipFile(backup_path, "r") as zf:
            namelist = zf.namelist()
            assert "backupinfo.xml" in namelist, (
                f"{backup_path.name} is missing 'backupinfo.xml'! It may be a project export rather than a gateway backup."
            )

            xml_data = zf.read("backupinfo.xml").decode("utf-8", errors="replace")
            root = ET.fromstring(xml_data)

            version = root.findtext("version")
            assert version is not None and len(version.strip()) > 0, "Missing <version> tag in backupinfo.xml"
            assert re.match(r"^\d+\.\d+", version), f"Invalid version format: '{version}' in {backup_path.name}"

            timestamp = root.findtext("timestamp")
            assert timestamp is not None and len(timestamp.strip()) > 0, "Missing <timestamp> tag in backupinfo.xml"

    def test_internal_database_exists(self, backup_path: Path):
        """Verify that the internal configuration database (config.idb) is included in the backup."""
        with zipfile.ZipFile(backup_path, "r") as zf:
            namelist = zf.namelist()
            has_db = any(
                name in namelist
                for name in [
                    "db_backup_sqlite.idb",
                    "db_backup/config.idb",
                    "config.idb",
                    "data/db/config.idb",
                    "db/config.idb",
                ]
            )
            assert has_db, f"{backup_path.name} is missing an internal database (config.idb)!"

    def test_required_configuration_files(self, backup_path: Path):
        """Verify that gateway.xml and ignition.conf exist in the archive."""
        with zipfile.ZipFile(backup_path, "r") as zf:
            namelist = zf.namelist()
            assert "gateway.xml" in namelist, f"{backup_path.name} is missing gateway.xml"
            assert "ignition.conf" in namelist, f"{backup_path.name} is missing ignition.conf"

    def test_projects_present(self, backup_path: Path):
        """Verify that the backup contains project resources."""
        with zipfile.ZipFile(backup_path, "r") as zf:
            namelist = zf.namelist()
            projects = {
                parts[1]
                for name in namelist
                if name.startswith("projects/")
                for parts in [name.split("/")]
                if len(parts) > 1 and parts[1] and parts[1] != ".resources"
            }
            assert len(projects) > 0, f"{backup_path.name} contains no projects in projects/ folder"

    def test_manifest_consistency(self, backup_path: Path, backup_manifests):
        """If a manifest.xml exists alongside the backup, verify that declared platform metadata matches."""
        manifest = backup_manifests.get(backup_path)
        if manifest is None:
            pytest.skip("No manifest.xml present for this backup")

        platform_version = manifest.attrib.get("platform.version")
        assert platform_version is not None, "manifest.xml missing platform.version attribute"

        # Compare with backupinfo.xml version
        with zipfile.ZipFile(backup_path, "r") as zf:
            root = ET.fromstring(zf.read("backupinfo.xml").decode("utf-8", errors="replace"))
            backupinfo_version = root.findtext("version", "")
            assert platform_version.startswith(backupinfo_version[:3]), (
                f"Version mismatch: manifest has {platform_version}, backupinfo has {backupinfo_version}"
            )
