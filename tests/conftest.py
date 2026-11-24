"""Fixtures for the ADT Pulse tests."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load the integration from custom_components."""
    return


def make_pulse(site_id: str = "12345") -> MagicMock:
    """Return a PyADTPulseAsync stand-in that logs in without MFA."""
    pulse = MagicMock()
    pulse.async_login = AsyncMock()
    pulse.async_logout = AsyncMock()
    pulse.async_request_mfa_code = AsyncMock(return_value="EMAIL")
    pulse.async_complete_mfa_login = AsyncMock()
    pulse.site.id = site_id
    return pulse


@pytest.fixture
def flow_pulse():
    """Patch the client the config flow creates, and return it."""
    pulse = make_pulse()
    with patch(
        "custom_components.adtpulse.config_flow.PyADTPulseAsync", return_value=pulse
    ) as cls:
        pulse.cls = cls
        yield pulse
