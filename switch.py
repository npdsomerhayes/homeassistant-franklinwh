#!/usr/bin/env python

from __future__ import annotations

from datetime import timedelta
import logging

import franklinwh

from homeassistant.components.switch import (
    SwitchEntity,
    PLATFORM_SCHEMA as PARENT_PLATFORM_SCHEMA,
)
import voluptuous as vol
import homeassistant.helpers.config_validation as cv
from homeassistant.const import (
        CONF_USERNAME,
        CONF_PASSWORD,
        CONF_ID,
        CONF_NAME,
        CONF_SWITCHES,
        )
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

_LOGGER = logging.getLogger(__name__)
DEFAULT_UPDATE_INTERVAL = 30

PLATFORM_SCHEMA = PARENT_PLATFORM_SCHEMA.extend(
        {
            vol.Required(CONF_USERNAME): cv.string,
            vol.Required(CONF_PASSWORD): cv.string,
            vol.Required(CONF_ID): cv.string,
            vol.Optional(CONF_NAME): cv.string,
            vol.Optional(CONF_SWITCHES): cv.ensure_list(vol.In([1, 2, 3])),
            vol.Optional("storm_hedge", default=False): cv.boolean,
            vol.Optional("use_sn", default=False): cv.boolean,
            vol.Optional("prefix", default=False): cv.string,
            vol.Optional("update_interval", default=DEFAULT_UPDATE_INTERVAL): cv.time_period,
            }
        )

async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None
) -> None:
    """Set up the switch platform."""
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

    # Storm Hedge switch (no smart circuit required)
    if config.get("storm_hedge"):
        await _setup_storm_hedge_switch(hass, config, add_entities)

    # Smart circuit switches
    if not config.get(CONF_SWITCHES):
        return

    name: str = config[CONF_NAME]
    switches: list[int] = list(map(lambda x: x-1, config[CONF_SWITCHES]))

    fetcher = franklinwh.TokenFetcher(username, password)
    client = franklinwh.Client(fetcher, gateway)

    async def _update_data():
        _LOGGER.debug("Fetching latest switch data from FranklinWH...")
        try:
            return await client.get_smart_switch_state()

        except franklinwh.client.DeviceTimeoutException as e:
            _LOGGER.warning("Error getting data from FranklinWH - Device Timeout: %s", e)
        except franklinwh.client.GatewayOfflineException as e:
            _LOGGER.warning("Error getting data from FranklinWH - Gateway Offline %s", e)
        except franklinwh.client.AccountLockedException as e:
            _LOGGER.warning("Error getting data from FranklinWH - Account Locked %s", e)
        except franklinwh.client.InvalidCredentialsException as e:
            _LOGGER.warning("Error getting data from FranklinWH - Invalid Credentials %s", e)

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="franklinwh",
        update_method=_update_data,
        update_interval=update_interval,
        always_update=False
    )
    await coordinator.async_refresh()

    add_entities([
        SmartCircuitSwitch(prefix, unique_id, name, switches, client, coordinator),
        ])


