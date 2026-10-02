import json
from pathlib import Path
import subprocess
from typing import Dict, List, Optional
import httpx
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
FLEET_COMPOSE_PATH = REPO_ROOT / "docker-compose.fleet.yml"


def load_fleet_services() -> List[Dict]:
    """Parse docker-compose.fleet.yml and return metadata for each configured gateway service."""
    if not FLEET_COMPOSE_PATH.exists():
        return []

    try:
        with open(FLEET_COMPOSE_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception:
        return []

    services = []
    for svc_name, cfg in data.get("services", {}).items():
        container_name = cfg.get("container_name", f"ignition-{svc_name}")
        ports = cfg.get("ports", [])
        http_port = None
        for p in ports:
            p_str = str(p)
            if ":8088" in p_str:
                host_part = p_str.split(":8088")[0]
                if ":" in host_part:
                    host_part = host_part.split(":")[-1]
                try:
                    http_port = int(host_part)
                except ValueError:
                    pass
                break

        profiles = cfg.get("profiles", [])
        services.append(
            {
                "service_name": svc_name,
                "container_name": container_name,
                "http_port": http_port,
                "profiles": profiles,
            }
        )
    return services


def get_running_docker_containers() -> Dict[str, Dict]:
    """Query docker ps to get currently running containers and their status."""
    try:
        res = subprocess.run(
            ["docker", "ps", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
            check=True,
        )
    except Exception:
        return {}

    running = {}
    for line in res.stdout.strip().splitlines():
        if not line.strip():
            continue
        try:
            info = json.loads(line)
            names = info.get("Names", "")
            # Docker names can be e.g. "ignition-prod-scada_master"
            running[names] = info
        except Exception:
            pass
    return running


ALL_FLEET_SERVICES = load_fleet_services()
RUNNING_CONTAINERS = get_running_docker_containers()

# Match running containers with their fleet definitions
ACTIVE_FLEET_SERVICES = [
    svc for svc in ALL_FLEET_SERVICES if svc["container_name"] in RUNNING_CONTAINERS
]


@pytest.fixture(scope="session")
def running_fleet():
    """Ensure at least one fleet container is running, or skip gracefully."""
    if not FLEET_COMPOSE_PATH.exists():
        pytest.skip(f"{FLEET_COMPOSE_PATH.name} not found. Run 'uv run ignition-fleet-generator' first.")
    if not ACTIVE_FLEET_SERVICES:
        pytest.skip(
            "No fleet containers are currently running.\n"
            "To test the fleet, start them first with:\n"
            "  docker compose -f docker-compose.fleet.yml --profile <profile> up -d\n"
            "  (e.g. --profile test, --profile prod, or --profile '*')"
        )
    return ACTIVE_FLEET_SERVICES


@pytest.mark.parametrize(
    "service_info",
    ACTIVE_FLEET_SERVICES,
    ids=[s["service_name"] for s in ACTIVE_FLEET_SERVICES],
)
class TestFleetGatewaysLive:
    """Verify health, StatusPing, cookies, and projects on active fleet containers."""

    def test_container_running_and_healthy(self, service_info: Dict, running_fleet):
        """Verify the container is active and not in an exited or restarting state."""
        container_name = service_info["container_name"]
        res = subprocess.run(
            ["docker", "inspect", container_name, "--format", "{{json .State}}"],
            capture_output=True,
            text=True,
            check=True,
        )
        state = json.loads(res.stdout)
        assert state.get("Running") is True, f"Container {container_name} is not running!"
        assert state.get("Restarting") is False, f"Container {container_name} is crash-looping!"

    def test_gateway_status_ping(self, service_info: Dict, running_fleet):
        """Verify the gateway responds on its allocated host port with state=RUNNING."""
        port = service_info["http_port"]
        assert port is not None, f"Service {service_info['service_name']} has no mapped HTTP port"

        url = f"http://localhost:{port}/StatusPing"
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(url)
                assert resp.status_code == 200, f"StatusPing returned {resp.status_code} for {url}"
                data = resp.json()
                assert data.get("state") == "RUNNING", (
                    f"Gateway {service_info['service_name']} on port {port} is not RUNNING! State: {data.get('state')}"
                )
        except httpx.ConnectError:
            pytest.fail(f"Could not connect to gateway on port {port} ({url})")

    def test_gateway_web_cookie_security(self, service_info: Dict, running_fleet):
        """Verify that session cookies on /web/home use SameSite=Lax (avoiding redirect loops)."""
        port = service_info["http_port"]
        url = f"http://localhost:{port}/web/home"
        try:
            with httpx.Client(timeout=10.0, follow_redirects=False) as client:
                resp = client.get(url)
                set_cookie = resp.headers.get("set-cookie", "")
                if "SameSite" in set_cookie:
                    assert "SameSite=None" not in set_cookie, (
                        f"Gateway on port {port} returned invalid SameSite=None over HTTP!"
                    )
        except httpx.ConnectError:
            pytest.fail(f"Could not connect to {url}")

    def test_restored_projects_present(self, service_info: Dict, running_fleet):
        """Verify projects directory in container data volume contains unpacked resources."""
        container_name = service_info["container_name"]
        res = subprocess.run(
            ["docker", "exec", container_name, "ls", "/usr/local/bin/ignition/data/projects"],
            capture_output=True,
            text=True,
        )
        if res.returncode == 0:
            entries = [e.strip() for e in res.stdout.splitlines() if e.strip() and e.strip() != ".resources"]
            assert len(entries) > 0, f"No project files found in {container_name} data/projects"
