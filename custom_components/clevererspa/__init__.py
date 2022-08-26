"""ClevererSpa Integration"""
from __future__ import annotations

from datetime import timedelta
from logging import getLogger

import async_timeout
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .clevererspa import CleverSpaApi, CleverSpaDeviceReport
from .const import (
    CONF_API_ROOT,
    CONF_API_ROOT_EU,
    CONF_PASSWORD,
    CONF_USER_TOKEN,
    CONF_USER_TOKEN_EXPIRY,
    CONF_USERNAME,
    DOMAIN,
)

_LOGGER = getLogger(__name__)
_PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up CleverSpa from a config entry."""
    username = entry.data.get(CONF_USERNAME)
    password = entry.data.get(CONF_PASSWORD)
    api_root = entry.data.get(CONF_API_ROOT)
    user_token = entry.data.get(CONF_USER_TOKEN)
    user_token_expiry = entry.data.get(CONF_USER_TOKEN_EXPIRY)

    session = async_get_clientsession(hass)

    if user_token:
        _LOGGER.info("Reusing existing access token")
    else:
        _LOGGER.info("No saved token found - requesting a new one")
        try:
            token = await CleverSpaApi.get_user_token(
                session, username, password
            )
        except Exception as ex:  # pylint: disable=broad-except
            raise ConfigEntryNotReady from ex
        user_token = token.user_token
        user_token_expiry = token.expiry

        new_config_data = {
            CONF_USER_TOKEN: user_token,
            CONF_USER_TOKEN_EXPIRY: user_token_expiry,
        }

        hass.config_entries.async_update_entry(
            entry, data={**entry.data, **new_config_data}
        )

    api = CleverSpaApi(session, user_token, api_root)
    coordinator = CleverSpaUpdateCoordinator(hass, api)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    hass.config_entries.async_setup_platforms(entry, _PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok: bool = await hass.config_entries.async_unload_platforms(
        entry, _PLATFORMS
    )
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrates old config versions to the latest."""

    _LOGGER.debug("Migrating from version %s", entry.version)

    if entry.version == 1:
        # API root needs to be set
        # In version 1, this was hard coded to the EU endpoint
        new = {**entry.data}
        new[CONF_API_ROOT] = CONF_API_ROOT_EU
        entry.version = 2
        hass.config_entries.async_update_entry(entry, data=new)

        _LOGGER.info("Migration to version %s successful", entry.version)
        return True

    _LOGGER.error("Existing schema version %s is not supported", entry.version)
    return False


class CleverSpaUpdateCoordinator(DataUpdateCoordinator[dict[str, CleverSpaDeviceReport]]):
    """Update coordinator that polls the device status for all devices in an account."""

    def __init__(self, hass: HomeAssistant, api: CleverSpaApi) -> None:
        """Initialize my coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="CleverSpa API",
            update_interval=timedelta(seconds=30),
        )
        self.api = api

    async def _async_update_data(self) -> dict[str, CleverSpaDeviceReport]:
        """Fetch data from API endpoint.
        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        try:
            async with async_timeout.timeout(10):
                await self.api.refresh_bindings()
                return await self.api.fetch_data()
        except Exception as err:
            _LOGGER.exception("Data update failed")
            raise UpdateFailed(f"Error communicating with API: {err}") from err