"""Number platform for FranklinWH — per-mode battery reserve SOC control.

Exposes one NumberEntity per operating mode so users can read and set each
mode's battery reserve percentage directly from Home Assistant without leaving
the current operating mode.

The write path uses ``POST /hes-gateway/terminal/tou/updateSocV2`` (discovered
via MITM interception of the FranklinWH app), which updates the stored reserve
for a mode without activating that mode.

Emergency Backup always has ``editSocFlag = false`` from the API and is exposed
as a read-only sensor (native_min/max both set to 100, step 1 but can't write).
"""

from __future__ import annotations

from datetime import timedelta
import logging

import franklinwh
import voluptuous as vol

from homeassistant.components.number import (
    PLATFORM_SCHEMA as NUMBER_PLATFORM_SCHEMA,
    NumberEntity,
    NumberMode,
)
from homeassistant.const import CONF_ID, CONF_PASSWORD, CONF_USERNAME, PERCENTAGE
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

_LOGGER = logging.getLogger(__name__)
DEFAULT_UPDATE_INTERVAL = 30

PLATFORM_SCHEMA = NUMBER_PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Required(CONF_ID): cv.string,
        vol.Optional("prefix", default=False): cv.string,
        vol.Optional(
            "update_interval", default=DEFAULT_UPDATE_INTERVAL
        ): cv.time_period,
    }
)

# workMode int (from API) → human label used in entity names
_WORK_MODE_LABELS: dict[int, str] = {
    1: "Time of Use Reserve",
    2: "Self Consumption Reserve",
    3: "Emergency Backup Reserve",
}

# workMode ints that the API allows editing (editSocFlag = true)
_EDITABLE_WORK_MODES = {1, 2}


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the number platform."""
    username: str = config[CONF_USERNAME]
    password: str = config[CONF_PASSWORD]
    gateway: str = config[CONF_ID]
    update_interval: timedelta = config["update_interval"]

    if config["prefix"] and config["prefix"] != "False":
        prefix = config["prefix"]
    else:
        prefix = "FranklinWH"

    fetcher = franklinwh.TokenFetcher(username, password)
    client = franklinwh.Client(fetcher, gateway)

    async def _get_tou_list() -> dict:
        """Call getGatewayTouListV2 and return structured reserves.

        Inlined here so it works with older installed versions of the
        franklinwh PyPI package that predate the get_tou_list() method.
        Uses client._post() which has always been present.
        """
        url = client.url_base + "hes-gateway/terminal/tou/getGatewayTouListV2"
        result = (await client._post(url, "", params={"showType": "1"}))["result"]
        current_id = result["currendId"]
        reserves: dict[int, float] = {}
        current_work_mode: int | None = None
        for entry in result["list"]:
            reserves[entry["workMode"]] = entry["soc"]
            if entry["id"] == current_id:
                current_work_mode = entry["workMode"]
        return {
            "reserves": reserves,
            "current_work_mode": current_work_mode,
            "list": result["list"],
        }

    async def _update_data() -> dict[int, float]:
        """Fetch per-mode reserve SOC percentages from the gateway.

        Returns a dict mapping workMode int (1/2/3) → reserve SOC float.
        """
        _LOGGER.debug("Fetching TOU reserve list from FranklinWH")
        try:
            tou = await _get_tou_list()
            reserves = tou.get("reserves", {})
            if not reserves:
                raise UpdateFailed("get_tou_list() returned empty reserves")
            _LOGGER.debug("Reserves: %s", reserves)
            return reserves
        except franklinwh.client.DeviceTimeoutException as e:
            raise UpdateFailed(f"Device timeout: {e}") from e
        except franklinwh.client.GatewayOfflineException as e:
            raise UpdateFailed(f"Gateway offline: {e}") from e
        except franklinwh.client.AccountLockedException as e:
            raise UpdateFailed(f"Account locked: {e}") from e
        except franklinwh.client.InvalidCredentialsException as e:
            raise UpdateFailed(f"Invalid credentials: {e}") from e
        except UpdateFailed:
            raise
        except Exception as e:  # noqa: BLE001
            raise UpdateFailed(f"Error reading reserves: {e}") from e

    coordinator: DataUpdateCoordinator[dict[int, float]] = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="franklinwh_reserves",
        update_method=_update_data,
        update_interval=update_interval,
        always_update=False,
    )

    await coordinator.async_refresh()

    entities = [
        ReserveNumber(coordinator, prefix, gateway, client, work_mode, label)
        for work_mode, label in _WORK_MODE_LABELS.items()
    ]
    async_add_entities(entities)


class ReserveNumber(
    CoordinatorEntity[DataUpdateCoordinator[dict[int, float]]],
    NumberEntity,
):
    """Number entity exposing and controlling a single mode's reserve SOC."""

    _attr_native_min_value = 0.0
    _attr_native_max_value = 100.0
    _attr_native_step = 1.0
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[dict[int, float]],
        prefix: str,
        gateway: str,
        client: franklinwh.Client,
        work_mode: int,
        label: str,
    ) -> None:
        """Initialise."""
        super().__init__(coordinator)
        self._client = client
        self._work_mode = work_mode
        self._editable = work_mode in _EDITABLE_WORK_MODES
        self._attr_name = f"{prefix} {label}"
        self._attr_unique_id = f"{gateway}_{label.lower().replace(' ', '_')}"

    @property
    def available(self) -> bool:
        """Entity is available when the coordinator has data."""
        return (
            self.coordinator.last_update_success
            and self.coordinator.data is not None
            and self._work_mode in self.coordinator.data
        )

    @property
    def native_value(self) -> float | None:
        """Return the current reserve SOC for this mode."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self._work_mode)

    async def async_set_native_value(self, value: float) -> None:
        """Set a new battery reserve SOC for this mode."""
        if not self._editable:
            _LOGGER.warning(
                "%s is read-only (editSocFlag=false on the gateway)", self._attr_name
            )
            return

        soc = int(value)
        _LOGGER.info(
            "Setting %s reserve to %s%%", self._attr_name, soc
        )

        # Inline the updateSocV2 call so this works with older franklinwh
        # library versions that predate set_mode_reserve().
        url = self._client.url_base + "hes-gateway/terminal/tou/updateSocV2"
        result = await self._client._post(
            url,
            "",
            params={
                "workMode": str(self._work_mode),
                "electricityType": "1",
                "soc": str(soc),
            },
        )
        if result.get("code") != 200:
            raise RuntimeError(f"updateSocV2 failed: {result}")

        # Refresh so the displayed value matches what the gateway confirms.
        await self.coordinator.async_refresh()
