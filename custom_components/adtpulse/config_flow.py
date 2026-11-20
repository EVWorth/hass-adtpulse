"""HASS ADT Pulse Config Flow."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
import homeassistant.helpers.config_validation as cv
from pyadtpulse.site import ADTPulseSite
from pyadtpulse.const import (
    API_HOST_CA,
    DEFAULT_API_HOST,
    ADT_MIN_RELOGIN_INTERVAL,
    ADT_DEFAULT_POLL_INTERVAL,
    ADT_MAX_KEEPALIVE_INTERVAL,
    ADT_DEFAULT_RELOGIN_INTERVAL,
    ADT_DEFAULT_KEEPALIVE_INTERVAL,
)
from homeassistant.core import callback
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, CONF_SCAN_INTERVAL
from pyadtpulse.exceptions import (
    PulseConnectionError,
    PulseMFARequiredError,
    PulseAccountLockedError,
    PulseAuthenticationError,
    PulseGatewayOfflineError,
    PulseServiceTemporarilyUnavailableError,
)
from homeassistant.exceptions import ConfigEntryNotReady
from pyadtpulse.pyadtpulse_async import PyADTPulseAsync
from homeassistant.config_entries import (
    CONN_CLASS_CLOUD_PUSH,
    ConfigFlow,
    ConfigEntry,
    OptionsFlowWithConfigEntry,
)
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_HOSTNAME,
    ADTPULSE_DOMAIN,
    CONF_FINGERPRINT,
    CONF_RELOGIN_INTERVAL,
    CONF_KEEPALIVE_INTERVAL,
)

CONF_MFA_CODE = "code"
# the name the fingerprint is trusted under in the ADT Pulse account
MFA_DEVICE_NAME = "Home Assistant"

logger = logging.getLogger(__name__)


async def _async_logout_quietly(adtpulse: PyADTPulseAsync) -> None:
    """Log out a session the flow is abandoning; a failure must not block the form."""
    try:
        await adtpulse.async_logout()
    except Exception:  # pylint: disable=broad-except
        logger.debug("Could not log out an abandoned ADT Pulse session", exc_info=True)


class PulseConfigFlow(ConfigFlow, domain=ADTPULSE_DOMAIN):  # type: ignore
    """Handle a config flow for ADT Pulse."""

    @staticmethod
    async def validate_input(data: dict[str, str]) -> dict[str, str]:
        """Validate form input.

        Args:
            hass (core.HomeAssistant): hass object
            data (Dict): voluptuous Schema

        Raises:
            CannotConnect: Cannot connect to ADT Pulse site
            InvalidAuth: login failed

        Returns:
            Dict[str, str | bool]: "title" : username used to validate
                                "login result": True if login succeeded
        """
        adtpulse = PyADTPulseAsync(
            data[CONF_USERNAME],
            data[CONF_PASSWORD],
            data[CONF_FINGERPRINT],
            service_host=data[CONF_HOSTNAME],
        )
        try:
            await adtpulse.async_login()
        except PulseMFARequiredError as ex:
            # keep the session: the login can be finished with a code
            ex.adtpulse = adtpulse  # type: ignore[attr-defined]
            raise
        except Exception:
            logger.error("ERROR VALIDATING INPUT")
            await adtpulse.async_logout()
            raise
        return await PulseConfigFlow._finish_validation(adtpulse)

    @staticmethod
    async def _finish_validation(adtpulse: PyADTPulseAsync) -> dict[str, str]:
        """Read the site from a logged-in session, then log it out."""
        try:
            site: ADTPulseSite = adtpulse.site
            site_id = site.id
        finally:
            await adtpulse.async_logout()
        return {"title": f"ADT: Site {site_id}"}

    @staticmethod
    def _get_data_schema(orig_input: dict[str, Any] | None) -> vol.Schema:
        if orig_input is None:
            orig_input = {}
        DATA_SCHEMA = vol.Schema(
            {
                vol.Required(
                    CONF_USERNAME, default=orig_input.get(CONF_USERNAME, "")
                ): cv.string,
                vol.Required(
                    CONF_PASSWORD, default=orig_input.get(CONF_PASSWORD, "")
                ): cv.string,
                vol.Required(
                    CONF_FINGERPRINT, default=orig_input.get(CONF_FINGERPRINT, "")
                ): cv.string,
                vol.Required(
                    CONF_HOSTNAME, default=orig_input.get(CONF_HOSTNAME, "")
                ): vol.In([DEFAULT_API_HOST, API_HOST_CA]),
            }
        )
        return DATA_SCHEMA

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> PulseOptionsFlowHandler:
        """Create the options flow."""
        return PulseOptionsFlowHandler(config_entry)

    VERSION = 1
    CONNECTION_CLASS = CONN_CLASS_CLOUD_PUSH

    _reauth_entry: ConfigEntry | None = None
    _mfa_pulse: PyADTPulseAsync | None = None
    _mfa_input: dict[str, Any] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step.

        Args:
            user_input (Optional[Dict[str, Any]], optional): user input.
                    Defaults to None.

        Returns:
            FlowResult: the flow result
        """
        # This goes through the steps to take the user through the setup process.
        # Using this it is possible to update the UI and prompt for additional
        # information. This example provides a single form (built from `DATA_SCHEMA`),
        # and when that has some validated input, it calls `async_create_entry` to
        # actually create the HA config entry. Note the "title" value is returned by
        # `validate_input` above.
        errors = info = {}
        if user_input is not None:
            try:
                info = await self.validate_input(user_input)
            except PulseAuthenticationError:
                errors["base"] = "invalid_auth"
            except PulseMFARequiredError as ex:
                adtpulse: PyADTPulseAsync = ex.adtpulse  # type: ignore[attr-defined]
                if ex.challenge is None:
                    await _async_logout_quietly(adtpulse)
                    errors["base"] = "mfa_required"
                else:
                    return await self._async_start_mfa(adtpulse, user_input)
            except (
                PulseAccountLockedError,
                PulseGatewayOfflineError,
                PulseServiceTemporarilyUnavailableError,
            ) as ex:
                errors["base"] = "service_unavailable"
                raise ConfigEntryNotReady from ex
            except PulseConnectionError as ex:
                errors["base"] = "cannot_connect"
                raise ConfigEntryNotReady from ex
            except Exception:  # pylint: disable=broad-except
                logger.exception("Unexpected exception")
                errors["base"] = "unknown"
            if not errors:
                return await self._async_save_entry(info, user_input)

        # If there is no user input or there were errors, show the form again,
        # including any errors that were found with the input.
        if user_input is None:
            user_input = self.init_data
        return self.async_show_form(
            step_id="user", data_schema=self._get_data_schema(user_input), errors=errors
        )

    async def _async_save_entry(
        self, info: dict[str, str], user_input: dict[str, Any]
    ) -> FlowResult:
        if not self._reauth_entry:
            return super().async_create_entry(title=info["title"], data=user_input)
        self.hass.config_entries.async_update_entry(
            self._reauth_entry, title=info["title"], data=user_input
        )
        await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
        return self.async_abort(reason="reauth_successful")

    async def _async_start_mfa(
        self, adtpulse: PyADTPulseAsync, user_input: dict[str, Any]
    ) -> FlowResult:
        """Send a verification code and ask for it."""
        try:
            await adtpulse.async_request_mfa_code()
        except Exception:  # pylint: disable=broad-except
            logger.exception("Could not request an ADT Pulse verification code")
            await _async_logout_quietly(adtpulse)
            return self.async_show_form(
                step_id="user",
                data_schema=self._get_data_schema(user_input),
                errors={"base": "mfa_code_not_sent"},
            )
        self._mfa_pulse = adtpulse
        self._mfa_input = user_input
        return await self.async_step_mfa()

    async def async_step_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Finish a login that needs MFA with the code ADT Pulse sent."""
        errors: dict[str, str] = {}
        if user_input is not None and self._mfa_pulse and self._mfa_input:
            try:
                await self._mfa_pulse.async_complete_mfa_login(
                    user_input[CONF_MFA_CODE], device_name=MFA_DEVICE_NAME
                )
                info = await self._finish_validation(self._mfa_pulse)
            except PulseAuthenticationError:
                errors["base"] = "invalid_mfa_code"
            except Exception:  # pylint: disable=broad-except
                # the code may already be spent, so start over
                logger.exception("Unexpected exception finishing MFA login")
                adtpulse, input_data = self._mfa_pulse, self._mfa_input
                self._mfa_pulse = self._mfa_input = None
                await _async_logout_quietly(adtpulse)
                return self.async_show_form(
                    step_id="user",
                    data_schema=self._get_data_schema(input_data),
                    errors={"base": "unknown"},
                )
            else:
                data = self._mfa_input
                self._mfa_pulse = self._mfa_input = None
                return await self._async_save_entry(info, data)
        return self.async_show_form(
            step_id="mfa",
            data_schema=vol.Schema({vol.Required(CONF_MFA_CODE): cv.string}),
            errors=errors,
        )

    async def async_step_reauth(self, user_input=None):
        """Perform reauth upon an API authentication error."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm(user_input)

    async def async_step_reauth_confirm(self, user_input=None):
        """Dialog that informs the user that reauth is required."""
        if user_input is None:
            orig_input = {}
            if self._reauth_entry is not None:
                orig_input = self._reauth_entry.data.copy()
            return self.async_show_form(
                step_id="reauth_confirm", data_schema=self._get_data_schema(orig_input)
            )
        return await self.async_step_user(user_input)


