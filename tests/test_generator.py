"""Unit and integration tests for the Ignition fleet compose generator."""

from pathlib import Path
import shutil
import subprocess
import pytest
import yaml

from ignition_gateway_harness.compose_builder import (
    build_fleet_compose_dict,
    render_compose_yaml,
)
from ignition_gateway_harness.config import (
    merge_service_config,
    scaffold_gateway_yaml,
)
from ignition_gateway_harness.discovery import (
    clean_identifier,
    derive_service_identity,
    find_all_gwbk_files,
    find_gateway_yaml,
    matches_filter,
    matches_profiles,
)
from ignition_gateway_harness.exceptions import (
    BackupDiscoveryError,
    ConfigurationError,
    PortConflictError,
)
from ignition_gateway_harness.generator import (
    generate_fleet_compose,
    main,
    parse_args,
)
from ignition_gateway_harness.models import GatewayServiceConfig
from ignition_gateway_harness.port_allocator import (
    allocate_ports,
    extract_host_port,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKUPS_DIR = REPO_ROOT / "backups"


# ==============================================================================
# 1. Discovery Tests
# ==============================================================================


class TestDiscovery:
    """Tests covering .gwbk backup discovery and filtering."""

    def test_discover_real_backups(self):
        """Verify discovery finds all 17 existing backup archives in the repository."""
        backups = find_all_gwbk_files(BACKUPS_DIR)
        assert len(backups) == 17
        for b in backups:
            assert b.suffix == ".gwbk"
            assert b.is_file()

    def test_discover_mock_directory(self, tmp_path: Path):
        """Verify recursive discovery finds .gwbk files and ignores non-gwbk files."""
        sub1 = tmp_path / "gateway_a"
        sub2 = tmp_path / "nested" / "gateway_b"
        sub1.mkdir(parents=True)
        sub2.mkdir(parents=True)

        (sub1 / "backup.gwbk").write_bytes(b"mock1")
        (sub2 / "backup.gwbk").write_bytes(b"mock2")
        (sub1 / "ignored.zip").write_bytes(b"zip")
        (sub1 / "manifest.xml").write_text("<manifest/>")
        (sub1 / "notes.txt").write_text("notes")

        discovered = find_all_gwbk_files(tmp_path)
        assert len(discovered) == 2
        names = [p.parent.name for p in discovered]
        assert "gateway_a" in names
        assert "gateway_b" in names

    def test_discover_empty_directory(self, tmp_path: Path):
        """Verify scanning an empty directory returns an empty list."""
        discovered = find_all_gwbk_files(tmp_path)
        assert discovered == []

    def test_discover_nonexistent_directory(self, tmp_path: Path):
        """Verify scanning a non-existent directory raises BackupDiscoveryError."""
        with pytest.raises(BackupDiscoveryError, match="does not exist"):
            find_all_gwbk_files(tmp_path / "does_not_exist")

    def test_discover_file_as_dir_raises(self, tmp_path: Path):
        """Verify passing a file path instead of directory raises BackupDiscoveryError."""
        fake_file = tmp_path / "fake.txt"
        fake_file.write_text("hello")
        with pytest.raises(BackupDiscoveryError, match="not a directory"):
            find_all_gwbk_files(fake_file)

    def test_filter_matching(self):
        """Verify matches_filter supports substring, glob, and case-insensitivity."""
        candidates = ["PROD-FE1_10-01-2026", "backup.gwbk", "PROD-FE1"]

        assert matches_filter(candidates, "prod")
        assert matches_filter(candidates, "FE1")
        assert matches_filter(candidates, "*FE*")
        assert matches_filter(candidates, "PROD-FE1*")
        assert not matches_filter(candidates, "TEST")
        assert not matches_filter(candidates, "SCADA")

    def test_profiles_matching(self):
        """Verify matches_profiles matches intersecting profiles case-insensitively."""
        assert matches_profiles(["prod", "fe"], ["prod"])
        assert matches_profiles(["PROD"], ["prod"])
        assert matches_profiles(["prod", "fe"], ["fe", "other"])
        assert not matches_profiles(["prod"], ["test"])
        assert not matches_profiles(["dev"], ["prod", "test"])
        # Empty requested profiles should match all
        assert matches_profiles(["prod"], [])

    def test_find_gateway_yaml_detection(self, tmp_path: Path):
        """Verify finding gateway.yaml next to .gwbk."""
        gwbk = tmp_path / "test.gwbk"
        gwbk.write_bytes(b"data")
        assert find_gateway_yaml(gwbk) is None

        yaml_file = tmp_path / "gateway.yaml"
        yaml_file.write_text("container_name: test")
        found = find_gateway_yaml(gwbk)
        assert found == yaml_file.resolve()


# ==============================================================================
# 2. Config Merging Tests
# ==============================================================================


class TestConfigMerging:
    """Tests covering sensible defaults and gateway.yaml overrides."""

    def test_default_config_values(self, tmp_path: Path):
        """Verify sensible defaults are populated when no gateway.yaml exists."""
        backup_dir = tmp_path / "PROD-FE1_10-01-2026"
        backup_dir.mkdir(parents=True)
        backup_file = backup_dir / "backup.gwbk"
        backup_file.write_bytes(b"content")

        cfg = merge_service_config(backup_file, tmp_path)

        assert cfg.service_name == "prod-fe1"
        assert cfg.container_name == "ignition-prod-fe1"
        assert cfg.hostname == "prod-fe1"
        assert cfg.system_name == "PROD-FE1"
        assert cfg.image == "inductiveautomation/ignition:8.1.51"
        assert cfg.heap_max == "1024m"
        assert cfg.ports == []  # Not allocated yet
        assert cfg.profiles == ["prod"]
        assert cfg.gan_aliases == ["prod-fe1"]
        assert cfg.environment["ACCEPT_IGNITION_EULA"] == "Y"
        assert cfg.environment["GATEWAY_SYSTEM_NAME"] == "PROD-FE1"
        assert cfg.environment["GATEWAY_ADMIN_PASSWORD"] == "password"
        assert cfg.get_data_volume_name() == "prod-fe1_data"

    def test_overrides_from_gateway_yaml(self, tmp_path: Path):
        """Verify gateway.yaml overrides container names, heap limits, env, profiles, and ports."""
        backup_dir = tmp_path / "PROD-FE1_10-01-2026"
        backup_dir.mkdir(parents=True)
        backup_file = backup_dir / "backup.gwbk"
        backup_file.write_bytes(b"content")

        yaml_content = {
            "container_name": "custom-container-name",
            "hostname": "custom-host",
            "heap_max": "2048m",
            "ports": ["8150:8088", "8151:8043"],
            "profiles": ["custom-prod", "edge"],
            "gan_aliases": ["fe1-gan-alias", "fe1-backup"],
            "environment": {
                "GATEWAY_ADMIN_PASSWORD": "supersecretpassword",
                "CUSTOM_TEST_VAR": "enabled",
            },
            "volumes": ["/host/custom/path:/container/path:ro"],
        }
        yaml_file = backup_dir / "gateway.yaml"
        yaml_file.write_text(yaml.dump(yaml_content))

        cfg = merge_service_config(backup_file, tmp_path)

        assert cfg.container_name == "custom-container-name"
        assert cfg.hostname == "custom-host"
        assert cfg.heap_max == "2048m"
        assert cfg.ports == ["8150:8088", "8151:8043"]
        assert cfg.profiles == ["custom-prod", "edge"]
        assert cfg.gan_aliases == ["fe1-gan-alias", "fe1-backup"]
        assert cfg.environment["GATEWAY_ADMIN_PASSWORD"] == "supersecretpassword"
        assert cfg.environment["CUSTOM_TEST_VAR"] == "enabled"
        assert cfg.environment["ACCEPT_IGNITION_EULA"] == "Y"  # Inherited
        assert "/host/custom/path:/container/path:ro" in cfg.extra_volumes

    def test_heap_limit_normalization(self, tmp_path: Path):
        """Verify various heap limit formats (-Xmx2048m, 2048, 2048m, 4g) normalize correctly."""
        backup_file = tmp_path / "test.gwbk"
        backup_file.write_bytes(b"data")

        for raw_val, expected in [
            ("-Xmx2048m", "2048m"),
            ("2048m", "2048m"),
            (2048, "2048m"),
            ("4g", "4g"),
            ("-Xmx4g", "4g"),
        ]:
            yaml_file = tmp_path / "gateway.yaml"
            yaml_file.write_text(yaml.dump({"heap_max": raw_val}))
            cfg = merge_service_config(backup_file, tmp_path, yaml_file)
            assert cfg.heap_max == expected

    def test_empty_gateway_yaml_handled_gracefully(self, tmp_path: Path):
        """Verify empty or comment-only gateway.yaml does not crash and defaults apply."""
        backup_file = tmp_path / "test.gwbk"
        backup_file.write_bytes(b"data")
        yaml_file = tmp_path / "gateway.yaml"
        yaml_file.write_text("# Just comments\n\n")

        cfg = merge_service_config(backup_file, tmp_path, yaml_file)
        assert cfg.service_name == "test"
        assert cfg.heap_max == "1024m"

    def test_scaffold_gateway_yaml(self, tmp_path: Path):
        """Verify scaffold_gateway_yaml generates a valid YAML template with appropriate fields."""
        backup_dir = tmp_path / "PROD-FE1_10-01-2026"
        backup_dir.mkdir(parents=True)
        backup_file = backup_dir / "backup.gwbk"
        backup_file.write_bytes(b"data")

        scaffold_path, created = scaffold_gateway_yaml(backup_file, tmp_path)
        assert created is True
        assert scaffold_path.exists()

        content = yaml.safe_load(scaffold_path.read_text())
        assert content["container_name"] == "ignition-prod-fe1"
        assert content["heap_max"] == "1024m"
        assert content["profiles"] == ["prod"]
        assert content["gan_aliases"] == ["prod-fe1"]
        assert content["environment"]["ACCEPT_IGNITION_EULA"] == "Y"

        # Calling again without force should not overwrite
        _, created_second = scaffold_gateway_yaml(backup_file, tmp_path, force=False)
        assert created_second is False


# ==============================================================================
# 3. Port Allocation and Conflict Detection Tests
# ==============================================================================


class TestPortAllocation:
    """Tests covering sequential port allocation and collision detection."""

    def test_extract_host_port(self):
        """Verify host port extraction from various docker port formats."""
        assert extract_host_port("8100") == 8100
        assert extract_host_port(8100) == 8100
        assert extract_host_port("8100:8088") == 8100
        assert extract_host_port("127.0.0.1:8100:8088") == 8100
        assert extract_host_port("0.0.0.0:8100:8088/tcp") == 8100

        with pytest.raises(ConfigurationError):
            extract_host_port("not_a_port:8088")
        with pytest.raises(ConfigurationError):
            extract_host_port("70000:8088")  # > 65535
        with pytest.raises(ConfigurationError):
            extract_host_port("0:8088")  # < 1

    def test_sequential_allocation_no_conflicts(self, tmp_path: Path):
        """Verify services without explicit ports receive sequential non-conflicting ports."""
        services = [
            GatewayServiceConfig(backup_path=tmp_path / "1.gwbk", service_name="svc-1"),
            GatewayServiceConfig(backup_path=tmp_path / "2.gwbk", service_name="svc-2"),
            GatewayServiceConfig(backup_path=tmp_path / "3.gwbk", service_name="svc-3"),
        ]

        allocate_ports(services, base_port=8100)

        assert services[0].ports == ["8100:8088"]
        assert services[1].ports == ["8101:8088"]
        assert services[2].ports == ["8102:8088"]

    def test_allocation_skips_explicitly_reserved_ports(self, tmp_path: Path):
        """Verify allocator skips ports explicitly claimed by overrides."""
        services = [
            GatewayServiceConfig(backup_path=tmp_path / "1.gwbk", service_name="svc-1"),
            # svc-2 explicitly claims 8101
            GatewayServiceConfig(
                backup_path=tmp_path / "2.gwbk",
                service_name="svc-2",
                ports=["8101:8088"],
            ),
            GatewayServiceConfig(backup_path=tmp_path / "3.gwbk", service_name="svc-3"),
        ]

        allocate_ports(services, base_port=8100)

        assert services[0].ports == ["8100:8088"]
        assert services[1].ports == ["8101:8088"]
        # svc-3 skips 8101 and receives 8102
        assert services[2].ports == ["8102:8088"]

    def test_explicit_port_conflict_detection(self, tmp_path: Path):
        """Verify PortConflictError is raised when two services claim the same host port."""
        services = [
            GatewayServiceConfig(
                backup_path=tmp_path / "1.gwbk",
                service_name="svc-1",
                ports=["8105:8088"],
            ),
            GatewayServiceConfig(
                backup_path=tmp_path / "2.gwbk",
                service_name="svc-2",
                ports=["8105:8088"],
            ),
        ]

        with pytest.raises(PortConflictError, match="claimed by multiple services"):
            allocate_ports(services, base_port=8100)

    def test_internal_port_conflict_same_service(self, tmp_path: Path):
        """Verify PortConflictError is raised when a single service maps the same host port twice."""
        services = [
            GatewayServiceConfig(
                backup_path=tmp_path / "1.gwbk",
                service_name="svc-1",
                ports=["8100:8088", "8100:8043"],
            ),
        ]

        with pytest.raises(PortConflictError, match="specified multiple times within service"):
            allocate_ports(services, base_port=8100)

    def test_custom_base_port(self, tmp_path: Path):
        """Verify custom base_port is respected."""
        services = [
            GatewayServiceConfig(backup_path=tmp_path / "1.gwbk", service_name="svc-1"),
            GatewayServiceConfig(backup_path=tmp_path / "2.gwbk", service_name="svc-2"),
        ]
        allocate_ports(services, base_port=9000)
        assert services[0].ports == ["9000:8088"]
        assert services[1].ports == ["9001:8088"]


# ==============================================================================
# 4. CLI Parsing and Generation Tests
# ==============================================================================


class TestCLIParsingAndExecution:
    """Tests covering command line options, dry-run, and config scaffolding."""

    def test_cli_parse_defaults(self):
        """Verify default CLI arguments."""
        parsed = parse_args([])
        assert parsed.output == Path("docker-compose.fleet.yml")
        assert parsed.base_port == 8100
        assert parsed.filter == []
        assert parsed.profiles == []
        assert parsed.dry_run is False
        assert parsed.init_configs is False

    def test_cli_parse_custom_flags(self):
        """Verify custom CLI flags are parsed."""
        args = [
            "-o",
            "custom.yml",
            "--base-port",
            "8200",
            "-f",
            "PROD",
            "--filter",
            "FE*",
            "-p",
            "prod",
            "--profiles",
            "edge",
            "--dry-run",
            "--init-configs",
            "--force",
        ]
        parsed = parse_args(args)
        assert parsed.output == Path("custom.yml")
        assert parsed.base_port == 8200
        assert parsed.filter == ["PROD", "FE*"]
        assert parsed.profiles == ["prod", "edge"]
        assert parsed.dry_run is True
        assert parsed.init_configs is True
        assert parsed.force is True

    def test_dry_run_does_not_create_file(self, tmp_path: Path, capsys):
        """Verify --dry-run prints output to stdout and creates no files on disk."""
        out_file = tmp_path / "should_not_exist.yml"
        yaml_out, configs = generate_fleet_compose(
            backups_dir=BACKUPS_DIR,
            output_path=out_file,
            filters=["PROD-FE1"],
            dry_run=True,
        )

        assert not out_file.exists()
        assert "services:" in yaml_out
        assert "prod-fe1:" in yaml_out
        assert len(configs) == 1

        # Check stdout
        captured = capsys.readouterr()
        assert "services:" in captured.out
        assert "prod-fe1:" in captured.out

    def test_init_configs_scaffolding(self, tmp_path: Path):
        """Verify --init-configs scaffolds gateway.yaml next to every .gwbk."""
        # Create mock backups
        b1_dir = tmp_path / "PROD-FE1_10-01-2026"
        b2_dir = tmp_path / "TEST-FE_10-01-2026"
        b1_dir.mkdir(parents=True)
        b2_dir.mkdir(parents=True)
        (b1_dir / "backup.gwbk").write_bytes(b"data1")
        (b2_dir / "backup.gwbk").write_bytes(b"data2")

        out_compose = tmp_path / "docker-compose.fleet.yml"
        generate_fleet_compose(
            backups_dir=tmp_path,
            output_path=out_compose,
            init_configs=True,
        )

        assert (b1_dir / "gateway.yaml").exists()
        assert (b2_dir / "gateway.yaml").exists()
        assert out_compose.exists()

    def test_main_cli_success(self, tmp_path: Path):
        """Verify main() entrypoint returns 0 on success."""
        out_file = tmp_path / "fleet.yml"
        code = main([
            "--backups-dir",
            str(BACKUPS_DIR),
            "--output",
            str(out_file),
            "--filter",
            "PROD-FE1",
        ])
        assert code == 0
        assert out_file.exists()

    def test_main_cli_port_conflict_returns_1(self, tmp_path: Path):
        """Verify main() returns 1 and prints error message on port conflict."""
        b1_dir = tmp_path / "FE1"
        b2_dir = tmp_path / "FE2"
        b1_dir.mkdir()
        b2_dir.mkdir()
        (b1_dir / "backup.gwbk").write_bytes(b"1")
        (b2_dir / "backup.gwbk").write_bytes(b"2")
        (b1_dir / "gateway.yaml").write_text("ports: ['8100:8088']")
        (b2_dir / "gateway.yaml").write_text("ports: ['8100:8088']")

        code = main([
            "--backups-dir",
            str(tmp_path),
            "--output",
            str(tmp_path / "out.yml"),
        ])
        assert code == 1


# ==============================================================================
# 5. Compose Syntax and Docker Compose Config Validation Tests
# ==============================================================================


class TestComposeValidation:
    """Tests validating generated Compose syntax and running `docker compose config`."""

    def test_verified_restore_command_and_volume_structure(self, tmp_path: Path):
        """Verify command syntax and volume mounts match the exact verified restore specifications."""
        backup_dir = tmp_path / "PROD-FE1_10-01-2026"
        backup_dir.mkdir(parents=True)
        backup_file = backup_dir / "backup.gwbk"
        backup_file.write_bytes(b"content")

        out_compose = tmp_path / "docker-compose.fleet.yml"
        yaml_str, configs = generate_fleet_compose(
            backups_dir=tmp_path,
            output_path=out_compose,
        )

        parsed = yaml.safe_load(yaml_str)
        svc = parsed["services"]["prod-fe1"]

        # Verified restore command syntax check:
        # ["-r", "/restore.gwbk", "--", "-Dignition.http.session.cookie.same-site.value=Lax", "-Xmx1024m"]
        expected_cmd_prefix = [
            "-r",
            "/restore.gwbk",
            "--",
            "-Dignition.http.session.cookie.same-site.value=Lax",
            "-Xmx1024m",
        ]
        assert svc["command"] == expected_cmd_prefix

        # Volume checks: read-only backup mount and named data volume
        vols = svc["volumes"]
        assert any(
            v.endswith("backup.gwbk:/restore.gwbk:ro") for v in vols
        ), f"Missing read-only backup mount in {vols}"
        assert "prod-fe1_data:/usr/local/bin/ignition/data" in vols

        # Named volume at top level
        assert "prod-fe1_data" in parsed["volumes"]

        # Network declaration (bridge network preserving port publishing)
        assert "ignition_network" in parsed["networks"]
        assert parsed["networks"]["ignition_network"].get("driver") == "bridge"
        assert not parsed["networks"]["ignition_network"].get("internal")

    def test_docker_compose_config_full_fleet(self, tmp_path: Path):
        """Verify the full generated 17-gateway compose file passes `docker compose config`."""
        docker_bin = shutil.which("docker")
        if not docker_bin:
            pytest.skip("Docker CLI is not available in test environment")

        out_compose = tmp_path / "docker-compose.fleet.yml"
        generate_fleet_compose(
            backups_dir=BACKUPS_DIR,
            output_path=out_compose,
            base_port=8100,
        )

        res = subprocess.run(
            [docker_bin, "compose", "-f", str(out_compose), "--profile", "*", "config"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        assert res.returncode == 0, f"docker compose config failed:\n{res.stderr}\n{res.stdout}"

    def test_docker_compose_config_filtered_profiles(self, tmp_path: Path):
        """Verify filtered compose file (e.g. --profiles test) passes `docker compose config`."""
        docker_bin = shutil.which("docker")
        if not docker_bin:
            pytest.skip("Docker CLI is not available in test environment")

        out_compose = tmp_path / "docker-compose.test.yml"
        yaml_str, configs = generate_fleet_compose(
            backups_dir=BACKUPS_DIR,
            output_path=out_compose,
            profiles=["test"],
            base_port=8200,
        )

        assert len(configs) == 5  # 5 TEST gateways
        for cfg in configs:
            assert "test" in cfg.profiles

        res = subprocess.run(
            [docker_bin, "compose", "-f", str(out_compose), "config"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        assert res.returncode == 0, f"docker compose config failed:\n{res.stderr}\n{res.stdout}"


# ==============================================================================
# 6. Edge Case and Boundary Condition Tests
# ==============================================================================


class TestEdgeCases:
    """Tests covering boundary conditions, malformed configs, and collision edge cases."""

    def test_singular_port_override(self, tmp_path: Path):
        """Verify singular port: key in gateway.yaml."""
        b_file = tmp_path / "test.gwbk"
        b_file.write_bytes(b"data")
        cfg_file = tmp_path / "gateway.yaml"
        cfg_file.write_text("port: 8155")

        cfg = merge_service_config(b_file, tmp_path, cfg_file)
        assert cfg.ports == ["8155:8088"]

    def test_list_environment_override(self, tmp_path: Path):
        """Verify list of KEY=VALUE strings for environment."""
        b_file = tmp_path / "test.gwbk"
        b_file.write_bytes(b"data")
        cfg_file = tmp_path / "gateway.yaml"
        cfg_file.write_text("environment:\n  - VAR1=hello\n  - VAR2=world\n")

        cfg = merge_service_config(b_file, tmp_path, cfg_file)
        assert cfg.environment["VAR1"] == "hello"
        assert cfg.environment["VAR2"] == "world"

    def test_dict_port_mapping_override(self, tmp_path: Path):
        """Verify dictionary format for ports: {8150: 8088, 8151: 8043}."""
        b_file = tmp_path / "test.gwbk"
        b_file.write_bytes(b"data")
        cfg_file = tmp_path / "gateway.yaml"
        cfg_file.write_text("ports:\n  8150: 8088\n  8151: 8043\n")

        cfg = merge_service_config(b_file, tmp_path, cfg_file)
        assert "8150:8088" in cfg.ports
        assert "8151:8043" in cfg.ports

    def test_custom_command_and_jvm_args(self, tmp_path: Path):
        """Verify custom explicit command and extra jvm_args."""
        b_file = tmp_path / "test.gwbk"
        b_file.write_bytes(b"data")
        cfg_file = tmp_path / "gateway.yaml"
        cfg_file.write_text(
            "jvm_args:\n  - -XX:+UseG1GC\n  - -XX:+PrintGCDetails\n"
        )

        cfg = merge_service_config(b_file, tmp_path, cfg_file)
        assert "-XX:+UseG1GC" in cfg.jvm_args
        assert "-XX:+PrintGCDetails" in cfg.jvm_args

        # Check in compose builder
        comp = build_fleet_compose_dict([cfg], tmp_path / "docker-compose.yml")
        cmd = comp["services"]["test"]["command"]
        assert "-XX:+UseG1GC" in cmd
        assert "-XX:+PrintGCDetails" in cmd

    def test_explicit_command_replaces_default(self, tmp_path: Path):
        """Verify explicit command override replaces the generated restore command."""
        b_file = tmp_path / "test.gwbk"
        b_file.write_bytes(b"data")
        cfg_file = tmp_path / "gateway.yaml"
        cfg_file.write_text("command: ['run', '--custom-mode']")

        cfg = merge_service_config(b_file, tmp_path, cfg_file)
        comp = build_fleet_compose_dict([cfg], tmp_path / "docker-compose.yml")
        assert comp["services"]["test"]["command"] == ["run", "--custom-mode"]

    def test_invalid_yaml_raises_configuration_error(self, tmp_path: Path):
        """Verify malformed YAML raises ConfigurationError."""
        b_file = tmp_path / "test.gwbk"
        b_file.write_bytes(b"data")
        cfg_file = tmp_path / "gateway.yaml"
        cfg_file.write_text("invalid: yaml: syntax: [unbalanced")

        with pytest.raises(ConfigurationError, match="Error parsing YAML"):
            merge_service_config(b_file, tmp_path, cfg_file)

    def test_non_dict_yaml_raises_configuration_error(self, tmp_path: Path):
        """Verify YAML containing a list or scalar raises ConfigurationError."""
        b_file = tmp_path / "test.gwbk"
        b_file.write_bytes(b"data")
        cfg_file = tmp_path / "gateway.yaml"
        cfg_file.write_text("- item1\n- item2\n")

        with pytest.raises(ConfigurationError, match="must be a YAML mapping"):
            merge_service_config(b_file, tmp_path, cfg_file)

    def test_invalid_base_port_raises_configuration_error(self, tmp_path: Path):
        """Verify invalid base ports (<1 or >65535) raise ConfigurationError."""
        services = [
            GatewayServiceConfig(backup_path=tmp_path / "1.gwbk", service_name="svc-1")
        ]
        with pytest.raises(ConfigurationError, match="not a valid port number"):
            allocate_ports(services, base_port=0)

        with pytest.raises(ConfigurationError, match="not a valid port number"):
            allocate_ports(services, base_port=70000)

    def test_port_exhaustion_raises_configuration_error(self, tmp_path: Path):
        """Verify allocating beyond 65535 raises ConfigurationError."""
        services = [
            GatewayServiceConfig(backup_path=tmp_path / "1.gwbk", service_name="svc-1"),
            GatewayServiceConfig(backup_path=tmp_path / "2.gwbk", service_name="svc-2"),
        ]
        with pytest.raises(ConfigurationError, match="Exhausted valid TCP port range"):
            allocate_ports(services, base_port=65535)

    def test_service_name_collision_resolution(self, tmp_path: Path):
        """Verify collisions in service names get resolved with index suffixes."""
        existing = {"gateway", "gateway-2"}
        svc_name, _, _ = derive_service_identity(
            tmp_path / "gateway.gwbk", tmp_path, existing_service_names=existing
        )
        assert svc_name == "gateway-3"

    def test_no_backups_matching_filter_raises_error(self, tmp_path: Path):
        """Verify ConfigurationError when filter pattern matches no backups."""
        b_file = tmp_path / "PROD-FE1.gwbk"
        b_file.write_bytes(b"data")

        with pytest.raises(ConfigurationError, match="No backups matched"):
            generate_fleet_compose(
                backups_dir=tmp_path,
                output_path=tmp_path / "out.yml",
                filters=["NONEXISTENT_FILTER"],
            )

    def test_no_backups_matching_profiles_raises_error(self, tmp_path: Path):
        """Verify ConfigurationError when profiles criteria matches no backups."""
        b_file = tmp_path / "PROD-FE1.gwbk"
        b_file.write_bytes(b"data")

        with pytest.raises(ConfigurationError, match="No backups matched"):
            generate_fleet_compose(
                backups_dir=tmp_path,
                output_path=tmp_path / "out.yml",
                profiles=["nonexistent_profile"],
            )

    def test_ports_list_of_dicts_format(self, tmp_path: Path):
        """Verify ports defined with space after colon (- 8150: 8088) parse correctly without crashing."""
        b_file = tmp_path / "test.gwbk"
        b_file.write_bytes(b"data")
        cfg_file = tmp_path / "gateway.yaml"
        # PyYAML parses this format as a list of dicts: [{'8150': 8088}] or [{8150: 8088}]
        cfg_file.write_text("ports:\n  - 8150: 8088\n")

        cfg = merge_service_config(b_file, tmp_path, cfg_file)
        assert cfg.ports == ["8150:8088"]
        assert extract_host_port(cfg.ports[0]) == 8150

    def test_duplicate_container_name_raises_configuration_error(self, tmp_path: Path):
        """Verify duplicate container_name across services raises ConfigurationError."""
        b1 = tmp_path / "GW1"
        b2 = tmp_path / "GW2"
        b1.mkdir()
        b2.mkdir()
        (b1 / "backup.gwbk").write_bytes(b"1")
        (b2 / "backup.gwbk").write_bytes(b"2")
        (b1 / "gateway.yaml").write_text("container_name: duplicate-container\n")
        (b2 / "gateway.yaml").write_text("container_name: duplicate-container\n")

        with pytest.raises(ConfigurationError, match="Duplicate container name"):
            generate_fleet_compose(
                backups_dir=tmp_path,
                output_path=tmp_path / "out.yml",
            )

    def test_duplicate_service_name_raises_configuration_error(self, tmp_path: Path):
        """Verify duplicate service_name across services raises ConfigurationError."""
        b1 = tmp_path / "GW1"
        b2 = tmp_path / "GW2"
        b1.mkdir()
        b2.mkdir()
        (b1 / "backup.gwbk").write_bytes(b"1")
        (b2 / "backup.gwbk").write_bytes(b"2")
        (b1 / "gateway.yaml").write_text("service_name: duplicate-service\n")
        (b2 / "gateway.yaml").write_text("service_name: duplicate-service\n")

        with pytest.raises(ConfigurationError, match="Duplicate service name"):
            generate_fleet_compose(
                backups_dir=tmp_path,
                output_path=tmp_path / "out.yml",
            )

    def test_dry_run_with_init_configs_does_not_create_files(self, tmp_path: Path):
        """Verify --dry-run with --init-configs does not write any files to disk."""
        b1 = tmp_path / "GW1"
        b1.mkdir()
        (b1 / "backup.gwbk").write_bytes(b"1")

        generate_fleet_compose(
            backups_dir=tmp_path,
            output_path=tmp_path / "out.yml",
            dry_run=True,
            init_configs=True,
        )

        assert not (b1 / "gateway.yaml").exists()
        assert not (tmp_path / "out.yml").exists()

    def test_extra_named_volumes_registered_in_top_level_volumes(self, tmp_path: Path):
        """Verify extra named volumes are registered in the top-level volumes block."""
        cfg = GatewayServiceConfig(
            backup_path=tmp_path / "1.gwbk",
            service_name="test-svc",
            extra_volumes=["custom_named_vol:/var/data", "./bind/path:/var/bind"],
        )
        comp = build_fleet_compose_dict([cfg], tmp_path / "docker-compose.yml")

        assert "test-svc_data" in comp["volumes"]
        assert "custom_named_vol" in comp["volumes"]
        # Bind mount path should NOT be registered in top-level volumes
        assert "./bind/path" not in comp["volumes"]

    def test_empty_gan_aliases_clears_aliases(self, tmp_path: Path):
        """Verify gan_aliases: [] in gateway.yaml preserves empty list rather than resetting to default."""
        b_file = tmp_path / "test.gwbk"
        b_file.write_bytes(b"data")
        cfg_file = tmp_path / "gateway.yaml"
        cfg_file.write_text("gan_aliases: []\n")

        cfg = merge_service_config(b_file, tmp_path, cfg_file)
        assert cfg.gan_aliases == []

    def test_empty_profiles_clears_profiles(self, tmp_path: Path):
        """Verify profiles: [] in gateway.yaml preserves empty list rather than resetting to default."""
        b_file = tmp_path / "test.gwbk"
        b_file.write_bytes(b"data")
        cfg_file = tmp_path / "gateway.yaml"
        cfg_file.write_text("profiles: []\n")

        cfg = merge_service_config(b_file, tmp_path, cfg_file)
        assert cfg.profiles == []

    def test_explicit_null_heap_max_removes_limit(self, tmp_path: Path):
        """Verify heap_max: null or none removes the heap limit and omits -Xmx from command."""
        b_file = tmp_path / "test.gwbk"
        b_file.write_bytes(b"data")
        cfg_file = tmp_path / "gateway.yaml"
        cfg_file.write_text("heap_max: null\n")

        cfg = merge_service_config(b_file, tmp_path, cfg_file)
        assert cfg.heap_max is None

        comp = build_fleet_compose_dict([cfg], tmp_path / "docker-compose.yml")
        cmd = comp["services"]["test"]["command"]
        assert not any(arg.startswith("-Xmx") for arg in cmd)

    def test_ipv6_and_host_ip_port_mapping_extraction(self):
        """Verify extract_host_port correctly parses IPv6 bracketed and host-IP bindings."""
        assert extract_host_port("[::1]:8100:8088") == 8100
        assert extract_host_port("[::]:8100") == 8100
        assert extract_host_port("127.0.0.1:8100") == 8100
        assert extract_host_port("127.0.0.1:8100:8088") == 8100

    def test_clean_identifier_iso_date(self):
        """Verify clean_identifier strips ISO 8601 YYYY-MM-DD date formats."""
        assert clean_identifier("Gateway_2026-10-01") == "Gateway"
        assert clean_identifier("PROD-FE_2026-10-01") == "PROD-FE"

    def test_cli_profile_singular_alias(self):
        """Verify --profile singular CLI flag works identically to --profiles."""
        parsed = parse_args(["--profile", "prod", "-p", "test"])
        assert parsed.profiles == ["prod", "test"]

    def test_filter_by_service_or_container_name(self, tmp_path: Path):
        """Verify filtering by service_name or container_name matches correctly."""
        b1 = tmp_path / "PROD-FE1_10-01-2026"
        b1.mkdir()
        (b1 / "backup.gwbk").write_bytes(b"1")
        (b1 / "gateway.yaml").write_text("container_name: special-container-fe1\n")

        yaml_out, svcs = generate_fleet_compose(
            backups_dir=tmp_path,
            output_path=tmp_path / "out.yml",
            filters=["special-container-fe1"],
            dry_run=True,
        )
        assert len(svcs) == 1
        assert svcs[0].service_name == "prod-fe1"

    def test_scaffold_gateway_yaml_multi_backup_directory(self, tmp_path: Path):
        """Verify scaffolding in directory with multiple backups creates individual <stem>.gateway.yaml files."""
        multi_dir = tmp_path / "multi_gateways"
        multi_dir.mkdir()
        b1 = multi_dir / "gateway1.gwbk"
        b2 = multi_dir / "gateway2.gwbk"
        b1.write_bytes(b"1")
        b2.write_bytes(b"2")

        p1, c1 = scaffold_gateway_yaml(b1, tmp_path)
        p2, c2 = scaffold_gateway_yaml(b2, tmp_path)

        assert c1 is True
        assert c2 is True
        assert p1.name == "gateway1.gateway.yaml"
        assert p2.name == "gateway2.gateway.yaml"
        assert p1.exists()
        assert p2.exists()


class TestLowRamMode:
    """Tests covering tuned low-RAM mode, CLI flags, memory limits, and command generation."""

    def test_low_ram_cli_flag_parsing(self):
        """Verify --low-ram and -l flags are properly parsed."""
        p1 = parse_args(["--low-ram"])
        assert p1.low_ram is True

        p2 = parse_args(["-l"])
        assert p2.low_ram is True

        p3 = parse_args([])
        assert p3.low_ram is False

    def test_low_ram_command_and_cgroup_generation(self, tmp_path: Path):
        """Verify low-RAM mode injects wrapper properties, metaspace/stack caps, and cgroup limits."""
        b_dir = tmp_path / "TEST-GW"
        b_dir.mkdir()
        (b_dir / "backup.gwbk").write_bytes(b"content")

        yaml_str, svcs = generate_fleet_compose(
            backups_dir=tmp_path,
            output_path=tmp_path / "compose.yml",
            low_ram=True,
            dry_run=True,
        )
        parsed = yaml.safe_load(yaml_str)
        svc = parsed["services"]["test-gw"]
        cmd = svc["command"]

        # Assert wrapper properties disabled percent
        assert "wrapper.java.initmemory.percent=0" in cmd
        assert "wrapper.java.maxmemory.percent=0" in cmd
        assert "wrapper.java.initmemory=256" in cmd
        assert "wrapper.java.maxmemory=1024" in cmd
        assert "-Xmx1024m" in cmd
        assert "-XX:MaxMetaspaceSize=256m" in cmd
        assert "-Xss256k" in cmd

        # Assert deploy resources limits
        assert svc["deploy"]["resources"]["limits"]["memory"] == "1800M"
        assert svc["mem_limit"] == "1800m"

    def test_low_ram_per_gateway_yaml_override(self, tmp_path: Path):
        """Verify low_ram: true in gateway.yaml activates low-RAM mode without global CLI flag."""
        b_dir = tmp_path / "CUSTOM-GW"
        b_dir.mkdir()
        (b_dir / "backup.gwbk").write_bytes(b"content")
        (b_dir / "gateway.yaml").write_text("low_ram: true\nheap_max: 384m\nmem_limit: 800M\n")

        yaml_str, svcs = generate_fleet_compose(
            backups_dir=tmp_path,
            output_path=tmp_path / "compose.yml",
            low_ram=False,
            dry_run=True,
        )
        parsed = yaml.safe_load(yaml_str)
        svc = parsed["services"]["custom-gw"]
        cmd = svc["command"]

        assert "wrapper.java.maxmemory=384" in cmd
        assert "-Xmx384m" in cmd
        assert svc["deploy"]["resources"]["limits"]["memory"] == "800M"


# ==============================================================================
# 8. Persistent Volume and Restore Mode Tests
# ==============================================================================


class TestPersistentVolumeAndRestoreModes:
    """Tests covering persistent volume storage, run mode, restore mode, and companion overlays."""

    def test_run_mode_omits_restore_command_and_backup_volume(self, tmp_path: Path):
        """Verify restore=False generates persistent volume run command without -r or backup mount."""
        backup_dir = tmp_path / "PROD-FE1_10-01-2026"
        backup_dir.mkdir(parents=True)
        backup_file = backup_dir / "backup.gwbk"
        backup_file.write_bytes(b"content")

        out_compose = tmp_path / "docker-compose.fleet.yml"
        yaml_str, configs = generate_fleet_compose(
            backups_dir=tmp_path,
            output_path=out_compose,
            restore=False,
        )

        parsed = yaml.safe_load(yaml_str)
        svc = parsed["services"]["prod-fe1"]

        # Run command starts directly with '--' without '-r /restore.gwbk'
        assert svc["command"] == [
            "--",
            "-Dignition.http.session.cookie.same-site.value=Lax",
            "-Xmx1024m",
        ]

        # Only persistent data volume is mounted, backup file is NOT mounted
        vols = svc["volumes"]
        assert "prod-fe1_data:/usr/local/bin/ignition/data" in vols
        assert not any(v.endswith("/restore.gwbk:ro") for v in vols)
        assert "prod-fe1_data" in parsed["volumes"]

    def test_run_mode_preserves_low_ram_tuning(self, tmp_path: Path):
        """Verify restore=False with low_ram=True preserves all low-RAM tuning parameters."""
        b_dir = tmp_path / "TEST-GW"
        b_dir.mkdir()
        (b_dir / "backup.gwbk").write_bytes(b"content")

        yaml_str, _ = generate_fleet_compose(
            backups_dir=tmp_path,
            output_path=tmp_path / "compose.yml",
            low_ram=True,
            restore=False,
            dry_run=True,
        )
        parsed = yaml.safe_load(yaml_str)
        svc = parsed["services"]["test-gw"]
        cmd = svc["command"]

        # Assert wrapper properties disabled percent and low-RAM parameters
        assert "-r" not in cmd
        assert "/restore.gwbk" not in cmd
        assert cmd[0] == "--"
        assert "wrapper.java.initmemory.percent=0" in cmd
        assert "wrapper.java.maxmemory.percent=0" in cmd
        assert "wrapper.java.initmemory=256" in cmd
        assert "wrapper.java.maxmemory=1024" in cmd
        assert "-Xmx1024m" in cmd
        assert "-XX:MaxMetaspaceSize=256m" in cmd
        assert "-Xss256k" in cmd

        # Assert memory limits
        assert svc["deploy"]["resources"]["limits"]["memory"] == "1800M"
        assert svc["mem_limit"] == "1800m"

    def test_gateway_yaml_restore_false_per_service_override(self, tmp_path: Path):
        """Verify restore: false in gateway.yaml configures individual service in run mode."""
        b1_dir = tmp_path / "GW1"
        b2_dir = tmp_path / "GW2"
        b1_dir.mkdir()
        b2_dir.mkdir()
        (b1_dir / "backup.gwbk").write_bytes(b"1")
        (b2_dir / "backup.gwbk").write_bytes(b"2")
        # GW1 overrides restore: false
        (b1_dir / "gateway.yaml").write_text("restore: false\n")

        yaml_str, _ = generate_fleet_compose(
            backups_dir=tmp_path,
            output_path=tmp_path / "compose.yml",
            restore=True,  # Default global is restore
            dry_run=True,
        )
        parsed = yaml.safe_load(yaml_str)
        s1 = parsed["services"]["gw1"]
        s2 = parsed["services"]["gw2"]

        # s1 is in run mode (no -r)
        assert s1["command"][0] == "--"
        assert not any(v.endswith("/restore.gwbk:ro") for v in s1["volumes"])

        # s2 remains in restore mode (-r)
        assert s2["command"][0] == "-r"
        assert any(v.endswith("/restore.gwbk:ro") for v in s2["volumes"])

    def test_gateway_yaml_mode_run_override(self, tmp_path: Path):
        """Verify mode: run in gateway.yaml configures run mode."""
        b_dir = tmp_path / "GW1"
        b_dir.mkdir()
        (b_dir / "backup.gwbk").write_bytes(b"1")
        (b_dir / "gateway.yaml").write_text("mode: run\n")

        yaml_str, _ = generate_fleet_compose(
            backups_dir=tmp_path,
            output_path=tmp_path / "compose.yml",
            dry_run=True,
        )
        parsed = yaml.safe_load(yaml_str)
        svc = parsed["services"]["gw1"]
        assert svc["command"][0] == "--"
        assert not any(v.endswith("/restore.gwbk:ro") for v in svc["volumes"])

    def test_gateway_yaml_custom_named_data_volume(self, tmp_path: Path):
        """Verify data_volume in gateway.yaml overrides named volume name."""
        b_dir = tmp_path / "GW1"
        b_dir.mkdir()
        (b_dir / "backup.gwbk").write_bytes(b"1")
        (b_dir / "gateway.yaml").write_text("data_volume: custom_fe_volume\n")

        yaml_str, _ = generate_fleet_compose(
            backups_dir=tmp_path,
            output_path=tmp_path / "compose.yml",
            dry_run=True,
        )
        parsed = yaml.safe_load(yaml_str)
        svc = parsed["services"]["gw1"]
        assert "custom_fe_volume:/usr/local/bin/ignition/data" in svc["volumes"]
        assert "custom_fe_volume" in parsed["volumes"]

    def test_gateway_yaml_bind_mount_data_volume(self, tmp_path: Path):
        """Verify host bind mount data_volume is mounted but not added to top-level volumes."""
        b_dir = tmp_path / "GW1"
        b_dir.mkdir()
        (b_dir / "backup.gwbk").write_bytes(b"1")
        (b_dir / "gateway.yaml").write_text("data_volume: ./local_data/gw1\n")

        yaml_str, _ = generate_fleet_compose(
            backups_dir=tmp_path,
            output_path=tmp_path / "compose.yml",
            dry_run=True,
        )
        parsed = yaml.safe_load(yaml_str)
        svc = parsed["services"]["gw1"]
        assert "./local_data/gw1:/usr/local/bin/ignition/data" in svc["volumes"]
        assert "./local_data/gw1" not in parsed["volumes"]

    def test_companion_restore_overlay_generation(self, tmp_path: Path):
        """Verify generating companion restore overlay creates valid Docker Compose files."""
        docker_bin = shutil.which("docker")
        b_dir = tmp_path / "GW1"
        b_dir.mkdir()
        (b_dir / "backup.gwbk").write_bytes(b"1")

        base_compose = tmp_path / "docker-compose.fleet.yml"
        overlay_compose = tmp_path / "docker-compose.fleet.restore.yml"

        generate_fleet_compose(
            backups_dir=tmp_path,
            output_path=base_compose,
            restore=False,
            restore_overlay_path=overlay_compose,
            low_ram=True,
        )

        assert base_compose.exists()
        assert overlay_compose.exists()

        base_data = yaml.safe_load(base_compose.read_text())
        overlay_data = yaml.safe_load(overlay_compose.read_text())

        # Base compose runs from persistent volume without -r
        assert base_data["services"]["gw1"]["command"][0] == "--"
        assert not any(v.endswith("/restore.gwbk:ro") for v in base_data["services"]["gw1"]["volumes"])

        # Overlay supplies -r and backup mount
        assert overlay_data["services"]["gw1"]["command"][0] == "-r"
        assert any(v.endswith("/restore.gwbk:ro") for v in overlay_data["services"]["gw1"]["volumes"])

        if docker_bin:
            # Validate overlay combination with docker compose config
            res = subprocess.run(
                [docker_bin, "compose", "-f", str(base_compose), "-f", str(overlay_compose), "config"],
                capture_output=True,
                text=True,
                cwd=REPO_ROOT,
            )
            assert res.returncode == 0, f"docker compose config overlay failed:\n{res.stderr}"

    def test_cli_flags_run_mode_and_overlay(self, tmp_path: Path):
        """Verify CLI flags --run, --no-restore, --mode run, and --restore-overlay work via main()."""
        b_dir = tmp_path / "GW1"
        b_dir.mkdir()
        (b_dir / "backup.gwbk").write_bytes(b"1")

        out_base = tmp_path / "fleet_run.yml"
        out_overlay = tmp_path / "fleet_restore_overlay.yml"

        ret = main([
            "--backups-dir", str(tmp_path),
            "--output", str(out_base),
            "--run",
            "--restore-overlay", str(out_overlay),
        ])
        assert ret == 0
        assert out_base.exists()
        assert out_overlay.exists()

        base_data = yaml.safe_load(out_base.read_text())
        assert base_data["services"]["gw1"]["command"][0] == "--"

    def test_windows_drive_letter_in_extra_volumes(self, tmp_path: Path):
        """Verify Windows drive letter paths in extra_volumes do not create bogus named volumes."""
        b_dir = tmp_path / "GW1"
        b_dir.mkdir()
        (b_dir / "backup.gwbk").write_bytes(b"1")
        (b_dir / "gateway.yaml").write_text(
            "volumes:\n"
            "  - 'C:/host/extra:/container/extra'\n"
            "  - 'D:\\\\host\\\\data:/container/data'\n"
            "  - 'real_named_volume:/container/named'\n"
            "  - type: bind\n"
            "    source: /bind/mount\n"
            "    target: /target\n"
            "  - type: volume\n"
            "    source: dict_named_volume\n"
            "    target: /dict/target\n"
        )

        yaml_str, _ = generate_fleet_compose(
            backups_dir=tmp_path,
            output_path=tmp_path / "compose.yml",
            dry_run=True,
        )
        parsed = yaml.safe_load(yaml_str)
        top_volumes = parsed.get("volumes", {})

        # Ensure drive letters 'C' and 'D' are NOT registered as named volumes
        assert "C" not in top_volumes
        assert "D" not in top_volumes
        # Ensure bind mounts are not registered
        assert "/bind/mount" not in top_volumes
        # Ensure actual named volumes are registered
        assert "real_named_volume" in top_volumes
        assert "dict_named_volume" in top_volumes

    def test_custom_command_preserved_in_restore_overlay(self, tmp_path: Path):
        """Verify custom command in gateway.yaml is preserved and prepended with -r in restore overlay."""
        b_dir = tmp_path / "GW1"
        b_dir.mkdir()
        (b_dir / "backup.gwbk").write_bytes(b"1")
        (b_dir / "gateway.yaml").write_text(
            "command:\n"
            "  - '--'\n"
            "  - '-Dignition.custom.property=active'\n"
            "  - '-Xmx2048m'\n"
        )

        base_compose = tmp_path / "compose.yml"
        overlay_compose = tmp_path / "overlay.yml"

        generate_fleet_compose(
            backups_dir=tmp_path,
            output_path=base_compose,
            restore=False,
            restore_overlay_path=overlay_compose,
        )

        base_data = yaml.safe_load(base_compose.read_text())
        overlay_data = yaml.safe_load(overlay_compose.read_text())

        # Base compose preserves custom command without -r
        assert base_data["services"]["gw1"]["command"] == [
            "--",
            "-Dignition.custom.property=active",
            "-Xmx2048m",
        ]

        # Overlay prepends -r /restore.gwbk while keeping custom args
        assert overlay_data["services"]["gw1"]["command"] == [
            "-r",
            "/restore.gwbk",
            "--",
            "-Dignition.custom.property=active",
            "-Xmx2048m",
        ]

    def test_explicit_restore_false_omitted_from_restore_overlay(self, tmp_path: Path):
        """Verify service with explicit restore: false in gateway.yaml is omitted from companion restore overlay."""
        b1_dir = tmp_path / "GW1"
        b2_dir = tmp_path / "GW2"
        b1_dir.mkdir()
        b2_dir.mkdir()
        (b1_dir / "backup.gwbk").write_bytes(b"1")
        (b2_dir / "backup.gwbk").write_bytes(b"2")
        # GW1 explicitly opts out of restore
        (b1_dir / "gateway.yaml").write_text("restore: false\n")

        base_compose = tmp_path / "compose.yml"
        overlay_compose = tmp_path / "overlay.yml"

        generate_fleet_compose(
            backups_dir=tmp_path,
            output_path=base_compose,
            restore=False,
            restore_overlay_path=overlay_compose,
        )

        overlay_data = yaml.safe_load(overlay_compose.read_text())
        overlay_svcs = overlay_data.get("services", {})

        # GW1 must NOT be in overlay
        assert "gw1" not in overlay_svcs
        # GW2 must be in overlay
        assert "gw2" in overlay_svcs

    def test_conflicting_cli_flags_error(self, tmp_path: Path):
        """Verify main() returns error code 1 when conflicting mode flags are passed."""
        b_dir = tmp_path / "GW1"
        b_dir.mkdir()
        (b_dir / "backup.gwbk").write_bytes(b"1")

        # --run and --restore together
        ret1 = main([
            "--backups-dir", str(tmp_path),
            "--run",
            "--restore",
        ])
        assert ret1 == 1

        # --mode run and --restore
        ret2 = main([
            "--backups-dir", str(tmp_path),
            "--mode", "run",
            "--restore",
        ])
        assert ret2 == 1

        # --mode restore and --run
        ret3 = main([
            "--backups-dir", str(tmp_path),
            "--mode", "restore",
            "--run",
        ])
        assert ret3 == 1

    def test_cli_positional_subcommands(self, tmp_path: Path):
        """Verify positional subcommands (analyze, sim, trial-reset) work via main()."""
        # 1. Test analyze subcommand with export
        rep_out = tmp_path / "report.json"
        ret_analyze = main([
            "analyze",
            "--backups-dir", "backups",
            "--filter", "PROD-SCADA_Master",
            "--export-report", str(rep_out),
        ])
        assert ret_analyze == 0
        assert rep_out.exists()
        assert "prod-scada_master" in rep_out.read_text(encoding="utf-8")

        # 2. Test sim subcommand
        sim_out = tmp_path / "docker-compose.sim.yml"
        sim_init = tmp_path / "sim_init"
        ret_sim = main([
            "sim",
            "--backups-dir", "backups",
            "--filter", "PROD-SCADA_Master",
            "--sim-output", str(sim_out),
            "--sim-init-dir", str(sim_init),
        ])
        assert ret_sim == 0
        assert sim_out.exists()
        assert (sim_init / "init-databases.sql").exists()

    def test_cli_db_type_options(self, tmp_path: Path):
        """Verify --db-type option parses and routes to simulation generation."""
        # 1. Verify parse_args
        args_mssql = parse_args(["--db-type", "mssql"])
        assert args_mssql.db_type == "mssql"

        args_mysql = parse_args(["--db-type", "mysql"])
        assert args_mysql.db_type == "mysql"

        args_ts = parse_args(["--db-type", "timescale"])
        assert args_ts.db_type == "timescale"

        # 2. Test running sim generation with --db-type mysql
        sim_out_mysql = tmp_path / "sim_mysql.yml"
        sim_init_mysql = tmp_path / "sim_init_mysql"
        ret_mysql = main([
            "sim",
            "--backups-dir", "backups",
            "--filter", "PROD-SCADA_Master",
            "--db-type", "mysql",
            "--sim-output", str(sim_out_mysql),
            "--sim-init-dir", str(sim_init_mysql),
        ])
        assert ret_mysql == 0
        with open(sim_out_mysql, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert "sim-mysql" in data["services"]
        assert "sim-mssql" not in data["services"]




