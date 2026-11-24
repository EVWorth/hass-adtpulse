"""HASS ADT Pulse Config Flow."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
import homeassistant.helpers.config_validation as cv
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
from .fingerprint import generate_fingerprint

CONF_MFA_CODE = "code"
# the name the fingerprint is trusted under in the ADT Pulse account
MFA_DEVICE_NAME = "Home Assistant"

logger = logging.getLogger(__name__)


async def _async_logout_quietly(adtpulse: PyADTPulseAsync) -> None:
    """Log out a session the flow is abandoning; a failure must not block the form."""
    try:
        await adtpulse.async_logout()
    except Exception:
        logger.debug("Could not log out an abandoned ADT Pulse session", exc_info=True)


class PulseConfigFlow(ConfigFlow, domain=ADTPULSE_DOMAIN):  # type: ignore
    """Handle a config flow for ADT Pulse."""

    VERSION = 1
    CONNECTION_CLASS = CONN_CLASS_CLOUD_PUSH

    _reauth_entry: ConfigEntry | None = None
    _mfa_pulse: PyADTPulseAsync | None = None
    _mfa_input: dict[str, Any] | None = None

    @staticmethod
    def _get_data_schema(orig_input: dict[str, Any] | None) -> vol.Schema:
        if orig_input is None:
            orig_input = {}
        return vol.Schema(
            {
                vol.Required(
                    CONF_USERNAME, default=orig_input.get(CONF_USERNAME, "")
                ): cv.string,
                vol.Required(
                    CONF_PASSWORD, default=orig_input.get(CONF_PASSWORD, "")
                ): cv.string,
                vol.Required(
                    CONF_HOSTNAME,
                    default=orig_input.get(CONF_HOSTNAME, DEFAULT_API_HOST),
                ): vol.In([DEFAULT_API_HOST, API_HOST_CA]),
            }
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> PulseOptionsFlowHandler:
        """Create the options flow."""
        return PulseOptionsFlowHandler(config_entry)

    def _with_fingerprint(self, user_input: dict[str, Any]) -> dict[str, Any]:
        """Add the fingerprint to log in with.

        Re-authentication keeps the entry's fingerprint, which ADT may already
        trust. A new entry gets a new one, which is trusted after the
        verification code.
        """
        fingerprint = None
        if self._reauth_entry is not None:
            fingerprint = self._reauth_entry.data.get(CONF_FINGERPRINT)
        return {**user_input, CONF_FINGERPRINT: fingerprint or generate_fingerprint()}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask for the account, log in with it, and ask for a code if ADT wants one.

        Args:
            user_input (Optional[Dict[str, Any]], optional): user input.
                    Defaults to None.

        Returns:
            FlowResult: the flow result
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = self._with_fingerprint(user_input)
            try:
                # raises ValueError for input it rejects, such as a bad email
                adtpulse = PyADTPulseAsync(
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                    user_input[CONF_FINGERPRINT],
                    service_host=user_input[CONF_HOSTNAME],
                )
            except ValueError:
                errors["base"] = "invalid_auth"
            else:
                try:
                    await adtpulse.async_login()
                except PulseMFARequiredError as ex:
                    if ex.challenge is not None:
                        return await self._async_start_mfa(adtpulse, user_input)
                    errors["base"] = "mfa_required"
                except PulseAuthenticationError:
                    errors["base"] = "invalid_auth"
                except (
                    PulseAccountLockedError,
                    PulseGatewayOfflineError,
                    PulseServiceTemporarilyUnavailableError,
                ):
                    errors["base"] = "service_unavailable"
                except PulseConnectionError:
                    errors["base"] = "cannot_connect"
                except Exception:
                    logger.exception("Unexpected exception")
                    errors["base"] = "unknown"
                else:
                    return await self._async_save_entry(adtpulse, user_input)
                await _async_logout_quietly(adtpulse)

        return self.async_show_form(
            step_id="user", data_schema=self._get_data_schema(user_input), errors=errors
        )

    async def _async_save_entry(
        self, adtpulse: PyADTPulseAsync, user_input: dict[str, Any]
    ) -> FlowResult:
        """Read the site from a logged-in session, log it out and save the entry."""
        try:
            title = f"ADT: Site {adtpulse.site.id}"
        finally:
            await _async_logout_quietly(adtpulse)
        if self._reauth_entry is None:
            return self.async_create_entry(title=title, data=user_input)
        return self.async_update_reload_and_abort(
            self._reauth_entry, title=title, data=user_input
        )

    async def _async_start_mfa(
        self, adtpulse: PyADTPulseAsync, user_input: dict[str, Any]
    ) -> FlowResult:
        """Send a verification code and ask for it."""
        try:
            await adtpulse.async_request_mfa_code()
        except Exception:
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

    @callback
    def async_remove(self) -> None:
        """Log out a pending MFA session when the flow is closed."""
        if self._mfa_pulse is not None:
            self.hass.async_create_task(_async_logout_quietly(self._mfa_pulse))
            self._mfa_pulse = None

    async def async_step_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Finish a login that needs MFA with the code ADT Pulse sent."""
        errors: dict[str, str] = {}
        if user_input is not None and self._mfa_pulse and self._mfa_input:
            adtpulse, data = self._mfa_pulse, self._mfa_input
            try:
                await adtpulse.async_complete_mfa_login(
                    user_input[CONF_MFA_CODE].strip(), device_name=MFA_DEVICE_NAME
                )
            except PulseAuthenticationError:
                errors["base"] = "invalid_mfa_code"
            except Exception as ex:
                # the code may already be spent, so start over
                logger.exception("Unexpected exception finishing MFA login")
                self._mfa_pulse = self._mfa_input = None
                await _async_logout_quietly(adtpulse)
                return self.async_show_form(
                    step_id="user",
                    data_schema=self._get_data_schema(data),
                    errors={
                        "base": "cannot_connect"
                        if isinstance(ex, PulseConnectionError)
                        else "unknown"
                    },
                )
            else:
                self._mfa_pulse = self._mfa_input = None
                return await self._async_save_entry(adtpulse, data)
        return self.async_show_form(
            step_id="mfa",
            data_schema=vol.Schema({vol.Required(CONF_MFA_CODE): cv.string}),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Perform reauth upon an API authentication error."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
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
