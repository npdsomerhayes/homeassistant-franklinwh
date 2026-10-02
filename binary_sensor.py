"""Binary sensor platform for FranklinWH."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

import franklinwh
import httpx
import voluptuous as vol

from homeassistant.components.binary_sensor import (
    PLATFORM_SCHEMA as BINARY_SENSOR_PLATFORM_SCHEMA,
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import (
    CONF_ID,
    CONF_PASSWORD,
    CONF_USERNAME,
    MAJOR_VERSION as HASS_MAJOR_VERSION,
    MINOR_VERSION as HASS_MINOR_VERSION,
)
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .sensor import DEFAULT_UPDATE_INTERVAL, DOMAIN, FETCH_TIMEOUT, StaleDataCache, supports_http2

_LOGGER = logging.getLogger(__name__)

PLATFORM_SCHEMA = BINARY_SENSOR_PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Required(CONF_ID): cv.string,
        vol.Optional("use_sn", default=False): cv.boolean,
        vol.Optional("prefix", default=False): cv.string,
        vol.Optional(
            "update_interval", default=DEFAULT_UPDATE_INTERVAL
        ): cv.time_period,
        vol.Optional("tolerate_stale_data", default=False): cv.boolean,
    }
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the binary sensor platform."""
    username: str = config[CONF_USERNAME]
    password: str = config[CONF_PASSWORD]
    gateway: str = config[CONF_ID]
    update_interval: timedelta = config["update_interval"]

    if config["use_sn"] and config["use_sn"] != "False":
        unique_id = gateway
    else:
        unique_id = None

    if config["prefix"] and config["prefix"] != "False":
        prefix = config["prefix"]
    else:
        prefix = "FranklinWH"

    hass.data.setdefault(DOMAIN, {})
    if gateway in hass.data[DOMAIN]:
        coordinator = hass.data[DOMAIN][gateway]
    else:
        fetcher = franklinwh.TokenFetcher(username, password)
        if supports_http2():
            # pylint: disable=no-name-in-module,import-outside-toplevel
            from homeassistant.helpers.httpx_client import (  # noqa: PLC0415
                SSL_ALPN_HTTP11_HTTP2,  # type: ignore  # noqa: PGH003
                create_async_httpx_client,
            )
            # pylint: enable=no-name-in-module,import-outside-toplevel

            def get_client() -> httpx.AsyncClient:
                return create_async_httpx_client(hass, alpn_protocols=SSL_ALPN_HTTP11_HTTP2)

            franklinwh.HttpClientFactory.set_client_factory(get_client)
            client = franklinwh.Client(fetcher, gateway)
        else:
            client = await hass.async_add_executor_job(franklinwh.Client, fetcher, gateway)

        cache = StaleDataCache()

        async def _update_data() -> franklinwh.Stats:
            max_retries = 3
            retry_delay = 2

            _LOGGER.debug("Fetching latest data from FranklinWH")
            for attempt in range(max_retries):
                if attempt > 0:
                    _LOGGER.warning("Trying again")
                    await asyncio.sleep(retry_delay)
                try:
                    async with asyncio.timeout(FETCH_TIMEOUT):
                        data = await client.get_stats()
                except TimeoutError as e:
                    _LOGGER.warning("Error getting data from FranklinWH - request hung for %ss: %s", FETCH_TIMEOUT, e)
                except franklinwh.client.DeviceTimeoutException as e:
                    _LOGGER.warning("Error getting data from FranklinWH - Device Timeout: %s", e)
                except franklinwh.client.GatewayOfflineException as e:
                    _LOGGER.warning("Error getting data from FranklinWH - Gateway Offline %s", e)
                except franklinwh.client.AccountLockedException as e:
                    _LOGGER.warning("Error getting data from FranklinWH - Account Locked %s", e)
                except franklinwh.client.InvalidCredentialsException as e:
                    _LOGGER.warning("Error getting data from FranklinWH - Invalid Credentials %s", e)
                except franklinwh.client.InvalidDataException as e:
                    _LOGGER.warning("Error getting data from FranklinWH - Invalid Body Returned %s", e)
                except httpx.ReadTimeout as e:
                    _LOGGER.warning("Timeout fetching data from FranklinWH: %s", e)
                else:
                    if attempt > 0:
                        _LOGGER.warning("Successfully fetched data from FranklinWH after retry")
                    else:
                        _LOGGER.debug("Fetched latest data from FranklinWH: %s", data)
                    cache.store(data)
                    return data

            _LOGGER.warning("Failed to fetch data from FranklinWH after %s attempts", max_retries)

            if config["tolerate_stale_data"] and cache.is_populated():
                return cache.data()

            raise UpdateFailed(f"Failed to fetch data from FranklinWH after {max_retries} attempts.")

        coordinator = DataUpdateCoordinator[franklinwh.Stats](
            hass,
            _LOGGER,
            name="franklinwh",
            update_method=_update_data,
            update_interval=update_interval,
            always_update=False,
        )
        hass.data[DOMAIN][gateway] = coordinator
        await coordinator.async_refresh()

    # Storm Hedge: own client + coordinator polling every 5 minutes.
    # Uses a separate TokenFetcher/Client so it doesn't share session state
    # with the main stats coordinator (same pattern as select.py / number.py).
    _storm_fetcher = franklinwh.TokenFetcher(username, password)
    _storm_client = franklinwh.Client(_storm_fetcher, gateway)

    async def _storm_update_data() -> dict:
        """Fetch Storm Hedge status from the gateway."""
        try:
            setting = (
                await _storm_client._get(  # noqa: SLF001
                    _storm_client.url_base
                    + "hes-gateway/terminal/weather/getStormSetting",
                    {"equipNo": gateway},
                )
            )["result"]
            storms = (
                await _storm_client._get(  # noqa: SLF001
                    _storm_client.url_base
                    + "hes-gateway/terminal/weather/getProgressingStormList",
                    {"equipNo": gateway},
                )
            )["result"] or []
            return {
                "enabled": setting.get("enableStorm", 0) == 1,
                "storms": storms,
            }
        except franklinwh.client.DeviceTimeoutException as e:
            raise UpdateFailed(f"Device timeout: {e}") from e
        except franklinwh.client.GatewayOfflineException as e:
            raise UpdateFailed(f"Gateway offline: {e}") from e
        except franklinwh.client.AccountLockedException as e:
            raise UpdateFailed(f"Account locked: {e}") from e
        except franklinwh.client.InvalidCredentialsException as e:
            raise UpdateFailed(f"Invalid credentials: {e}") from e
        except Exception as e:  # noqa: BLE001
            raise UpdateFailed(f"Error reading storm hedge: {e}") from e

    storm_coordinator = DataUpdateCoordinator[dict](
        hass,
        _LOGGER,
        name="franklinwh_storm",
        update_method=_storm_update_data,
        update_interval=timedelta(minutes=5),
        always_update=False,
    )
    await storm_coordinator.async_refresh()

    async_add_entities(
        [
            GridOutageSensor(coordinator, prefix, unique_id),
            StormActiveSensor(storm_coordinator, prefix, gateway),
            StormHedgeEnabledSensor(storm_coordinator, prefix, gateway),
        ]
    )


