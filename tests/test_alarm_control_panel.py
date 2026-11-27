"""Tests for the ADT Pulse alarm control panel."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from pyadtpulse.alarm_panel import ADT_ALARM_OFF, ADT_ALARM_AWAY
from homeassistant.exceptions import HomeAssistantError
from homeassistant.components.alarm_control_panel import AlarmControlPanelState

from custom_components.adtpulse.alarm_control_panel import ADTPulseAlarm


def _alarm(zone_open: bool) -> tuple[ADTPulseAlarm, MagicMock]:
    """Return a disarmed alarm entity whose site has one zone."""
    site = MagicMock()
    site.id = "12345"
    site.alarm_control_panel.status = ADT_ALARM_OFF
    site.gateway.is_online = True
    zone = MagicMock(state="Open" if zone_open else "OK", status="Online")
    site.zones_as_dict = {1: zone}
    site.zones = [zone]
    for name in ("async_arm_home", "async_arm_away", "async_arm_night"):
        setattr(site, name, AsyncMock(return_value=True))
    coordinator = MagicMock()
    coordinator.adtpulse.site = site
    entity = ADTPulseAlarm(coordinator, site)
    entity.async_write_ha_state = MagicMock()
    return entity, site


@pytest.mark.parametrize(
    ("method", "site_call"),
    [
        ("async_alarm_arm_force_stay", "async_arm_home"),
        ("async_alarm_arm_custom_bypass", "async_arm_away"),
    ],
)
async def test_force_arm_with_open_zone(method, site_call):
    """Force arming skips the open-zone check and arms with force_arm."""
    entity, site = _alarm(zone_open=True)
    await getattr(entity, method)()
    getattr(site, site_call).assert_awaited_once_with(force_arm=True)


async def test_normal_arm_with_open_zone_is_refused():
    """A normal arm with an open zone is refused without calling ADT."""
    entity, site = _alarm(zone_open=True)
    with pytest.raises(HomeAssistantError, match="opened/tripped zone"):
        await entity.async_alarm_arm_home()
    site.async_arm_home.assert_not_called()


async def test_arm_home_when_closed():
    """A normal arm with every zone closed arms home."""
    entity, site = _alarm(zone_open=False)
    await entity.async_alarm_arm_home()
    site.async_arm_home.assert_awaited_once_with()


async def test_arm_that_raises_clears_assumed_state():
    """An arm command that raises leaves the real state showing, not arming."""
    entity, site = _alarm(zone_open=False)
    site.async_arm_home.side_effect = RuntimeError("connection lost")
    with pytest.raises(RuntimeError):
        await entity.async_alarm_arm_home()
    assert entity.alarm_state is AlarmControlPanelState.DISARMED


async def test_disarm_that_raises_clears_assumed_state():
    """A disarm command that raises leaves the real state showing, not disarming."""
    entity, site = _alarm(zone_open=False)
    site.alarm_control_panel.status = ADT_ALARM_AWAY
    site.async_disarm = AsyncMock(side_effect=RuntimeError("connection lost"))
    with pytest.raises(RuntimeError):
        await entity.async_alarm_disarm()
    assert entity.alarm_state is AlarmControlPanelState.ARMED_AWAY


async def test_unknown_status_update_does_not_raise():
    """An "unknown" alarm status updates as no state instead of raising."""
    entity, site = _alarm(zone_open=False)
    site.alarm_control_panel.status = "unknown"
    entity._handle_coordinator_update()
    assert entity.alarm_state is None
    entity.async_write_ha_state.assert_called_once()


def test_states_are_mapped():
    """The disarmed status maps to the disarmed state."""
    entity, _ = _alarm(zone_open=False)
    assert entity.alarm_state is AlarmControlPanelState.DISARMED
