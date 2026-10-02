import re
import pytest
from playwright.sync_api import Page, expect


def test_gateway_ui_home(page: Page, base_url: str, wait_for_gateway_ready):
    """
    Use Playwright to render the Ignition Gateway homepage in a headless browser.
    Verifies that the page renders without redirect loops and navigation exists.
    """
    page.goto(base_url)

    # Wait for the main gateway shell to load (matches e.g. "Ignition-ignition - Ignition Gateway")
    expect(page).to_have_title(re.compile(r"Ignition.*Gateway"), timeout=15000)

    # Verify primary navigation tabs exist
    expect(page.locator("#home")).to_be_visible()
    expect(page.locator("#status")).to_be_visible()
    expect(page.locator("#config")).to_be_visible()


def test_gateway_ui_navigation_requires_auth(page: Page, base_url: str, wait_for_gateway_ready):
    """
    Verify navigating from Home to Status page prompts for IdP authentication
    or enters the status section, matching Ignition's security configuration.
    """
    page.goto(base_url)
    expect(page).to_have_title(re.compile(r"Ignition.*Gateway"), timeout=15000)

    # Click the Status navigation link
    page.locator("#status a").click(force=True)

    # Protected Status page will redirect to IdP login or status section
    expect(page).to_have_url(re.compile(r".*(/web/status/|/web/idp-log-in).*"))
