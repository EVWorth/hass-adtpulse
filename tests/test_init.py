"""Tests for setting up an ADT Pulse entry."""

from unittest.mock import patch

import pytest
from pyadtpulse.const import DEFAULT_API_HOST
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from pyadtpulse.exceptions import (
    PulseMFARequiredError,
    PulseClientConnectionError,
    PulseServerConnectionError,
)
from pyadtpulse.pulse_backoff import PulseBackoff
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.adtpulse.const import (
    CONF_HOSTNAME,
    ADTPULSE_DOMAIN,
    CONF_FINGERPRINT,
)

from .conftest import make_pulse


def _entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=ADTPULSE_DOMAIN,
        data={
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "test-password",
            CONF_HOSTNAME: DEFAULT_API_HOST,
            CONF_FINGERPRINT: "fingerprint",
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.parametrize(
    "error",
    [
        PulseServerConnectionError("DNS failure", PulseBackoff("test", 1)),
        PulseClientConnectionError("network down", PulseBackoff("test", 1)),
    ],
)
async def test_network_error_retries_setup(hass, error):
    """A network error during login retries setup instead of failing it."""
    pulse = make_pulse()
    pulse.async_login.side_effect = error
    entry = _entry(hass)
    with patch("custom_components.adtpulse.PyADTPulseAsync", return_value=pulse):
        await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_mfa_at_startup_starts_reauth(hass):
    """An entry that hits MFA at startup asks the user to re-authenticate."""
    pulse = make_pulse()
    pulse.async_login.side_effect = PulseMFARequiredError()
    entry = _entry(hass)
    with patch("custom_components.adtpulse.PyADTPulseAsync", return_value=pulse):
        await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == ["reauth"]
