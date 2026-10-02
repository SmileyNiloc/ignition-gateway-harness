import httpx
import pytest


def test_gateway_status_ping(http_client: httpx.Client):
    """Verify that the Ignition Gateway StatusPing returns RUNNING state."""
    response = http_client.get("/StatusPing")
    assert response.status_code == 200
    data = response.json()
    assert data.get("state") == "RUNNING"


def test_gateway_home_page_loads(http_client: httpx.Client):
    """Verify that the Gateway Web UI loads successfully without redirect loops."""
    response = http_client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    assert "Ignition" in response.text


def test_session_cookie_security(base_url: str, wait_for_gateway_ready):
    """
    Ensure the gateway does not send invalid SameSite=None on plain HTTP,
    which triggers infinite redirect loops in modern browsers.
    """
    with httpx.Client(base_url=base_url, timeout=10.0, follow_redirects=False) as client:
        response = client.get("/web/home")
        set_cookie = response.headers.get("set-cookie", "")

        # If SameSite is present in the cookie over HTTP, it must NOT be None
        if "SameSite" in set_cookie:
            assert "SameSite=None" not in set_cookie, (
                "Found SameSite=None on insecure HTTP! Modern browsers will reject this cookie "
                "and cause an infinite redirect loop."
            )


@pytest.mark.asyncio
async def test_async_gateway_ping(async_http_client: httpx.AsyncClient):
    """Demonstrate asynchronous API testing with httpx."""
    response = await async_http_client.get("/StatusPing")
    assert response.status_code == 200
    assert response.json()["state"] == "RUNNING"
