"""Automated Trial Reset Module for Ignition Gateway Fleet.

Uses Playwright to automate logging in and resetting the 2-hour trial timer across
any or all Ignition Gateways (Standard and Edge) in headless or headed mode.
Handles IdP login forms, confirmation dialogs, Perspective session navigation,
and concurrent fleet-wide execution.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
import logging
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.error
import urllib.request
import yaml

from playwright.sync_api import Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

logger = logging.getLogger("TrialReset")


# ==============================================================================
# Data Models and Status Codes
# ==============================================================================


class TrialStatus:
    """Trial status enum constants."""

    ACTIVE = "active"
    EXPIRED = "expired"
    NOT_IN_TRIAL = "not_in_trial"
    RESET_SUCCESS = "reset_success"
    RESET_FAILED = "reset_failed"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


@dataclass
class TrialResetResult:
    """Outcome of an automated trial check or reset attempt on a gateway."""

    target: str
    url: str
    status: str
    success: bool
    initial_time_remaining: Optional[str] = None
    final_time_remaining: Optional[str] = None
    seconds_remaining: Optional[int] = None
    error: Optional[str] = None
    duration_seconds: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        """Convert result to dictionary."""
        return asdict(self)


# ==============================================================================
# Target Resolver
# ==============================================================================


def resolve_gateway_targets(
    raw_targets: Optional[List[str]] = None,
    compose_path: Optional[Path | str] = None,
    default_base_url: str = "http://localhost:8088",
) -> List[Tuple[str, str]]:
    """
    Resolve target descriptors (service names, ports, URLs, or 'all') to list of (service_name, url).

    Examples:
    - ["8100", "8101"] -> [("gateway-8100", "http://localhost:8100"), ("gateway-8101", "http://localhost:8101")]
    - ["prod-fe1"] -> resolved from compose file to its mapped host port
    - ["http://192.168.1.50:8088"] -> [("custom", "http://192.168.1.50:8088")]
    - ["all"] or None -> resolve all services from compose_path (or fallback default_base_url)
    """
    resolved: List[Tuple[str, str]] = []
    compose_services: Dict[str, str] = {}  # svc_name -> url

    c_path = Path(compose_path).resolve() if compose_path else None
    if c_path and c_path.exists() and c_path.is_file():
        try:
            with open(c_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            for svc_name, cfg in data.get("services", {}).items():
                ports = cfg.get("ports", [])
                for p in ports:
                    if isinstance(p, dict):
                        target_p = str(p.get("target", ""))
                        if target_p == "8088":
                            host_port = str(p.get("published", ""))
                            if host_port:
                                compose_services[svc_name] = f"http://localhost:{host_port}"
                                break
                    else:
                        p_str = str(p)
                        if ":8088" in p_str:
                            host_port = p_str.split(":8088")[0].split(":")[-1]
                            compose_services[svc_name] = f"http://localhost:{host_port}"
                            break
        except Exception as exc:
            logger.warning("Could not read compose file %s: %s", c_path, exc)

    if not raw_targets or "all" in [t.lower() for t in raw_targets] or "fleet" in [t.lower() for t in raw_targets]:
        if compose_services:
            for sname, url in sorted(compose_services.items()):
                resolved.append((sname, url))
        else:
            resolved.append(("default", default_base_url))
        return resolved

    for item in raw_targets:
        target = item.strip()
        if not target:
            continue

        if target.startswith("http://") or target.startswith("https://"):
            resolved.append((target, target))
        elif target.isdigit():
            port = int(target)
            resolved.append((f"gateway-{port}", f"http://localhost:{port}"))
        elif ":" in target and not target.startswith("/"):
            # Format host:port, e.g. "localhost:8100" or "10.0.0.5:8088"
            resolved.append((target, f"http://{target}"))
        elif target in compose_services:
            resolved.append((target, compose_services[target]))
        else:
            # Check partial match in compose services
            matched = False
            for sname, url in compose_services.items():
                if target.lower() in sname.lower():
                    resolved.append((sname, url))
                    matched = True
            if not matched:
                # Treat as simple hostname
                resolved.append((target, f"http://{target}:8088"))

    return resolved


def parse_time_remaining(text: str) -> Tuple[Optional[str], Optional[int]]:
    """Parse time remaining from trial banner text (e.g. '01:45:22' or '115 min')."""
    # Pattern 1: HH:MM:SS or MM:SS
    m = re.search(r"(\d{1,2}:\d{2}(?::\d{2})?)", text)
    if m:
        time_str = m.group(1)
        parts = [int(p) for p in time_str.split(":")]
        if len(parts) == 3:
            total_sec = parts[0] * 3600 + parts[1] * 60 + parts[2]
        elif len(parts) == 2:
            total_sec = parts[0] * 60 + parts[1]
        else:
            total_sec = parts[0]
        return time_str, total_sec

    # Pattern 2: X minutes remaining
    m_min = re.search(r"(\d+)\s*(?:minutes?|min)", text, re.IGNORECASE)
    if m_min:
        mins = int(m_min.group(1))
        return f"{mins} min", mins * 60

    return None, None


# ==============================================================================
# Playwright Trial Reset Automator
# ==============================================================================


class TrialResetAutomator:
    """Automates checking and resetting the trial period for Ignition Gateways via Playwright."""

    def __init__(
        self,
        username: str = "admin",
        password: str = "password",
        headless: bool = True,
        timeout: float = 30.0,
    ) -> None:
        self.username = username
        self.password = password
        self.headless = headless
        self.timeout = timeout

    def check_gateway_reachable(self, url: str) -> bool:
        """Quick healthcheck using StatusPing or HTTP GET before launching full browser."""
        import ssl
        # Gateways frequently use self-signed certificates for HTTPS
        ssl_ctx = ssl._create_unverified_context()

        try:
            ping_url = f"{url.rstrip('/')}/StatusPing"
            req = urllib.request.Request(ping_url, headers={"User-Agent": "TrialResetHealthCheck"})
            with urllib.request.urlopen(req, timeout=3.0, context=ssl_ctx) as resp:
                return resp.status in (200, 302, 401, 403)
        except Exception:
            pass

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "TrialResetHealthCheck"})
            with urllib.request.urlopen(req, timeout=3.0, context=ssl_ctx) as resp:
                return resp.status in (200, 302, 401, 403)
        except Exception:
            return False

    def handle_login_if_present(self, page: Page) -> bool:
        """Detect and complete IdP or classic gateway admin login forms."""
        # Check if URL indicates login or page contains credentials inputs
        current_url = page.url.lower()
        is_login_page = any(kw in current_url for kw in ["idp-log-in", "login", "auth"])

        # Selectors for password
        password_selectors = [
            "input[name='password']",
            "input#password",
            "input[type='password']:visible",
        ]
        pwd_field = None
        for sel in password_selectors:
            try:
                locator = page.locator(sel).first
                if locator.is_visible(timeout=1000):
                    pwd_field = locator
                    is_login_page = True
                    break
            except Exception:
                continue

        # Selectors for username
        username_selectors = [
            "input[name='username']",
            "input#username",
            "input[name='user']",
            "input[name='identifier']",
        ]
        if is_login_page:
            username_selectors.append("input[type='text']:visible")

        user_field = None
        for sel in username_selectors:
            try:
                locator = page.locator(sel).first
                if locator.is_visible(timeout=1000):
                    user_field = locator
                    break
            except Exception:
                continue

        if not is_login_page or (not user_field and not pwd_field):
            return False

        logger.info("Login form detected on %s. Submitting credentials...", page.url)

        if user_field:
            user_field.fill(self.username)

        if pwd_field:
            pwd_field.fill(self.password)

        # Submit form
        submit_selectors = [
            "button[type='submit']",
            "input[type='submit']",
            "button:has-text('Sign In')",
            "button:has-text('Log In')",
            "button:has-text('Submit')",
            "button.primary",
        ]
        submitted = False
        for ssel in submit_selectors:
            try:
                btn = page.locator(ssel).first
                if btn.is_visible(timeout=1000):
                    btn.click()
                    submitted = True
                    break
            except Exception:
                continue

        if not submitted:
            # Fall back to pressing Enter in password field
            if pwd_field:
                pwd_field.press("Enter")

        page.wait_for_load_state("domcontentloaded", timeout=self.timeout * 1000)
        return True

    def handle_confirmation_dialog_if_present(self, page: Page) -> bool:
        """Detect and click confirmation modal buttons (e.g. 'Reset Trial', 'Restart', 'Confirm')."""
        confirm_selectors = [
            "button:has-text('Reset Trial')",
            "button:has-text('Restart Trial')",
            "button:has-text('Reset')",
            "button:has-text('Restart')",
            "button:has-text('Confirm')",
            "button:has-text('Yes')",
            "button:has-text('OK')",
            "[role='dialog'] button:has-text('Reset')",
            ".modal button:has-text('Reset')",
            ".modal-footer button.btn-primary",
        ]

        for csel in confirm_selectors:
            try:
                btn = page.locator(csel).first
                if btn.is_visible(timeout=1500):
                    logger.info("Confirmation dialog detected. Clicking %s...", csel)
                    btn.click()
                    page.wait_for_load_state("domcontentloaded", timeout=self.timeout * 1000)
                    return True
            except Exception:
                continue
        return False

    def detect_trial_status(self, page: Page) -> Tuple[str, Optional[str], Optional[int]]:
        """
        Inspect the rendered gateway page to detect trial status and time remaining.

        Returns (status, time_str, seconds_remaining).
        """
        banner_selectors = [
            ".trial-banner",
            ".trial-container",
            ".trial-status",
            "#trial-status",
            "[data-trial-time]",
            ".trial-timer",
            "div:has-text('Trial Mode')",
            "span:has-text('Trial Mode')",
            "div:has-text('Trial Expired')",
            "span:has-text('Trial Expired')",
            "div:has-text('Edge Trial')",
        ]

        text_content = ""
        for sel in banner_selectors:
            try:
                locator = page.locator(sel).first
                if locator.is_visible(timeout=1500):
                    text_content = locator.inner_text()
                    break
            except Exception:
                continue

        if not text_content:
            # Fall back to page body text
            try:
                body_text = page.locator("body").inner_text()
                if "Trial Mode" in body_text or "Trial Expired" in body_text or "Reset Trial" in body_text:
                    text_content = body_text
            except Exception:
                pass

        if not text_content:
            return TrialStatus.NOT_IN_TRIAL, None, None

        time_str, sec_remaining = parse_time_remaining(text_content)

        if "expired" in text_content.lower() or (sec_remaining is not None and sec_remaining <= 0):
            return TrialStatus.EXPIRED, time_str, 0

        if time_str or "trial mode" in text_content.lower() or "edge trial" in text_content.lower():
            return TrialStatus.ACTIVE, time_str, sec_remaining

        return TrialStatus.NOT_IN_TRIAL, None, None

    def find_and_click_reset_trigger(self, page: Page) -> bool:
        """Find and click the Reset Trial or Restart Trial button/link on the page."""
        reset_selectors = [
            "a.trial-reset",
            "button.trial-reset",
            "button:has-text('Reset Trial')",
            "button:has-text('Restart Trial')",
            "a:has-text('Reset Trial')",
            "a:has-text('Restart Trial')",
            "button:has-text('Reset')",
            "a:has-text('Reset')",
            "button:has-text('Restart')",
            "a:has-text('Restart')",
            "[data-action='reset-trial']",
            "[id*='reset-trial']",
        ]

        for rsel in reset_selectors:
            try:
                trigger = page.locator(rsel).first
                if trigger.is_visible(timeout=1500):
                    logger.info("Found trial reset trigger using %s. Clicking...", rsel)
                    trigger.click()
                    page.wait_for_load_state("domcontentloaded", timeout=self.timeout * 1000)
                    return True
            except Exception:
                continue
        return False

    def reset_gateway(self, target: str, url: str) -> TrialResetResult:
        """
        Execute full trial check and reset flow for a single gateway.
        Safe for both synchronous and asyncio-managed thread execution.
        """
        is_loop_running = False
        try:
            import asyncio
            loop = asyncio.get_running_loop()
            if loop.is_running():
                is_loop_running = True
        except RuntimeError:
            pass

        if is_loop_running:
            # Playwright Sync API cannot be instantiated inside a thread with an active event loop.
            # Offload execution to a clean worker thread.
            with ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(self._reset_gateway_impl, target, url).result()

        return self._reset_gateway_impl(target, url)

    def _reset_gateway_impl(self, target: str, url: str) -> TrialResetResult:
        """
        Internal implementation of trial check and reset flow for a single gateway.

        1. Navigates to gateway web UI (handling Perspective redirects)
        2. Detects current trial state
        3. Clicks reset trial button
        4. Handles login / IdP auth if required
        5. Confirms modal dialog
        6. Verifies refreshed trial status
        """
        start_time = time.time()
        clean_url = url.rstrip("/")

        parsed_url = urllib.parse.urlsplit(url)
        base_origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
        query_suffix = f"?{parsed_url.query}" if parsed_url.query else ""
        home_url = f"{base_origin}/web/home{query_suffix}"
        status_url = f"{base_origin}/web/status{query_suffix}"
        overview_url = f"{base_origin}/web/config/sys.overview{query_suffix}"

        # Check reachability first
        if not self.check_gateway_reachable(clean_url):
            return TrialResetResult(
                target=target,
                url=clean_url,
                status=TrialStatus.OFFLINE,
                success=False,
                error=f"Gateway at {clean_url} is unreachable",
                duration_seconds=round(time.time() - start_time, 2),
            )

        with sync_playwright() as playwright:
            browser: Optional[Browser] = None
            try:
                browser = playwright.chromium.launch(
                    headless=self.headless,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
                )
                context: BrowserContext = browser.new_context(
                    ignore_https_errors=True,
                    viewport={"width": 1280, "height": 800},
                )
                page = context.new_page()
                page.set_default_timeout(self.timeout * 1000)

                # Step 1: Navigate to Gateway home
                try:
                    page.goto(home_url, wait_until="domcontentloaded")
                except PlaywrightTimeoutError:
                    # Retry with root URL
                    page.goto(clean_url, wait_until="domcontentloaded")

                # If on a perspective view, navigate back to gateway web interface
                if "/data/perspective" in page.url:
                    page.goto(home_url, wait_until="domcontentloaded")

                # Handle immediate login if homepage requires auth
                self.handle_login_if_present(page)

                # Step 2: Detect initial status
                status, init_time_str, sec_remaining = self.detect_trial_status(page)

                # If trial not detected on home, try status page
                if status == TrialStatus.NOT_IN_TRIAL:
                    try:
                        page.goto(status_url, wait_until="domcontentloaded")
                        self.handle_login_if_present(page)
                        status, init_time_str, sec_remaining = self.detect_trial_status(page)
                    except Exception:
                        pass

                if status == TrialStatus.NOT_IN_TRIAL:
                    return TrialResetResult(
                        target=target,
                        url=clean_url,
                        status=TrialStatus.NOT_IN_TRIAL,
                        success=True,
                        initial_time_remaining=None,
                        final_time_remaining=None,
                        error=None,
                        duration_seconds=round(time.time() - start_time, 2),
                    )

                # Step 3: Trigger Reset
                clicked = self.find_and_click_reset_trigger(page)
                if not clicked:
                    # Try navigating directly to reset action or overview
                    page.goto(overview_url, wait_until="domcontentloaded")
                    self.handle_login_if_present(page)
                    clicked = self.find_and_click_reset_trigger(page)

                # Step 4: Handle authentication if clicking reset prompted for login
                self.handle_login_if_present(page)


                # Step 5: Handle confirmation dialog
                self.handle_confirmation_dialog_if_present(page)

                # Short wait for trial reset state to propagate
                page.wait_for_timeout(1000)

                # Step 6: Verify final status
                final_status, final_time_str, final_sec = self.detect_trial_status(page)

                success = False
                if final_status == TrialStatus.ACTIVE:
                    # Successfully in active trial
                    success = True
                elif final_status == TrialStatus.EXPIRED:
                    success = False
                elif clicked and final_status != TrialStatus.EXPIRED:
                    success = True

                return TrialResetResult(
                    target=target,
                    url=clean_url,
                    status=TrialStatus.RESET_SUCCESS if success else TrialStatus.RESET_FAILED,
                    success=success,
                    initial_time_remaining=init_time_str,
                    final_time_remaining=final_time_str,
                    seconds_remaining=final_sec,
                    error=None if success else "Failed to verify trial reset on gateway",
                    duration_seconds=round(time.time() - start_time, 2),
                )

            except Exception as exc:
                return TrialResetResult(
                    target=target,
                    url=clean_url,
                    status=TrialStatus.RESET_FAILED,
                    success=False,
                    initial_time_remaining=None,
                    error=str(exc),
                    duration_seconds=round(time.time() - start_time, 2),
                )
            finally:
                if browser:
                    try:
                        browser.close()
                    except Exception:
                        pass

    def reset_fleet(
        self,
        targets: List[Tuple[str, str]],
        max_workers: int = 4,
    ) -> Dict[str, TrialResetResult]:
        """
        Execute trial reset across multiple targets concurrently.

        Returns dictionary of target_name -> TrialResetResult.
        """
        results: Dict[str, TrialResetResult] = {}
        if not targets:
            return results

        # Single target fast path
        if len(targets) == 1:
            name, url = targets[0]
            res = self.reset_gateway(name, url)
            results[name] = res
            return results

        # Concurrent execution across fleet
        workers = min(max_workers, len(targets))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_target = {
                executor.submit(self.reset_gateway, name, url): name
                for name, url in targets
            }
            for future in as_completed(future_to_target):
                name = future_to_target[future]
                try:
                    res = future.result()
                    results[name] = res
                except Exception as exc:
                    results[name] = TrialResetResult(
                        target=name,
                        url="",
                        status=TrialStatus.RESET_FAILED,
                        success=False,
                        error=str(exc),
                    )

        return results

    def run_daemon(
        self,
        targets: List[Tuple[str, str]],
        interval_minutes: int = 105,
        max_iterations: Optional[int] = None,
    ) -> None:
        """
        Run continuous background loop checking and resetting trials before 2-hour expiration.
        """
        iteration = 0
        logger.info(
            "Starting Trial Reset Daemon for %d targets (interval: %d minutes)",
            len(targets),
            interval_minutes,
        )
        while True:
            iteration += 1
            logger.info("=== Trial Reset Cycle #%d ===", iteration)
            results = self.reset_fleet(targets)
            for name, r in results.items():
                logger.info(
                    "Gateway: %s | URL: %s | Status: %s | Success: %s | Remaining: %s",
                    name,
                    r.url,
                    r.status,
                    r.success,
                    r.final_time_remaining or r.initial_time_remaining,
                )

            if max_iterations is not None and iteration >= max_iterations:
                logger.info("Reached maximum iterations (%d). Stopping daemon.", max_iterations)
                break

            sleep_seconds = interval_minutes * 60
            logger.info("Sleeping for %d minutes until next reset cycle...", interval_minutes)
            time.sleep(sleep_seconds)


# ==============================================================================
# CLI Helper
# ==============================================================================


def print_trial_results_table(results: Dict[str, TrialResetResult]) -> int:
    """Print formatted summary table of trial reset results."""
    header = f"{'TARGET':<22} {'STATUS':<15} {'SUCCESS':<8} {'REMAINING':<12} {'TIME':<7} {'MESSAGE':<25}"
    print("=" * len(header))
    print(header)
    print("=" * len(header))

    failures = 0
    for name, r in sorted(results.items()):
        status_disp = r.status.upper()
        succ_disp = "YES" if r.success else "NO"
        rem_disp = r.final_time_remaining or r.initial_time_remaining or "-"
        dur_disp = f"{r.duration_seconds:.1f}s"
        msg = r.error if r.error else "OK"
        print(f"{name:<22} {status_disp:<15} {succ_disp:<8} {rem_disp:<12} {dur_disp:<7} {msg:<25}")
        if not r.success and r.status != TrialStatus.NOT_IN_TRIAL:
            failures += 1

    print("=" * len(header))
    return 1 if failures > 0 else 0
