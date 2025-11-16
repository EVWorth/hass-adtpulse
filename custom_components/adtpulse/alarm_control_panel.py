"""Support for ADT Pulse alarm control panels."""

import logging
from datetime import datetime
from collections.abc import Coroutine

from pyadtpulse.site import ADTPulseSite
from homeassistant.core import HomeAssistant, callback
from homeassistant.util.dt import as_local
from pyadtpulse.alarm_panel import (
    ADT_ALARM_OFF,
    ADT_ALARM_AWAY,
    ADT_ALARM_HOME,
    ADT_ALARM_NIGHT,
    ADT_ALARM_ARMING,
    ADT_ALARM_DISARMING,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import (
    AddEntitiesCallback,
    async_get_current_platform,
)
from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelState,
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
)

from .const import ADTPULSE_DOMAIN
from .utils import (
    get_alarm_unique_id,
    migrate_entity_name,
    system_can_be_armed,
    get_gateway_unique_id,
)
from .base_entity import ADTPulseEntity
from .coordinator import ALARM_CONTEXT, ADTPulseDataUpdateCoordinator

logger = logging.getLogger(__name__)

ALARM_MAP = {
    ADT_ALARM_ARMING: AlarmControlPanelState.ARMING,
    ADT_ALARM_AWAY: AlarmControlPanelState.ARMED_AWAY,
    ADT_ALARM_DISARMING: AlarmControlPanelState.DISARMING,
    ADT_ALARM_HOME: AlarmControlPanelState.ARMED_HOME,
    ADT_ALARM_OFF: AlarmControlPanelState.DISARMED,
    ADT_ALARM_NIGHT: AlarmControlPanelState.ARMED_NIGHT,
}


FORCE_ARM = "force arm"
ARM_ERROR_MESSAGE = (
    f"Pulse system cannot be armed due to opened/tripped zone - use {FORCE_ARM}"
)


