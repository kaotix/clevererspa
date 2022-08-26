"""CleverSpa API."""
from dataclasses import dataclass
from enum import Enum, auto
from logging import getLogger
from time import time
from typing import Any
import asyncio

from aiohttp import ClientResponse, ClientSession
import async_timeout
import requests
from .const import GOOGLE_AUTH_URL, GOOGLE_AUTH_KEY, FIREBASE_DB

_LOGGER = getLogger(__name__)
_HEADERS = {
    "Content-type": "application/json; charset=UTF-8",
    "X-Gizwits-Application-Id": "805cc6a3f41b48aeae471e2fcb6ebc73",
}
_TIMEOUT = 10

# How old the latest update can be before a spa is considered offline
_CONNECTIVITY_TIMEOUT = 1000


class TemperatureUnit(Enum):
    """Temperature units supported by the spa."""

    CELSIUS = auto()
    FAHRENHEIT = auto()


@dataclass
class CleverSpaDevice:
    """A device under a user's account."""

    device_id: str
    alias: str
    product_name: str


@dataclass
class CleverSpaDeviceStatus:
    """A snapshot of the status of a device."""

    timestamp: int
    temp_now: float
    temp_set: float
    temp_set_unit: TemperatureUnit
    heat_power: bool
    filter_power: bool
    bubble_power: bool
    filter_age: int
    errors: list[int]

    @property
    def online(self) -> bool:
        """Determine whether the device is online based on the age of the latest update."""
        return self.timestamp > (time() - _CONNECTIVITY_TIMEOUT)


@dataclass
class CleverSpaUserToken:
    """User authentication token, obtained (and ideally stored) following a successful login."""

    user_id: str
    user_token: str
    expiry: int


@dataclass
class CleverSpaDeviceReport:
    """A device report, which combines device metadata with a current status snapshot."""

    device: CleverSpaDevice
    status: CleverSpaDeviceStatus


class CleverSpaException(Exception):
    """An exception returned via the API."""


class CleverSpaOfflineException(CleverSpaException):
    """Device is offline."""

    def __init__(self) -> None:
        """Construct the exception."""
        super().__init__("Device is offline")


class CleverSpaAuthException(CleverSpaException):
    """An authentication error."""


class CleverSpaUserDoesNotExistException(CleverSpaAuthException):
    """User does not exist."""


class CleverSpaIncorrectPasswordException(CleverSpaAuthException):
    """Password is incorrect."""

class AuthError(Exception):
    """Base exception for auth errors"""


async def raise_for_status(response: ClientResponse) -> None:
    """Raise an exception based on the response."""
    if response.ok:
        return

    # Try to parse out the CleverSpa error code
    try:
        api_error = await response.json()
    except Exception:  # pylint: disable=broad-except
        response.raise_for_status()

    # TODO: We don't know the error codes for CleverSpa yet
    #error_code = api_error.get("error_code", 0)
    #if error_code == 9005:
    #    raise CleverSpaUserDoesNotExistException()
    #if error_code == 9042:
    #    raise CleverSpaOfflineException()
    #if error_code == 9020:
    #    raise CleverSpaIncorrectPasswordException()

    # If we don't understand the error code, provide more detail for debugging
    response.raise_for_status()


