"""Config, reauth, reconfigure and options flows for Daikin One."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any, Final

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_API_KEY, CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .api import DaikinOneClient, DaikinOneError
from .const import (
    CONF_INTEGRATOR_TOKEN,
    CONF_READ_ONLY,
    CONF_SCAN_INTERVAL,
    DEFAULT_READ_ONLY,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA: Final = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="email")),
        vol.Required(CONF_API_KEY): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        vol.Required(CONF_INTEGRATOR_TOKEN): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
    }
)

#: Title for an entry whose email cannot be masked (and the upstream ha-daikinone title).
DEFAULT_TITLE: Final = "Daikin One"

#: Client error code -> `config.error.*` key.
ERROR_BY_CODE: Final[dict[str, str]] = {
    "invalid_credentials": "invalid_auth",
    "token_expired": "invalid_auth",
    "invalid_api_key": "invalid_api_key",
    "rate_limited": "rate_limited",
}

__all__ = ["DaikinOneConfigFlow", "DaikinOneOptionsFlow", "entry_title"]


def entry_title(email: str) -> str:
    """Return the config entry title for an account, with the email masked.

    Home Assistant core logs ``ConfigEntry.title`` verbatim whenever setup fails
    (WARNING on ConfigEntryAuthFailed, INFO on every ConfigEntryNotReady retry), so the
    raw address would end up in any log a user shares. The first character plus the
    domain still tells two accounts apart; the exact address stays in ``entry.data``.
    """
    local, at_sign, domain = email.partition("@")
    if not at_sign or len(local) < 2:
        return DEFAULT_TITLE
    return f"{local[0]}***@{domain}"


async def _async_validate(hass: HomeAssistant, data: Mapping[str, Any]) -> str | None:
    """Try the credentials against the API; return an error key or None on success."""
    client = DaikinOneClient(
        async_get_clientsession(hass),
        data[CONF_EMAIL],
        data[CONF_API_KEY],
        data[CONF_INTEGRATOR_TOKEN],
    )
    try:
        devices = await client.async_get_devices()
    except DaikinOneError as err:
        return ERROR_BY_CODE.get(err.code, "cannot_connect")
    except Exception:
        _LOGGER.exception("Unexpected error while validating the Daikin One credentials")
        return "unknown"
    return None if devices else "no_devices"


class DaikinOneConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Daikin One config flow."""

    VERSION = 2
    MINOR_VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> DaikinOneOptionsFlow:
        """Return the options flow handler."""
        return DaikinOneOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Collect the email, API key and Integrator Token for a new account."""
        errors: dict[str, str] = {}
        if user_input is not None:
            # The email identifies the account; Daikin treats it as case-sensitive, so the
            # exact spelling is stored while the unique id is lowercased.
            await self.async_set_unique_id(user_input[CONF_EMAIL].lower())
            self._abort_if_unique_id_configured()
            error = await _async_validate(self.hass, user_input)
            if error is None:
                return self.async_create_entry(title=entry_title(user_input[CONF_EMAIL]), data=user_input)
            errors["base"] = error

        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start reauthentication (also used for legacy password-only entries)."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for fresh credentials for the existing entry."""
        return await self._async_step_credentials(self._get_reauth_entry(), "reauth_confirm", user_input)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Let the user change the stored credentials."""
        return await self._async_step_credentials(self._get_reconfigure_entry(), "reconfigure", user_input)

    async def _async_step_credentials(
        self,
        entry: ConfigEntry,
        step_id: str,
        user_input: dict[str, Any] | None,
    ) -> ConfigFlowResult:
        """Shared reauth/reconfigure body: validate, then rewrite the entry data."""
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_EMAIL].lower())
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            error = await _async_validate(self.hass, user_input)
            if error is None:
                # The legacy password is dropped only now that the new credentials work.
                data = {key: value for key, value in entry.data.items() if key != CONF_PASSWORD} | user_input
                # Re-titles entries written before the email was masked out of the title.
                return self.async_update_reload_and_abort(entry, title=entry_title(user_input[CONF_EMAIL]), data=data)
            errors["base"] = error

        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(STEP_USER_SCHEMA, {CONF_EMAIL: entry.data.get(CONF_EMAIL)}),
            errors=errors,
        )


class DaikinOneOptionsFlow(OptionsFlowWithReload):
    """Handle the Daikin One options (poll interval, read-only mode)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Set the polling interval (never below Daikin's 3-minute floor) and read-only mode."""
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    CONF_READ_ONLY: bool(user_input[CONF_READ_ONLY]),
                }
            )

        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, MIN_SCAN_INTERVAL)
        read_only = self.config_entry.options.get(CONF_READ_ONLY, DEFAULT_READ_ONLY)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=current): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL,
                            max=MAX_SCAN_INTERVAL,
                            step=30,
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="s",
                        )
                    ),
                    vol.Required(CONF_READ_ONLY, default=read_only): BooleanSelector(),
                }
            ),
        )
