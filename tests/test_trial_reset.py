"""Unit and Playwright integration tests for Automated Trial Reset."""

from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import threading
from typing import Dict
import urllib.parse
import pytest

from ignition_gateway_harness.trial_reset import (
    TrialResetAutomator,
    TrialResetResult,
    TrialStatus,
    parse_time_remaining,
    print_trial_results_table,
    resolve_gateway_targets,
)


# ==============================================================================
# 1. Target Resolution & Parser Unit Tests
# ==============================================================================


class TestTargetResolutionAndParser:
    """Verify target string resolution and regex time parsing."""

    def test_resolve_numeric_ports(self):
        targets = resolve_gateway_targets(["8100", "8105"])
        assert len(targets) == 2
        assert targets[0] == ("gateway-8100", "http://localhost:8100")
        assert targets[1] == ("gateway-8105", "http://localhost:8105")

    def test_resolve_full_urls(self):
        targets = resolve_gateway_targets(["http://192.168.1.100:8088", "https://scada.corp:8043"])
        assert len(targets) == 2
        assert targets[0] == ("http://192.168.1.100:8088", "http://192.168.1.100:8088")
        assert targets[1] == ("https://scada.corp:8043", "https://scada.corp:8043")

    def test_resolve_from_compose_file(self, tmp_path: Path):
        compose_file = tmp_path / "docker-compose.fleet.yml"
        compose_file.write_text(
            """
services:
  prod-fe1:
    ports:
      - "8101:8088"
  prod-scada:
    ports:
      - "8104:8088"
"""
        )
        # Specific target
        targets = resolve_gateway_targets(["prod-fe1"], compose_path=compose_file)
        assert targets == [("prod-fe1", "http://localhost:8101")]

        # All targets
        all_targets = resolve_gateway_targets(["all"], compose_path=compose_file)
        assert len(all_targets) == 2
        assert ("prod-fe1", "http://localhost:8101") in all_targets
        assert ("prod-scada", "http://localhost:8104") in all_targets

    def test_resolve_host_port_format(self):
        targets = resolve_gateway_targets(["localhost:8100", "192.168.1.50:8088"])
        assert len(targets) == 2
        assert targets[0] == ("localhost:8100", "http://localhost:8100")
        assert targets[1] == ("192.168.1.50:8088", "http://192.168.1.50:8088")

    def test_resolve_dict_ports_in_compose(self, tmp_path: Path):
        compose_file = tmp_path / "docker-compose.dictports.yml"
        compose_file.write_text(
            """
services:
  test-gw:
    ports:
      - target: 8088
        published: 8150
        protocol: tcp
"""
        )
        targets = resolve_gateway_targets(["all"], compose_path=compose_file)
        assert targets == [("test-gw", "http://localhost:8150")]

    def test_parse_time_remaining_formats(self):
        # HH:MM:SS
        text1 = "Trial Mode - 01:45:22 remaining. [Reset Trial]"
        t_str, sec = parse_time_remaining(text1)
        assert t_str == "01:45:22"
        assert sec == 1 * 3600 + 45 * 60 + 22

        # MM:SS
        text2 = "Trial: 04:30 left"
        t_str, sec = parse_time_remaining(text2)
        assert t_str == "04:30"
        assert sec == 4 * 60 + 30

        # Minutes string
        text3 = "Trial Mode - 110 minutes remaining"
        t_str, sec = parse_time_remaining(text3)
        assert t_str == "110 min"
        assert sec == 110 * 60

        # Expired
        text4 = "Trial Expired"
        t_str, sec = parse_time_remaining(text4)
        assert t_str is None

    def test_print_results_table(self, capsys):
        results = {
            "gw1": TrialResetResult(
                target="gw1",
                url="http://localhost:8100",
                status=TrialStatus.RESET_SUCCESS,
                success=True,
                initial_time_remaining="00:30:00",
                final_time_remaining="02:00:00",
            ),
            "gw2": TrialResetResult(
                target="gw2",
                url="http://localhost:8101",
                status=TrialStatus.RESET_FAILED,
                success=False,
                error="Timeout",
            ),
        }
        ret = print_trial_results_table(results)
        captured = capsys.readouterr().out
        assert "gw1" in captured
        assert "RESET_SUCCESS" in captured
        assert "gw2" in captured
        assert ret == 1  # 1 failure


# ==============================================================================
# 2. Mock Ignition Gateway HTTP Server
# ==============================================================================


