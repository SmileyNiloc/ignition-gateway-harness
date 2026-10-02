import os
import time
import pytest
import httpx


@pytest.fixture(scope="session")
def base_url() -> str:
    """Base URL for the Ignition Gateway under test."""
    return os.getenv("GATEWAY_BASE_URL", "http://localhost:8089")


@pytest.fixture(scope="session")
def wait_for_gateway_ready(base_url: str):
    """Ensure the Ignition Gateway is in RUNNING state before running tests."""
    timeout = 45.0
    start = time.time()
    with httpx.Client(base_url=base_url, timeout=5.0) as client:
        while time.time() - start < timeout:
            try:
                resp = client.get("/StatusPing")
                if resp.status_code == 200 and resp.json().get("state") == "RUNNING":
                    return
            except Exception:
                pass
            time.sleep(2)
    pytest.fail(f"Ignition Gateway at {base_url} did not report RUNNING within {timeout}s")


@pytest.fixture
def http_client(base_url: str, wait_for_gateway_ready):
    """Synchronous HTTPX client with automatic redirect following."""
    with httpx.Client(base_url=base_url, timeout=10.0, follow_redirects=True) as client:
        yield client


@pytest.fixture
async def async_http_client(base_url: str, wait_for_gateway_ready):
    """Asynchronous HTTPX client with automatic redirect following."""
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0, follow_redirects=True) as client:
        yield client
