"""Tests for the ADT Pulse update coordinator."""

from unittest.mock import MagicMock

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