async def _setup_storm_hedge_switch(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Storm Hedge switch (called from async_setup_platform)."""
    username: str = config[CONF_USERNAME]
    password: str = config[CONF_PASSWORD]
    gateway: str = config[CONF_ID]

    if config.get("prefix") and config["prefix"] != "False":
        prefix = config["prefix"]
    else:
        prefix = "FranklinWH"

    _storm_fetcher = franklinwh.TokenFetcher(username, password)
    _storm_client = franklinwh.Client(_storm_fetcher, gateway)

    async def _storm_update_data() -> dict:
        try:
            result = (
                await _storm_client._get(  # noqa: SLF001
                    _storm_client.url_base
                    + "hes-gateway/terminal/weather/getStormSetting",
                    {"equipNo": gateway},
                )
            )["result"]
            return {"enabled": result.get("enableStorm", 0) == 1}
        except franklinwh.client.DeviceTimeoutException as e:
            raise UpdateFailed(f"Device timeout: {e}") from e
        except franklinwh.client.GatewayOfflineException as e:
            raise UpdateFailed(f"Gateway offline: {e}") from e
        except Exception as e:  # noqa: BLE001
            raise UpdateFailed(f"Error reading Storm Hedge state: {e}") from e

    storm_coordinator: DataUpdateCoordinator[dict] = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="franklinwh_storm_switch",
        update_method=_storm_update_data,
        update_interval=timedelta(minutes=5),
        always_update=False,
    )
    await storm_coordinator.async_refresh()
    async_add_entities([StormHedgeSwitch(storm_coordinator, prefix, gateway, _storm_client)])

# Is it chill to have a switch in here? We'll see!
class SmartCircuitSwitch(CoordinatorEntity, SwitchEntity):
    def __init__(self, prefix, unique_id, name, switches, client, coordinator):
        super().__init__(coordinator)
        self._is_on = False
        self.switches = switches
        self._attr_name = "{} {}".format(prefix, name)
        self.client = client
        self.coordinator = coordinator
        if unique_id:
            self._attr_has_entity_name = True
            self._attr_unique_id = unique_id + "_" + name

    @property
    def available(self) -> bool:
        _LOGGER.debug("Checking for switch availability")
        return self.coordinator.last_update_success and self.coordinator.data is not None

    @callback
    def _handle_coordinator_update(self) -> None:
        state = self.coordinator.data
        if state is None:
            _LOGGER.warning("Corrdinator data was None")
            # I think this should never happen, since it wouldn't be Available but here we are
            return
        values = list(map(lambda x: state[x], self.switches))
        if all(values):
            self._is_on = True
        elif all(map(lambda x: x is False, values)):
            self._is_on = False
        else:
            # Something's fucky!
            self._is_on = None
        self.async_write_ha_state()

    @property
    def is_on(self):
        """If the switch is currently on or off."""
        return self._is_on

    async def async_turn_on(self, **kwargs):
        """Turn the switch on."""
        switches = [None, None, None]
        for i in self.switches:
            switches[i] = True
        await self.client.set_smart_switch_state(switches)
        await self.coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):
        """Turn the switch off."""
        switches = [None, None, None]
        for i in self.switches:
            switches[i] = False
        await self.client.set_smart_switch_state(switches)
        await self.coordinator.async_refresh()


class StormHedgeSwitch(
    CoordinatorEntity[DataUpdateCoordinator[dict]], SwitchEntity
):
    """Switch to enable or disable Storm Hedge on the FranklinWH aGate."""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[dict],
        prefix: str,
        gateway: str,
        client: franklinwh.Client,
    ) -> None:
        """Initialise."""
        super().__init__(coordinator)
        self._client = client
        self._gateway = gateway
        self._attr_name = f"{prefix} Storm Hedge"
        self._attr_unique_id = f"{gateway}_storm_hedge_switch"

    @property
    def available(self) -> bool:
        """Entity is available when the coordinator has data."""
        return (
            self.coordinator.last_update_success
            and self.coordinator.data is not None
        )

    @property
    def is_on(self) -> bool | None:
        """Return True when Storm Hedge is enabled."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("enabled", False)

    async def _set_storm_hedge(self, enable: bool) -> None:
        """Call switchStorm on the gateway.

        Confirmed body format via MITM capture 2026-06-05:
        {"equipNo": "...", "stormEn": 0|1}
        """
        import json as _json  # noqa: PLC0415
        url = self._client.url_base + "hes-gateway/terminal/weather/switchStorm"
        body = {"equipNo": self._gateway, "stormEn": 1 if enable else 0}
        result = await self._client.session.post(
            url,
            params={"gatewayId": self._gateway, "lang": "en_US"},
            headers={"loginToken": self._client.token, "Content-Type": "application/json"},
            data=_json.dumps(body),
        )
        data = result.json()
        if data.get("code") != 200:
            _LOGGER.warning("switchStorm failed: %s", data)
        await self.coordinator.async_refresh()

    async def async_turn_on(self, **kwargs) -> None:
        """Enable Storm Hedge."""
        await self._set_storm_hedge(True)

    async def async_turn_off(self, **kwargs) -> None:
        """Disable Storm Hedge."""
        await self._set_storm_hedge(False)