class MockIgnitionHandler(BaseHTTPRequestHandler):
    """
    Mock HTTP handler simulating Ignition Gateway Web Interface and trial flows.
    Maintains session state and handles:
    - Active trial banner
    - Login redirect and submission
    - Confirmation modal dialog
    - Expired trial
    - Edge trial
    - Licensed state
    """

    # Class-level state per gateway scenario
    trial_states: Dict[str, Dict] = {}

    def log_message(self, format, *args):
        # Suppress standard HTTP request logging in test output
        return

    def do_GET(self):
        url_parts = urllib.parse.urlparse(self.path)
        path = url_parts.path
        scenario = url_parts.query if url_parts.query else "standard_active"

        # Check /StatusPing
        if path == "/StatusPing":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"state": "RUNNING"}')
            return

        state = self.trial_states.setdefault(
            scenario,
            {
                "time_remaining": "01:23:45",
                "is_expired": False,
                "logged_in": False,
                "reset_triggered": False,
                "confirmed": False,
                "scenario": scenario,
            },
        )

        # Route: Login page
        if "/idp-log-in" in path or "/login" in path:
            html = f"""
            <!DOCTYPE html>
            <html>
            <head><title>Ignition Gateway Login</title></head>
            <body>
                <h1>Sign In</h1>
                <form action="/web/login-submit?{scenario}" method="post">
                    <label>Username: <input type="text" name="username" id="username" /></label>
                    <label>Password: <input type="password" name="password" id="password" /></label>
                    <button type="submit" id="login-btn">Sign In</button>
                </form>
            </body>
            </html>
            """
            self._send_html(html)
            return

        # Route: Confirmation Modal Dialog
        if "/confirm-dialog" in path:
            state["reset_triggered"] = True
            html = f"""
            <!DOCTYPE html>
            <html>
            <head><title>Reset Trial Confirmation</title></head>
            <body>
                <div role="dialog" class="modal">
                    <h2>Reset Trial?</h2>
                    <p>Are you sure you want to restart the 2-hour trial period?</p>
                    <form action="/web/do-reset?{scenario}" method="post">
                        <button type="submit" class="btn-primary" id="confirm-btn">Reset</button>
                    </form>
                </div>
            </body>
            </html>
            """
            self._send_html(html)
            return

        # Route: Main Gateway Web Interface
        if scenario == "licensed":
            html = """
            <!DOCTYPE html>
            <html>
            <head><title>Ignition - Home</title></head>
            <body>
                <div class="header"><h1>Ignition Gateway (Licensed Enterprise Edition)</h1></div>
                <div id="home">Gateway running normally.</div>
            </body>
            </html>
            """
            self._send_html(html)
            return

        if scenario == "edge":
            time_disp = state["time_remaining"]
            html = f"""
            <!DOCTYPE html>
            <html>
            <head><title>Ignition Edge - Home</title></head>
            <body>
                <div class="trial-banner">
                    <span>Edge Trial Mode - {time_disp} remaining</span>
                    <a href="/web/confirm-dialog?{scenario}" class="trial-reset">Reset</a>
                </div>
            </body>
            </html>
            """
            self._send_html(html)
            return

        if scenario == "expired":
            if state["confirmed"]:
                time_disp = "02:00:00"
                html = f"""
                <!DOCTYPE html>
                <html>
                <head><title>Ignition - Home</title></head>
                <body>
                    <div class="trial-banner">
                        <span>Trial Mode - {time_disp} remaining</span>
                    </div>
                </body>
                </html>
                """
            else:
                html = f"""
                <!DOCTYPE html>
                <html>
                <head><title>Ignition - Home</title></head>
                <body>
                    <div class="trial-banner">
                        <span>Trial Expired</span>
                        <a href="/web/confirm-dialog?{scenario}" class="trial-reset">Restart Trial</a>
                    </div>
                </body>
                </html>
                """
            self._send_html(html)
            return

        if scenario == "requires_login":
            if not state["logged_in"]:
                # Require login first
                self.send_response(302)
                self.send_header("Location", f"/web/idp-log-in?{scenario}")
                self.end_headers()
                return

        # Standard Active Trial
        time_disp = "02:00:00" if state["confirmed"] else state["time_remaining"]
        html = f"""
        <!DOCTYPE html>
        <html>
        <head><title>Ignition - Home</title></head>
        <body>
            <div class="trial-banner">
                <span id="trial-status">Trial Mode - {time_disp} remaining</span>
                <a href="/web/confirm-dialog?{scenario}" class="trial-reset">Reset Trial</a>
            </div>
            <div id="home">Main gateway dashboard</div>
        </body>
        </html>
        """
        self._send_html(html)

    def do_POST(self):
        url_parts = urllib.parse.urlparse(self.path)
        path = url_parts.path
        scenario = url_parts.query if url_parts.query else "standard_active"
        state = self.trial_states.setdefault(scenario, {})

        if "/login-submit" in path:
            state["logged_in"] = True
            # Redirect to confirmation or home
            self.send_response(302)
            self.send_header("Location", f"/web/confirm-dialog?{scenario}")
            self.end_headers()
            return

        if "/do-reset" in path:
            state["confirmed"] = True
            state["time_remaining"] = "02:00:00"
            self.send_response(302)
            self.send_header("Location", f"/web/home?{scenario}")
            self.end_headers()
            return

        self.send_response(404)
        self.end_headers()

    def _send_html(self, html: str):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def mock_gateway_server():
    """Start local mock HTTP server serving mock Ignition Gateway pages."""
    server = HTTPServer(("127.0.0.1", 0), MockIgnitionHandler)
    port = server.server_port
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    base_url = f"http://127.0.0.1:{port}"
    yield base_url
    server.shutdown()
    server.server_close()