async def async_setup_entry(
    hass: HomeAssistant,
    config: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up an alarm control panel for ADT Pulse."""
    coordinator: ADTPulseDataUpdateCoordinator = hass.data[ADTPULSE_DOMAIN][
        config.entry_id
    ]
    if not coordinator:
        logger.error("ADT Pulse service not initialized, cannot setup alarm platform")
        return
    site = coordinator.adtpulse.site
    migrate_entity_name(
        hass,
        site,
        "alarm_control_panel",
        get_alarm_unique_id(site),
    )
    alarm_devices = [ADTPulseAlarm(coordinator, site)]

    async_add_entities(alarm_devices)
    platform = async_get_current_platform()
    platform.async_register_entity_service(
        "force_stay", {}, "async_alarm_arm_force_stay"
    )
    platform.async_register_entity_service(
        "force_away", {}, "async_alarm_arm_custom_bypass"
    )


class ADTPulseAlarm(ADTPulseEntity, AlarmControlPanelEntity):
    """An alarm_control_panel implementation for ADT Pulse."""

    def __init__(
        self,
        coordinator: ADTPulseDataUpdateCoordinator,
        site: ADTPulseSite,
    ):
        """Initialize the alarm control panel."""
        logger.debug("%s: adding alarm control panel for %s", ADTPULSE_DOMAIN, site.id)
        self._name = f"ADT Alarm Panel - Site {site.id}"
        self._assumed_state: AlarmControlPanelState | None = None
        super().__init__(coordinator, ALARM_CONTEXT)

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        """Return the current alarm state."""
        if self._assumed_state is not None:
            return self._assumed_state
        return ALARM_MAP.get(self._alarm.status)

    @property
    def assumed_state(self) -> bool:
        """Return if the alarm is in an assumed state."""
        return self._assumed_state is not None

    @property
    def supported_features(self) -> AlarmControlPanelEntityFeature:
        """Return the list of supported features."""
        return (
            AlarmControlPanelEntityFeature.ARM_AWAY
            | AlarmControlPanelEntityFeature.ARM_CUSTOM_BYPASS
            | AlarmControlPanelEntityFeature.ARM_HOME
            | AlarmControlPanelEntityFeature.ARM_NIGHT
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info.

        We set the identifiers to the site id since it is unique across all sites
        and the zones can be identified by site id and zone name
        """
        return DeviceInfo(
            identifiers={(ADTPULSE_DOMAIN, self._site.id)},
            manufacturer=self._alarm.manufacturer,
            model=self._alarm.model,
            via_device=(ADTPULSE_DOMAIN, get_gateway_unique_id(self._site)),
            name=self._name,
        )

    async def _perform_alarm_action(
        self,
        arm_disarm_func: Coroutine[bool | None, None, bool],
        action: AlarmControlPanelState,
    ) -> None:
        result = True
        logger.debug("%s: Setting Alarm to %s", ADTPULSE_DOMAIN, action)
        if action != AlarmControlPanelState.DISARMED:
            await self._check_if_system_armable(action)
        if self.alarm_state == action:
            logger.warning("Attempting to set alarm to same state, ignoring")
            return
        if not self._gateway.is_online:
            self._assumed_state = action
        elif action == AlarmControlPanelState.DISARMED:
            self._assumed_state = AlarmControlPanelState.DISARMING
        else:
            self._assumed_state = AlarmControlPanelState.ARMING
        self.async_write_ha_state()
        result = await arm_disarm_func
        if not result:
            logger.warning("Could not %s ADT Pulse alarm", action)
        self._assumed_state = None
        self.async_write_ha_state()
        if not result:
            raise HomeAssistantError(f"Could not set alarm status to {action}")

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        """Send disarm command."""
        await self._perform_alarm_action(
            arm_disarm_func=self._site.async_disarm(),
            action=AlarmControlPanelState.DISARMED,
        )

    async def _check_if_system_armable(self, new_state: str) -> None:
        """Checks if we can arm the system, raises exceptions if not."""
        if self.alarm_state != AlarmControlPanelState.DISARMED:
            raise HomeAssistantError(
                f"Cannot set alarm to {new_state} "
                f"because currently set to {self.alarm_state}"
            )
        if not new_state == FORCE_ARM and not system_can_be_armed(self._site):
            raise HomeAssistantError(ARM_ERROR_MESSAGE)

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        """Send arm home command."""
        await self._perform_alarm_action(
            arm_disarm_func=self._site.async_arm_home(),
            action=AlarmControlPanelState.ARMED_HOME,
        )

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        """Send arm away command."""
        await self._perform_alarm_action(
            arm_disarm_func=self._site.async_arm_away(),
            action=AlarmControlPanelState.ARMED_AWAY,
        )

    # Pulse can arm away or home with bypass
    async def async_alarm_arm_custom_bypass(self, code: str | None = None) -> None:
        """Send force arm command."""
        await self._perform_alarm_action(
            arm_disarm_func=self._site.async_arm_away(force_arm=True),
            action=AlarmControlPanelState.ARMED_CUSTOM_BYPASS,
        )

    async def async_alarm_arm_night(self) -> None:
        """Send arm night command."""
        await self._perform_alarm_action(
            arm_disarm_func=self._site.async_arm_night(),
            action=AlarmControlPanelState.ARMED_NIGHT,
        )

    async def async_alarm_arm_force_stay(self) -> None:
        """Send force arm stay command.

        This type of arming isn't implemented in HA, but we put it in anyway for
        use as a service call.
        """
        await self._perform_alarm_action(
            arm_disarm_func=self._site.async_arm_home(force_arm=True),
            action=AlarmControlPanelState.ARMED_HOME,
        )

    @property
    def extra_state_attributes(self) -> dict:
        """Return the state attributes."""
        return {
            "last_update_time": as_local(
                datetime.fromtimestamp(self._alarm.last_update)
            ),
            "alarm_state": self._alarm.status,
        }

    @property
    def unique_id(self) -> str:
        """Return HA unique id.

        Returns:
            str: the unique id
        """
        return get_alarm_unique_id(self._site)

    @property
    def code_format(self) -> None:
        """Return code format.

        Returns:
            None (not implmented)
        """
        return None

    @property
    def available(self) -> bool:
        """Alarm panel is always available even if gateway isn't."""
        return True

    @property
    def code_arm_required(self) -> bool:
        """Whether the code is required for arm actions."""
        return False

    @callback
    def _handle_coordinator_update(self) -> None:
        logger.debug(
            "Updating Pulse alarm to %s for site %s",
            ALARM_MAP[self._site.alarm_control_panel.status],
            self._site.id,
        )
        self.async_write_ha_state()