class CleverSpaApi:
    """CleverSpa API."""

    def __init__(self, session: ClientSession, user_token: str, api_root: str) -> None:
        """Initialize the API with a user token."""
        self._session = session
        self._user_token = user_token
        self._api_root = api_root

        # Maps device IDs to device info
        self._bindings: dict[str, CleverSpaDevice] | None = None

        # Cache containing state information for each device received from the API
        # This is used to work around an annoyance where changes to settings via
        # a POST request are not immediately reflected in a subsequent GET request.
        #
        # When updating state via HA, we update the cache and return this value
        # until the API can provide us with a response containing a timestamp
        # more recent than the local update.
        self._local_state_cache: dict[str, CleverSpaDeviceStatus] = {}


    async def login(self, email, password):
        try:
            user = requests.post(
                GOOGLE_AUTH_URL,
                params={"key": GOOGLE_AUTH_KEY},
                json={"email": email, "password": password, "returnSecureToken": True},
            ).json()
        except requests.exceptions.HTTPError as e:
            e = e.response.json()
            raise AuthError(e["error"]["message"])
        try:
            info = requests.get(
                f"{FIREBASE_DB}/users/{user['localId']}.json",
                params={"auth": user["idToken"]},
            ).json()
        except requests.exceptions.HTTPError as e:
            e = e.response.json()
            raise AuthError(e["error"]["message"])
        return dict(info)

    @staticmethod
    # TODO: This needs to change to the CleverSpa AUTH method
    async def get_user_token(session: ClientSession, username: str, password: str) -> CleverSpaUserToken:
        """
        Login and obtain a user token.
        The server rate-limits requests for this fairly aggressively.
        """
        body = {"email": username, "password": password, "returnSecureToken": True}

        async with async_timeout.timeout(_TIMEOUT):
            user = await session.post(
                GOOGLE_AUTH_URL,
                params={"key": GOOGLE_AUTH_KEY},
                json={"email": username, "password": password, "returnSecureToken": True},
            )
            await raise_for_status(user)
            user_json: dict[str, Any] = await user.json(content_type=None)

            info = await session.get(
                f"{FIREBASE_DB}/users/{user_json['localId']}.json",
                params={"auth": user_json["idToken"]},
            )
            info.raise_for_status()
            info_json: dict[str, Any] = await info.json(content_type=None)

        return CleverSpaUserToken(
            info_json["uid"], info_json["token"], info_json["expire_at"]
        )

    async def refresh_bindings(self) -> None:
        """Refresh and store the list of devices available in the account."""
        self._bindings = {
            device.device_id: device for device in await self._get_bindings()
        }

    async def _get_bindings(self) -> list[CleverSpaDevice]:
        """Get the list of devices available in the account."""
        headers = dict(_HEADERS)
        headers["X-Gizwits-User-token"] = self._user_token
        api_data = await self._do_get(f"{self._api_root}/app/bindings", headers)
        return list(
            map(
                lambda raw: CleverSpaDevice(
                    raw["did"], raw["dev_alias"], raw["product_name"]
                ),
                api_data["devices"],
            )
        )

    async def fetch_data(self) -> dict[str, CleverSpaDeviceReport]:
        """Fetch the latest data for all devices."""

        results: dict[str, CleverSpaDeviceReport] = {}

        if not self._bindings:
            return results

        for did, device_info in self._bindings.items():
            latest_data = await self._do_get(
                f"{self._api_root}/app/devdata/{did}/latest", _HEADERS
            )

            # Work out whether the received API update is more recent than the
            # locally cached state
            api_update_timestamp = latest_data["updated_at"]
            local_update_timestamp = 0
            if cached_state := self._local_state_cache.get(did):
                local_update_timestamp = cached_state.timestamp

            # If the API timestamp is more recent, update the cache
            if api_update_timestamp >= local_update_timestamp:
                _LOGGER.debug("New data received for device %s", did)
                device_attrs = latest_data["attr"]

                errors = []
                device_status = CleverSpaDeviceStatus(
                    latest_data["updated_at"],
                    device_attrs["Current_temperature"],
                    device_attrs["Temperature_setup"],
                    (
                        TemperatureUnit.CELSIUS
                    ),
                    device_attrs["Heater"] == 1,
                    device_attrs["Filter"] == 1,
                    device_attrs["Bubble"] == 1,
                    device_attrs["Time_filter"] == 0,
                    errors,
                )

                self._local_state_cache[did] = device_status

            else:
                _LOGGER.debug(
                    "Ignoring update for device %s as local data is newer", did
                )

            results[did] = CleverSpaDeviceReport(
                device_info,
                self._local_state_cache[did],
            )

        return results

    async def set_heat(self, device_id: str, heat: bool) -> None:
        """
        Turn the heater on/off.
        Turning the heater on will also turn on the filter pump.
        """
        _LOGGER.debug("Setting heater mode to %s", "ON" if heat else "OFF")
        headers = dict(_HEADERS)
        headers["X-Gizwits-User-token"] = self._user_token
        await self._do_post(
            f"{self._api_root}/app/control/{device_id}",
            headers,
            {"attrs": {"Heater": 1 if heat else 0}},
        )
        self._local_state_cache[device_id].timestamp = int(time())
        self._local_state_cache[device_id].heat_power = heat
        if heat:
            self._local_state_cache[device_id].filter_power = True
            await self.fetch_data()
        else:
            await self.fetch_data()
            # TODO: Filter cooldown for 30 seconds. This also needs to add some kind of
            # blocking for turning off the filter if it's in cooldown
            await asyncio.sleep(30)
            await self.set_filter(self._local_state_cache[device_id], False)


    async def set_filter(self, device_id: str, filtering: bool) -> None:
        """Turn the filter pump on/off."""
        _LOGGER.debug("Setting filter mode to %s", "ON" if filtering else "OFF")
        headers = dict(_HEADERS)
        headers["X-Gizwits-User-token"] = self._user_token
        await self._do_post(
            f"{self._api_root}/app/control/{device_id}",
            headers,
            {"attrs": {"Filter": 1 if filtering else 0}},
        )
        self._local_state_cache[device_id].timestamp = int(time())
        self._local_state_cache[device_id].filter_power = filtering
        if not filtering:
            self._local_state_cache[device_id].wave_power = False
            self._local_state_cache[device_id].heat_power = False

    async def set_locked(self, device_id: str, locked: bool) -> None:
        """Lock or unlock the physical control panel."""
        _LOGGER.debug("Setting lock state to %s", "ON" if locked else "OFF")
        headers = dict(_HEADERS)
        headers["X-Gizwits-User-token"] = self._user_token
        await self._do_post(
            f"{self._api_root}/app/control/{device_id}",
            headers,
            {"attrs": {"locked": 1 if locked else 0}},
        )
        self._local_state_cache[device_id].timestamp = int(time())
        self._local_state_cache[device_id].locked = locked
        await self.fetch_data()

    async def set_bubbles(self, device_id: str, bubbles: bool) -> None:
        """Turn the bubbles on/off."""
        _LOGGER.debug("Setting bubbles mode to %s", "ON" if bubbles else "OFF")
        headers = dict(_HEADERS)
        headers["X-Gizwits-User-token"] = self._user_token
        await self._do_post(
            f"{self._api_root}/app/control/{device_id}",
            headers,
            {"attrs": {"Bubble": 1 if bubbles else 0}},
        )
        self._local_state_cache[device_id].timestamp = int(time())
        self._local_state_cache[device_id].filter_power = bubbles
        if bubbles:
            self._local_state_cache[device_id].filter_power = True
        await self.fetch_data()

    async def set_target_temp(self, device_id: str, target_temp: int) -> None:
        """Set the target temperature."""
        _LOGGER.debug("Setting target temperature to %d", target_temp)
        headers = dict(_HEADERS)
        headers["X-Gizwits-User-token"] = self._user_token
        await self._do_post(
            f"{self._api_root}/app/control/{device_id}",
            headers,
            {"attrs": {"Temperature_setup": target_temp}},
        )
        self._local_state_cache[device_id].timestamp = int(time())
        self._local_state_cache[device_id].temp_set = target_temp
        await self.fetch_data()

    async def _do_get(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        """Make an API call to the specified URL, returning the response as a JSON object."""
        async with async_timeout.timeout(_TIMEOUT):
            response = await self._session.get(url, headers=headers)
            response.raise_for_status()

            # All API responses are encoded using JSON, however the headers often incorrectly
            # state 'text/html' as the content type.
            # We have to disable the check to avoid an exception.
            response_json: dict[str, Any] = await response.json(content_type=None)
            return response_json

    async def _do_post(
        self, url: str, headers: dict[str, str], body: dict[str, Any]
    ) -> dict[str, Any]:
        """Make an API call to the specified URL, returning the response as a JSON object."""
        async with async_timeout.timeout(_TIMEOUT):
            response = await self._session.post(url, headers=headers, json=body)
            await raise_for_status(response)

            # All API responses are encoded using JSON, however the headers often incorrectly
            # state 'text/html' as the content type.
            # We have to disable the check to avoid an exception.
            response_json: dict[str, Any] = await response.json(content_type=None)
            return response_json