class FranklinBinarySensor(
    CoordinatorEntity[DataUpdateCoordinator[franklinwh.Stats]], BinarySensorEntity
):
    """Base class for FranklinWH binary sensors."""

    def __init__(self, coordinator, prefix, unique_id, unique_id_suffix) -> None:
        """Initializer."""
        super().__init__(coordinator)
        self._attr_name = prefix + " " + unique_id_suffix.replace("_", " ").title()
        if unique_id_suffix and unique_id:
            self._attr_has_entity_name = True
            self._attr_unique_id = unique_id + unique_id_suffix

    @property
    def available(self) -> bool:
        """Is the sensor available?"""
        return (
            self.coordinator.last_update_success and self.coordinator.data is not None
        )


class GridOutageSensor(FranklinBinarySensor):
    """True when the grid is not available (down or intentionally off)."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator, prefix, unique_id) -> None:
        """Initializer."""
        super().__init__(coordinator, prefix, unique_id, "_grid_outage")

    @property
    def is_on(self) -> bool:
        """Return True when the grid is unavailable."""
        return self.coordinator.data.current.grid_status != franklinwh.GridStatus.NORMAL


class StormActiveSensor(
    CoordinatorEntity[DataUpdateCoordinator[dict]], BinarySensorEntity
):
    """True when a weather event is progressing and Storm Hedge is pre-charging."""

    _attr_device_class = BinarySensorDeviceClass.SAFETY

    def __init__(self, coordinator: DataUpdateCoordinator, prefix: str, gateway: str) -> None:
        """Initializer."""
        super().__init__(coordinator)
        self._attr_name = f"{prefix} Storm Active"
        self._attr_unique_id = f"{gateway}_storm_active"

    @property
    def available(self) -> bool:
        """Entity is available when the coordinator has data."""
        return (
            self.coordinator.last_update_success
            and self.coordinator.data is not None
        )

    @property
    def is_on(self) -> bool:
        """Return True when at least one storm is progressing."""
        return len(self.coordinator.data.get("storms", [])) > 0

    @property
    def extra_state_attributes(self) -> dict:
        """Return severity, onset, and duration of the active storm."""
        storms = self.coordinator.data.get("storms", [])
        if not storms:
            return {}
        storm = storms[0]
        return {
            "severity": storm.get("severity"),
            "onset": storm.get("onset"),
            "duration_minutes": storm.get("durationTime"),
        }


class StormHedgeEnabledSensor(
    CoordinatorEntity[DataUpdateCoordinator[dict]], BinarySensorEntity
):
    """True when Storm Hedge (MSN weather alerts → backup pre-charging) is enabled."""

    def __init__(self, coordinator: DataUpdateCoordinator, prefix: str, gateway: str) -> None:
        """Initializer."""
        super().__init__(coordinator)
        self._attr_name = f"{prefix} Storm Hedge Enabled"
        self._attr_unique_id = f"{gateway}_storm_hedge_enabled"

    @property
    def available(self) -> bool:
        """Entity is available when the coordinator has data."""
        return (
            self.coordinator.last_update_success
            and self.coordinator.data is not None
        )

    @property
    def is_on(self) -> bool:
        """Return True when Storm Hedge is enabled on the gateway."""
        return self.coordinator.data.get("enabled", False)

