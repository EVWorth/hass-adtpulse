"""Tests for the ADT Pulse update coordinator."""

import time
from asyncio import CancelledError
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pyadtpulse.exceptions import (
    PulseMFARequiredError,
    PulseNotLoggedInError,
    PulseAccountLockedError,
    PulseAuthenticationError,
    PulseServerConnectionError,
)
from pyadtpulse.pulse_backoff import PulseBackoff
from pyadtpulse.pyadtpulse_async import PulseUpdate

from custom_components.adtpulse.coordinator import (
    ALARM_CONTEXT,
    NEXT_REFRESH_CONTEXT,
    CONNECTION_STATUS_CONTEXT,
    ADTPulseDataUpdateCoordinator,
)


async def test_failing_listener_does_not_stop_the_others(hass):
    """One listener raising doesn't stop the rest from being updated."""
    coordinator = ADTPulseDataUpdateCoordinator(hass, MagicMock())
    calls = []

    def boom():
        raise KeyError("unknown")

    coordinator._listener_dictionary = {
        ALARM_CONTEXT: boom,
        CONNECTION_STATUS_CONTEXT: lambda: calls.append("status"),
        NEXT_REFRESH_CONTEXT: lambda: calls.append("refresh"),
    }
    # zone 7 has no listener registered
    coordinator.data = PulseUpdate(alarm_changed=True, updated_zones={7})
    coordinator.async_update_listeners()
    assert calls == ["status", "refresh"]


def _run_coordinator(hass, waits, logins=()):
    """Return a coordinator whose client yields `waits`, then is cancelled.

    `waits` are what successive async_wait_for_update() calls return or raise, and
    `logins` what successive async_login() calls raise (None for success).
    """
    pulse = MagicMock()
    pulse.async_wait_for_update = AsyncMock(side_effect=[*waits, CancelledError()])
    pulse.async_login = AsyncMock(side_effect=list(logins) or None)
    coordinator = ADTPulseDataUpdateCoordinator(hass, pulse)
    coordinator.config_entry = MagicMock()
    coordinator._relogin_backoff = MagicMock(async_wait_for_backoff=AsyncMock())
    return coordinator, pulse


def _backoff() -> PulseBackoff:
    return PulseBackoff("test", 1)


async def test_dropped_session_logs_back_in_without_reauth(hass):
    """A session Pulse ended is logged back in, without a reauth prompt."""
    update = PulseUpdate(alarm_changed=False, updated_zones=set())
    coordinator, pulse = _run_coordinator(hass, [PulseNotLoggedInError(), update])
    await coordinator._async_update_data()
    pulse.async_login.assert_awaited_once()
    coordinator.config_entry.async_start_reauth.assert_not_called()
    assert pulse.async_wait_for_update.await_count == 3
    coordinator._relogin_backoff.reset_backoff.assert_called_once()


async def test_failed_relogin_retries_with_backoff(hass):
    """A relogin that fails on the network is retried, waiting longer each time."""
    coordinator, pulse = _run_coordinator(
        hass,
        [PulseNotLoggedInError(), PulseNotLoggedInError()],
        logins=[PulseServerConnectionError("DNS failure", _backoff()), None],
    )
    await coordinator._async_update_data()
    assert pulse.async_login.await_count == 2
    coordinator._relogin_backoff.increment_backoff.assert_called_once()
    coordinator.config_entry.async_start_reauth.assert_not_called()


async def test_account_lock_waits_instead_of_reauth(hass):
    """A temporary account lock waits for its retry time, not for the user."""
    locked = PulseAccountLockedError(_backoff(), time.time() + 60)
    coordinator, pulse = _run_coordinator(
        hass, [locked, PulseNotLoggedInError()], logins=[locked, None]
    )
    with patch.object(PulseBackoff, "async_wait_for_backoff", AsyncMock()) as wait:
        await coordinator._async_update_data()
    wait.assert_awaited()
    assert pulse.async_login.await_count == 1
    coordinator.config_entry.async_start_reauth.assert_not_called()


@pytest.mark.parametrize("error", [PulseAuthenticationError(), PulseMFARequiredError()])
async def test_rejected_login_starts_reauth(hass, error):
    """Rejected credentials or a verification code request ask the user."""
    coordinator, pulse = _run_coordinator(hass, [error])
    await coordinator._async_update_data()
    coordinator.config_entry.async_start_reauth.assert_called_once()
    pulse.async_login.assert_not_called()
    assert pulse.async_wait_for_update.await_count == 1


@pytest.mark.parametrize("error", [PulseAuthenticationError(), PulseMFARequiredError()])
async def test_rejected_relogin_starts_reauth(hass, error):
    """A relogin that Pulse rejects stops the loop and asks the user."""
    coordinator, pulse = _run_coordinator(
        hass, [PulseNotLoggedInError()], logins=[error]
    )
    await coordinator._async_update_data()
    coordinator.config_entry.async_start_reauth.assert_called_once()
    assert pulse.async_wait_for_update.await_count == 1


async def test_stop_during_relogin_ends_quietly(hass):
    """Stopping the coordinator while it waits to log back in ends the loop."""
    coordinator, pulse = _run_coordinator(hass, [PulseNotLoggedInError()])
    coordinator._relogin_backoff.async_wait_for_backoff.side_effect = CancelledError()
    await coordinator._async_update_data()
    pulse.async_login.assert_not_called()
    assert pulse.async_wait_for_update.await_count == 1