# ==============================================================================
# 3. Playwright Browser Automation Integration Tests
# ==============================================================================


class TestPlaywrightTrialReset:
    """End-to-end integration tests driving headless Chromium to reset trials."""

    def test_standard_active_trial_reset(self, mock_gateway_server: str):
        """Active trial (01:23:45) -> clicks Reset Trial -> confirms modal -> verifies 02:00:00."""
        automator = TrialResetAutomator(headless=True, timeout=10.0)
        url = f"{mock_gateway_server}?standard_active"

        result = automator.reset_gateway(target="test-active-gw", url=url)

        assert result.success is True
        assert result.status == TrialStatus.RESET_SUCCESS
        assert result.initial_time_remaining == "01:23:45"
        assert result.final_time_remaining == "02:00:00"
        assert result.error is None
        assert result.duration_seconds > 0

    def test_protected_trial_reset_with_login(self, mock_gateway_server: str):
        """Trial requiring authentication -> submits admin credentials -> confirms -> succeeds."""
        automator = TrialResetAutomator(
            username="admin",
            password="password",
            headless=True,
            timeout=10.0,
        )
        url = f"{mock_gateway_server}?requires_login"

        result = automator.reset_gateway(target="test-auth-gw", url=url)

        assert result.success is True
        assert result.status == TrialStatus.RESET_SUCCESS
        assert result.final_time_remaining == "02:00:00"

    def test_expired_trial_restart(self, mock_gateway_server: str):
        """Expired trial -> clicks Restart Trial -> confirms -> verified active."""
        automator = TrialResetAutomator(headless=True, timeout=10.0)
        url = f"{mock_gateway_server}?expired"

        result = automator.reset_gateway(target="test-expired-gw", url=url)

        assert result.success is True
        assert result.status == TrialStatus.RESET_SUCCESS
        assert result.final_time_remaining == "02:00:00"

    def test_edge_gateway_trial_reset(self, mock_gateway_server: str):
        """Ignition Edge banner variant -> clicks Reset -> confirms -> succeeds."""
        automator = TrialResetAutomator(headless=True, timeout=10.0)
        url = f"{mock_gateway_server}?edge"

        result = automator.reset_gateway(target="test-edge-gw", url=url)

        assert result.success is True
        assert result.status == TrialStatus.RESET_SUCCESS

    def test_licensed_gateway_detected_not_in_trial(self, mock_gateway_server: str):
        """Gateway with permanent enterprise license -> detected as NOT_IN_TRIAL."""
        automator = TrialResetAutomator(headless=True, timeout=10.0)
        url = f"{mock_gateway_server}?licensed"

        result = automator.reset_gateway(target="test-licensed-gw", url=url)

        assert result.success is True
        assert result.status == TrialStatus.NOT_IN_TRIAL
        assert result.initial_time_remaining is None

    def test_unreachable_offline_gateway(self):
        """Gateway offline on non-listening port -> reports OFFLINE."""
        automator = TrialResetAutomator(headless=True, timeout=3.0)
        result = automator.reset_gateway(target="offline-gw", url="http://127.0.0.1:59998")

        assert result.success is False
        assert result.status == TrialStatus.OFFLINE
        assert "unreachable" in (result.error or "").lower()

    def test_concurrent_fleet_reset(self, mock_gateway_server: str):
        """Concurrent reset across multiple mock gateway targets."""
        automator = TrialResetAutomator(headless=True, timeout=10.0)
        targets = [
            ("fleet-gw-1", f"{mock_gateway_server}?standard_active"),
            ("fleet-gw-2", f"{mock_gateway_server}?edge"),
            ("fleet-gw-3", f"{mock_gateway_server}?licensed"),
        ]

        results = automator.reset_fleet(targets, max_workers=3)

        assert len(results) == 3
        assert results["fleet-gw-1"].success is True
        assert results["fleet-gw-2"].success is True
        assert results["fleet-gw-3"].status == TrialStatus.NOT_IN_TRIAL

    def test_daemon_mode_single_iteration(self, mock_gateway_server: str):
        """Verify daemon mode executes one cycle and terminates with max_iterations=1."""
        automator = TrialResetAutomator(headless=True, timeout=10.0)
        targets = [("daemon-gw", f"{mock_gateway_server}?standard_active")]

        # max_iterations=1 ensures it runs exactly one cycle without sleeping
        automator.run_daemon(targets, interval_minutes=1, max_iterations=1)

    def test_reset_gateway_from_async_context(self, mock_gateway_server: str):
        """Verify calling reset_gateway from inside an active asyncio loop does not crash."""
        import asyncio
        from concurrent.futures import ThreadPoolExecutor

        async def _async_caller():
            automator = TrialResetAutomator(headless=True, timeout=10.0)
            url = f"{mock_gateway_server}?standard_active"

            # reset_gateway detects running loop and offloads to worker thread cleanly
            result = automator.reset_gateway(target="test-async-gw", url=url)
            assert result.success is True
            assert result.status == TrialStatus.RESET_SUCCESS

        def _run(coro):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    return pool.submit(asyncio.run, coro).result()
            return asyncio.run(coro)

        _run(_async_caller())