class PulseOptionsFlowHandler(OptionsFlowWithConfigEntry):
    """Handle options flow for Pulse integration."""

    def _validate_options(self, options: dict[str, Any]) -> dict[str, Any]:
        """Validate options."""
        new_relogin = options.get(CONF_RELOGIN_INTERVAL, ADT_DEFAULT_RELOGIN_INTERVAL)
        new_keepalive = options.get(
            CONF_KEEPALIVE_INTERVAL, ADT_DEFAULT_KEEPALIVE_INTERVAL
        )
        if new_relogin != 0 and new_relogin < ADT_MIN_RELOGIN_INTERVAL:
            return {"base": "min_relogin"}
        if new_keepalive > ADT_MAX_KEEPALIVE_INTERVAL:
            return {"base": "max_keepalive"}
        return {"title": "Pulse Integration Options"}

    @staticmethod
    def _get_options_schema(original_input: dict[str, Any] | None) -> vol.Schema:
        if original_input is None:
            original_input = {}
        OPTIONS_SCHEMA = vol.Schema(
            {
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=original_input.get(
                        CONF_SCAN_INTERVAL, ADT_DEFAULT_POLL_INTERVAL
                    ),
                ): cv.positive_float,
                vol.Optional(
                    CONF_RELOGIN_INTERVAL,
                    default=original_input.get(
                        CONF_RELOGIN_INTERVAL, ADT_DEFAULT_RELOGIN_INTERVAL
                    ),
                ): cv.positive_int,
                vol.Optional(
                    CONF_KEEPALIVE_INTERVAL,
                    default=original_input.get(
                        CONF_KEEPALIVE_INTERVAL, ADT_DEFAULT_KEEPALIVE_INTERVAL
                    ),
                ): cv.positive_int,
            }
        )
        return OPTIONS_SCHEMA

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        result: dict[str, Any] | None = None
        if user_input is not None:
            result = self._validate_options(user_input)
            if result.get("base") is None:
                return self.async_create_entry(title=result["title"], data=user_input)
        else:
            user_input = self.options

        return self.async_show_form(
            step_id="init",
            data_schema=self._get_options_schema(user_input),
            errors=result,
        )
