"""Tests for the ADT Pulse config flow."""

import json
from base64 import b64decode
from unittest.mock import patch

import pytest
from pyadtpulse.const import DEFAULT_API_HOST
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from pyadtpulse.exceptions import (
    PulseMFARequiredError,
    PulseAuthenticationError,
    PulseServerConnectionError,
)
from pyadtpulse.pulse_backoff import PulseBackoff
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.adtpulse.const import (
    CONF_HOSTNAME,
    ADTPULSE_DOMAIN,
    CONF_FINGERPRINT,
)


@pytest.fixture(autouse=True)
def mock_setup_entry():
    """Don't set up the entry the flow creates; that would reach the network."""
    with patch("custom_components.adtpulse.async_setup_entry", return_value=True):
        yield


USER_INPUT = {
    CONF_USERNAME: "user@example.com",
    CONF_PASSWORD: "test-password",
    CONF_HOSTNAME: DEFAULT_API_HOST,
}


async def _start(hass):
    result = await hass.config_entries.flow.async_init(
        ADTPULSE_DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM
    assert CONF_FINGERPRINT not in result["data_schema"].schema
    return result


async def test_login_without_mfa_creates_entry(hass, flow_pulse):
    """A trusted login creates the entry with a generated fingerprint."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "ADT: Site 12345"
    fingerprint = result["data"][CONF_FINGERPRINT]
    decoded = json.loads(b64decode(fingerprint))
    assert decoded["fingerprint"]["uaBrowser"]["name"] == "Chrome"
    flow_pulse.async_logout.assert_awaited()


async def test_mfa_code_step(hass, flow_pulse):
    """MFA asks for the code; a wrong code retries, the right one finishes."""
    flow_pulse.async_login.side_effect = PulseMFARequiredError(challenge=object())
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mfa"
    flow_pulse.async_request_mfa_code.assert_awaited_once()

    flow_pulse.async_complete_mfa_login.side_effect = PulseAuthenticationError()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "000000"}
    )
    assert result["step_id"] == "mfa"
    assert result["errors"] == {"base": "invalid_mfa_code"}

    flow_pulse.async_complete_mfa_login.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": " 123456 "}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    flow_pulse.async_complete_mfa_login.assert_awaited_with(
        "123456", device_name="Home Assistant"
    )


async def test_mfa_without_challenge_shows_error(hass, flow_pulse):
    """An MFA page the library can't answer reports mfa_required."""
    flow_pulse.async_login.side_effect = PulseMFARequiredError()
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["errors"] == {"base": "mfa_required"}
    flow_pulse.async_logout.assert_awaited()


async def test_code_not_sent(hass, flow_pulse):
    """A failed code request returns to the credentials form."""
    flow_pulse.async_login.side_effect = PulseMFARequiredError(challenge=object())
    flow_pulse.async_request_mfa_code.side_effect = PulseAuthenticationError()
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "mfa_code_not_sent"}


async def test_connection_error_shows_form(hass, flow_pulse):
    """A network error is a form error, not an exception out of the flow."""
    flow_pulse.async_login.side_effect = PulseServerConnectionError(
        "down", PulseBackoff("test", 1)
    )
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_bad_credentials(hass, flow_pulse):
    """Rejected credentials show invalid_auth."""
    flow_pulse.async_login.side_effect = PulseAuthenticationError()
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["errors"] == {"base": "invalid_auth"}


async def test_reauth_keeps_fingerprint(hass, flow_pulse):
    """Re-authentication logs in with the entry's existing fingerprint."""
    entry = MockConfigEntry(
        domain=ADTPULSE_DOMAIN,
        data={**USER_INPUT, CONF_FINGERPRINT: "trusted-fingerprint"},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_PASSWORD: "new-password"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_FINGERPRINT] == "trusted-fingerprint"
    assert entry.data[CONF_PASSWORD] == "new-password"
    assert flow_pulse.cls.call_args.args[2] == "trusted-fingerprint"
