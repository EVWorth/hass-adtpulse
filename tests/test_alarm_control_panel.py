"""Tests for the ADT Pulse alarm control panel."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.util.dt import utcnow
from pyadtpulse.alarm_panel import (
    ADT_ALARM_OFF,
    ADT_ALARM_AWAY,
    ADT_ALARM_HOME,
    ADT_ALARM_ARMING,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.components.alarm_control_panel import AlarmControlPanelState
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.adtpulse.alarm_control_panel import (
    ARM_ERROR_MESSAGE,
    ACTION_CONFIRM_TIMEOUT,
    ALARM_ACTION_FAILED_EVENT,
    ADTPulseAlarm,
)


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
async def test_force_arm_with_open_zone(hass, method, site_call):
    """Force arming skips the open-zone check and arms with force_arm."""
    entity, site = _alarm(zone_open=True)
    _capture_failures(hass, entity)
    await getattr(entity, method)()
    getattr(site, site_call).assert_awaited_once_with(force_arm=True)
    entity._stop_confirm()


async def test_normal_arm_with_open_zone_is_refused(hass):
    """A normal arm with an open zone is refused without calling ADT."""
    entity, site = _alarm(zone_open=True)
    _capture_failures(hass, entity)
    with pytest.raises(HomeAssistantError, match="opened/tripped zone"):
        await entity.async_alarm_arm_home()
    site.async_arm_home.assert_not_called()


async def test_arm_home_when_closed(hass):
    """A normal arm with every zone closed arms home."""
    entity, site = _alarm(zone_open=False)
    _capture_failures(hass, entity)
    await entity.async_alarm_arm_home()
    site.async_arm_home.assert_awaited_once_with()
    entity._stop_confirm()


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


def _capture_failures(hass, entity: ADTPulseAlarm) -> list:
    """Attach entity to hass and return the failure events it fires."""
    entity.hass = hass
    entity.entity_id = "alarm_control_panel.adt"
    return async_capture_events(hass, ALARM_ACTION_FAILED_EVENT)


async def test_refused_arm_fires_failure_event(hass):
    """A refused arm fires an event naming the open zone."""
    entity, site = _alarm(zone_open=True)
    site.zones_as_dict[1].name = "Front Door"
    events = _capture_failures(hass, entity)
    with pytest.raises(HomeAssistantError):
        await entity.async_alarm_arm_away()
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data == {
        "entity_id": "alarm_control_panel.adt",
        "action": "armed_away",
        "force": False,
        "message": ARM_ERROR_MESSAGE,
        "blocking_zones": ["Front Door open"],
    }


async def test_failed_command_fires_failure_event(hass):
    """An arm command ADT rejects fires an event with the error."""
    entity, site = _alarm(zone_open=False)
    site.async_arm_home.return_value = False
    events = _capture_failures(hass, entity)
    with pytest.raises(HomeAssistantError, match="Could not set alarm status"):
        await entity.async_alarm_arm_home()
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data["action"] == "armed_home"
    assert events[0].data["blocking_zones"] == []


async def test_connection_error_fires_failure_event(hass):
    """An exception from pyadtpulse fires the event and still propagates."""
    entity, site = _alarm(zone_open=False)
    site.async_arm_home.side_effect = TimeoutError
    events = _capture_failures(hass, entity)
    with pytest.raises(TimeoutError):
        await entity.async_alarm_arm_home()
    await hass.async_block_till_done()
    assert events[0].data["message"] == "TimeoutError"


def _report(entity: ADTPulseAlarm, site: MagicMock, status: str) -> None:
    """Have Pulse report status for the alarm."""
    site.alarm_control_panel.status = status
    entity._handle_coordinator_update()


async def _time_passes(hass) -> None:
    """Move time past the confirmation timeout."""
    async_fire_time_changed(
        hass, utcnow() + timedelta(seconds=ACTION_CONFIRM_TIMEOUT + 1)
    )
    await hass.async_block_till_done()


async def test_successful_arm_fires_no_event(hass):
    """An arm that reaches the requested state fires no failure event."""
    entity, site = _alarm(zone_open=False)
    events = _capture_failures(hass, entity)
    await entity.async_alarm_arm_home()
    _report(entity, site, ADT_ALARM_HOME)
    await _time_passes(hass)
    assert events == []


async def test_accepted_command_never_carried_out_fires_event(hass):
    """A command Pulse accepts but the panel never carries out fires the event."""
    entity, site = _alarm(zone_open=True)
    events = _capture_failures(hass, entity)
    await entity.async_alarm_arm_force_stay()
    _report(entity, site, ADT_ALARM_ARMING)
    await hass.async_block_till_done()
    assert events == []
    await _time_passes(hass)
    assert len(events) == 1
    assert events[0].data["action"] == "armed_home"
    assert events[0].data["force"] is True
    assert "accepted the command" in events[0].data["message"]


async def test_disarm_after_arm_completes_is_not_a_failure(hass):
    """Disarming at the keypad after the arm completed fires no event."""
    entity, site = _alarm(zone_open=False)
    events = _capture_failures(hass, entity)
    await entity.async_alarm_arm_home()
    _report(entity, site, ADT_ALARM_HOME)
    _report(entity, site, ADT_ALARM_OFF)
    await _time_passes(hass)
    assert events == []


async def test_custom_bypass_is_confirmed_by_armed_away(hass):
    """Arm custom bypass arms away, so armed away confirms it."""
    entity, site = _alarm(zone_open=True)
    events = _capture_failures(hass, entity)
    await entity.async_alarm_arm_custom_bypass()
    site.alarm_control_panel.status = ADT_ALARM_AWAY
    await _time_passes(hass)
    assert events == []


async def test_new_action_replaces_pending_check(hass):
    """A second action cancels the first action's check."""
    entity, site = _alarm(zone_open=False)
    events = _capture_failures(hass, entity)
    site.async_disarm = AsyncMock(return_value=True)
    await entity.async_alarm_arm_home()
    _report(entity, site, ADT_ALARM_ARMING)
    await entity.async_alarm_disarm()
    _report(entity, site, ADT_ALARM_OFF)
    await _time_passes(hass)
    assert events == []
