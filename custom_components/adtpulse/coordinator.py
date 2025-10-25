"""ADT Pulse Update Coordinator."""

import logging
from typing import Any
from asyncio import Task, CancelledError
from collections.abc import Callable

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.util.dt import utcnow, as_local, utc_from_timestamp
from pyadtpulse.exceptions import (
    PulseLoginException,
    PulseExceptionWithRetry,
    PulseExceptionWithBackoff,
)
from homeassistant.exceptions import ConfigEntryNotReady
from pyadtpulse.pyadtpulse_async import PyADTPulseAsync
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import ADTPULSE_DOMAIN

logger = logging.getLogger(__name__)

ALARM_CONTEXT = "Alarm"
ZONE_CONTEXT_PREFIX = "Zone "
ZONE_TROUBLE_PREFIX = " Trouble"
CONNECTION_STATUS_CONTEXT = "ConnectionStatus"
NEXT_REFRESH_CONTEXT = "NextRefresh"


class ADTPulseDataUpdateCoordinator(DataUpdateCoordinator):
    """Update Coordinator for ADT Pulse entities."""

    def __init__(self, hass: HomeAssistant, pulse_service: PyADTPulseAsync):
        """Initialize Pulse data update coordinator.

        Args:
            hass (HomeAssistant): hass object
            pulse_service (PyADTPulseAsync): ADT Pulse API service object used to
                fetch updates and manage session state.
        """
        logger.debug("%s: creating update coordinator", ADTPULSE_DOMAIN)
        self._adt_pulse = pulse_service
        self._update_task: Task | None = None
        super().__init__(
            hass,
            logger,
            name=ADTPULSE_DOMAIN,
        )
        self._listener_dictionary: dict[str, CALLBACK_TYPE] = {}

    @property
    def adtpulse(self) -> PyADTPulseAsync:
        """Return the ADT Pulse service object."""
        return self._adt_pulse

    @callback
    def async_add_listener(
        self, update_callback: CALLBACK_TYPE, context: Any = None
    ) -> Callable[[], None]:
        """Listen for data updates."""
        self._listener_dictionary[context] = update_callback
        return super().async_add_listener(update_callback, context)

    @callback
    def async_update_listeners(self) -> None:
        """Update listeners based update returned data."""
        start_time = utcnow()
        if not self.data:
            super().async_update_listeners()
            logger.debug(
                "%s: async_update_listeners took %s",
                ADTPULSE_DOMAIN,
                utcnow() - start_time,
            )
            return
        data_to_update: tuple[bool, set[int]] = self.data
        if data_to_update[0]:
            self._listener_dictionary[ALARM_CONTEXT]()
        for zones in data_to_update[1]:
            self._listener_dictionary[ZONE_CONTEXT_PREFIX + str(zones)]()
            self._listener_dictionary[
                ZONE_CONTEXT_PREFIX + str(zones) + ZONE_TROUBLE_PREFIX
            ]()
        for i in CONNECTION_STATUS_CONTEXT, NEXT_REFRESH_CONTEXT:
            self._listener_dictionary[i]()
        logger.debug(
            "%s: partial async_update_listeners took %s",
            ADTPULSE_DOMAIN,
            utcnow() - start_time,
        )

    async def start(self) -> None:
        """Start ADT Pulse update coordinator.

        This doesn't really need to be async, but it is to yield the event loop.
        """
        if not self._update_task:
            ce = self.config_entry
            if ce:
                self._update_task = ce.async_create_background_task(
                    self.hass, self._async_update_data(), "ADT Pulse Data Update"
                )
            else:
                raise ConfigEntryNotReady

    async def stop(self):
        """Stop ADT Pulse update coordinator."""
        if self._update_task:
            if not self._update_task.cancelled():
                self._update_task.cancel()
            await self._update_task
            self._update_task = None

    async def _async_update_data(self) -> None:
        """Fetch data from ADT Pulse."""
        while not self._shutdown_requested and not self.hass.is_stopping:
            data = None
            logger.debug("%s: coordinator waiting for updates", ADTPULSE_DOMAIN)
            update_exception: Exception | None = None
            try:
                data = await self._adt_pulse.wait_for_update()
            except PulseLoginException as ex:
                logger.error(
                    "%s: ADT Pulse login failed during coordinator update: %s",
                    ADTPULSE_DOMAIN,
                    ex,
                )
                if self.config_entry:
                    self.config_entry.async_start_reauth(self.hass)
                return
            except PulseExceptionWithRetry as ex:
                if ex.retry_time:
                    logger.debug(
                        "%s: coordinator received retryable exception will retry at %s",
                        ADTPULSE_DOMAIN,
                        as_local(utc_from_timestamp(ex.retry_time)),
                    )
                update_exception = ex
            except PulseExceptionWithBackoff as ex:
                update_exception = ex
                logger.debug(
                    "%s: coordinator received backoff exception, backing off for %s seconds",  # noqa: E501
                    ADTPULSE_DOMAIN,
                    ex.backoff.get_current_backoff_interval(),
                )
            except CancelledError:
                logger.debug("%s: coordinator received cancellation", ADTPULSE_DOMAIN)
                return
            except Exception as ex:
                logger.error(
                    "%s: coordinator received unknown exception %s, exiting...",
                    ADTPULSE_DOMAIN,
                    ex,
                )
                raise
            finally:
                if update_exception:
                    self.async_set_update_error(update_exception)
                    # async_set_update_error will only notify listeners on first error
                    # it also doesn't reset data
                    self.data = None
                    if not self.last_update_success:
                        self.async_update_listeners()
                else:
                    self.last_exception = None
                    self.async_set_updated_data(data)

            logger.debug(
                "%s: coordinator received update notification", ADTPULSE_DOMAIN
            )
