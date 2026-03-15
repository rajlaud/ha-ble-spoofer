"""BLE Spoofer – inject arbitrary BLE advertisements into Home Assistant.

This integration registers a fake external Bluetooth scanner so that HA's
Bluetooth manager accepts injected advertisements.  A ``ble_spoofer.inject``
service is exposed that lets the user craft and fire a single
``BluetoothServiceInfoBleak`` through the normal discovery pipeline without
any real BLE hardware.

Intended use: development / testing only.
"""

from __future__ import annotations

import binascii
import logging
import re
import time
from typing import Any

import voluptuous as vol
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from home_assistant_bluetooth import BluetoothServiceInfoBleak
from habluetooth import BaseHaScanner

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, ServiceCall, callback
import homeassistant.helpers.config_validation as cv

from .const import (
    ATTR_ADDRESS,
    ATTR_CONNECTABLE,
    ATTR_MANUFACTURER_DATA,
    ATTR_NAME,
    ATTR_RSSI,
    ATTR_SERVICE_DATA,
    ATTR_SERVICE_UUIDS,
    ATTR_SOURCE,
    DEFAULT_CONNECTABLE,
    DEFAULT_RSSI,
    DEFAULT_SOURCE,
    DOMAIN,
    SCANNER_ADAPTER,
    SCANNER_SOURCE,
    SERVICE_INJECT,
)

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regular expression helpers
# ---------------------------------------------------------------------------

_MAC_RE = re.compile(
    r"^([0-9A-Fa-f]{2}[:\-]){5}([0-9A-Fa-f]{2})$"
)

# ---------------------------------------------------------------------------
# Input-parsing helpers
# ---------------------------------------------------------------------------


def _validate_mac(address: str) -> str:
    """Return the normalised MAC address or raise ValueError."""
    address = address.strip().upper().replace("-", ":")
    if not _MAC_RE.match(address):
        raise vol.Invalid(
            f"Invalid MAC address '{address}'. "
            "Expected format: AA:BB:CC:DD:EE:FF"
        )
    return address


def _parse_bytes(raw: str, context: str) -> bytes:
    """Parse a hex (with or without 0x prefix / separators) or base64 byte string.

    Supported encodings
    -------------------
    * Plain hex (with or without ``0x`` prefix, with or without ``:``, ``-``, or
      space separators): ``"0102030405"`` ``"01:02:03:04"`` ``"0x0102030405"``
    * Standard base64 (with ``=`` padding or without): ``"AQIDBA=="``
    """
    value = raw.strip()

    # Try hex (strip optional 0x prefix and separators)
    hex_candidate = value
    if hex_candidate.lower().startswith("0x"):
        hex_candidate = hex_candidate[2:]
    hex_candidate = hex_candidate.replace(":", "").replace("-", "").replace(" ", "")

    if all(c in "0123456789abcdefABCDEF" for c in hex_candidate) and len(hex_candidate) % 2 == 0:
        try:
            return bytes.fromhex(hex_candidate)
        except ValueError:
            pass  # fall through to base64

    # Try base64
    try:
        # Add padding if needed
        padded = value + "=" * (-len(value) % 4)
        return binascii.a2b_base64(padded)
    except Exception:
        pass

    raise vol.Invalid(
        f"Cannot decode bytes for {context}: '{raw}'. "
        "Provide hex (e.g. '0102AABB' or '01:02:AA:BB') or base64 (e.g. 'AQID')."
    )


def _parse_manufacturer_data(raw: dict[Any, Any]) -> dict[int, bytes]:
    """Validate and convert manufacturer_data mapping to {int: bytes}."""
    result: dict[int, bytes] = {}
    for key, value in raw.items():
        # Accept int keys or string representations (decimal or 0x-hex)
        if isinstance(key, int):
            mfr_id = key
        elif isinstance(key, str):
            stripped = key.strip()
            try:
                mfr_id = int(stripped, 0)  # int() with base 0 accepts 0x…
            except ValueError:
                raise vol.Invalid(
                    f"Manufacturer data key '{key}' is not a valid integer. "
                    "Use a decimal integer (e.g. 741) or 0x-prefixed hex (e.g. 0x02E1)."
                )
        else:
            raise vol.Invalid(
                f"Manufacturer data key '{key}' must be an integer or string, "
                f"got {type(key).__name__}."
            )

        if not 0 <= mfr_id <= 0xFFFF:
            raise vol.Invalid(
                f"Manufacturer ID {mfr_id} is out of range (must be 0–65535)."
            )

        result[mfr_id] = _parse_bytes(str(value), f"manufacturer_data[{key}]")

    return result


def _parse_service_data(raw: dict[Any, Any]) -> dict[str, bytes]:
    """Validate and convert service_data mapping to {uuid_str: bytes}."""
    result: dict[str, bytes] = {}
    for key, value in raw.items():
        uuid_str = str(key).strip().lower()
        # Normalise short UUIDs like "181C" -> full 128-bit form
        if re.match(r"^[0-9a-f]{4}$", uuid_str):
            uuid_str = f"0000{uuid_str}-0000-1000-8000-00805f9b34fb"
        elif re.match(r"^0x[0-9a-f]{4}$", uuid_str):
            uuid_str = f"0000{uuid_str[2:]}-0000-1000-8000-00805f9b34fb"
        # Validate full UUID
        if not re.match(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            uuid_str,
        ):
            raise vol.Invalid(
                f"Service data key '{key}' is not a valid UUID. "
                "Use a full 128-bit UUID (xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx) "
                "or a 16-bit short UUID (e.g. '181C' or '0x181C')."
            )
        result[uuid_str] = _parse_bytes(str(value), f"service_data[{key}]")

    return result


def _validate_service_uuids(raw: list[Any]) -> list[str]:
    """Validate and normalise a list of service UUIDs."""
    result: list[str] = []
    for item in raw:
        uuid_str = str(item).strip().lower()
        if re.match(r"^[0-9a-f]{4}$", uuid_str):
            uuid_str = f"0000{uuid_str}-0000-1000-8000-00805f9b34fb"
        elif re.match(r"^0x[0-9a-f]{4}$", uuid_str):
            uuid_str = f"0000{uuid_str[2:]}-0000-1000-8000-00805f9b34fb"
        if not re.match(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            uuid_str,
        ):
            raise vol.Invalid(
                f"Service UUID '{item}' is not valid. "
                "Use a full 128-bit UUID or a 16-bit short UUID."
            )
        result.append(uuid_str)
    return result


# ---------------------------------------------------------------------------
# Voluptuous schema for the inject service
# ---------------------------------------------------------------------------

INJECT_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ADDRESS): vol.All(cv.string, _validate_mac),
        vol.Optional(ATTR_NAME, default=""): cv.string,
        vol.Optional(ATTR_RSSI, default=DEFAULT_RSSI): vol.All(
            vol.Coerce(int), vol.Range(min=-127, max=20)
        ),
        vol.Optional(ATTR_CONNECTABLE, default=DEFAULT_CONNECTABLE): cv.boolean,
        vol.Optional(ATTR_MANUFACTURER_DATA, default={}): vol.All(
            dict, _parse_manufacturer_data
        ),
        vol.Optional(ATTR_SERVICE_DATA, default={}): vol.All(
            dict, _parse_service_data
        ),
        vol.Optional(ATTR_SERVICE_UUIDS, default=[]): vol.All(
            list, _validate_service_uuids
        ),
        vol.Optional(ATTR_SOURCE, default=DEFAULT_SOURCE): cv.string,
    }
)


# ---------------------------------------------------------------------------
# Fake scanner
# ---------------------------------------------------------------------------


class _SpoofScanner(BaseHaScanner):
    """A no-hardware external scanner used only to satisfy HA's scanner registry.

    We never actually scan; advertisements are injected manually via the
    ``ble_spoofer.inject`` service.
    """

    def __init__(self) -> None:
        super().__init__(source=SCANNER_SOURCE, adapter=SCANNER_ADAPTER)

    @property
    def discovered_devices(self) -> list[BLEDevice]:
        """Return an empty device list – we don't track devices."""
        return []

    @property
    def discovered_devices_and_advertisement_data(
        self,
    ) -> dict[str, tuple[BLEDevice, AdvertisementData]]:
        """Return an empty mapping – we don't track advertisement data."""
        return {}


# ---------------------------------------------------------------------------
# Integration setup
# ---------------------------------------------------------------------------


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the BLE Spoofer integration."""
    scanner = _SpoofScanner()

    # Register the fake scanner so HA's Bluetooth manager accepts injections.
    cancel_scanner = bluetooth.async_register_scanner(hass, scanner)

    # Obtain the advertisement callback used to push service info into HA.
    adv_callback = bluetooth.async_get_advertisement_callback(hass)

    # Store cleanup handles.
    hass.data[DOMAIN] = {"cancel_scanner": cancel_scanner}

    @callback
    def _handle_inject(call: ServiceCall) -> None:
        """Handle the ble_spoofer.inject service call."""
        data = call.data

        address: str = data[ATTR_ADDRESS]
        name: str = data[ATTR_NAME]
        rssi: int = data[ATTR_RSSI]
        connectable: bool = data[ATTR_CONNECTABLE]
        manufacturer_data: dict[int, bytes] = data[ATTR_MANUFACTURER_DATA]
        service_data: dict[str, bytes] = data[ATTR_SERVICE_DATA]
        service_uuids: list[str] = data[ATTR_SERVICE_UUIDS]
        source: str = data[ATTR_SOURCE]

        _LOGGER.debug(
            "Injecting BLE advertisement: address=%s name=%r rssi=%d "
            "manufacturer_data keys=%s service_uuids=%s",
            address,
            name,
            rssi,
            list(manufacturer_data.keys()),
            service_uuids,
        )

        device = BLEDevice(
            address=address,
            name=name if name else None,
            details={},
            rssi=rssi,
        )

        advertisement = AdvertisementData(
            local_name=name if name else None,
            manufacturer_data=manufacturer_data,
            service_data=service_data,
            service_uuids=service_uuids,
            tx_power=None,
            rssi=rssi,
            platform_data=(),
        )

        service_info = BluetoothServiceInfoBleak(
            name=name or address,
            address=address,
            rssi=rssi,
            manufacturer_data=manufacturer_data,
            service_data=service_data,
            service_uuids=service_uuids,
            source=source,
            device=device,
            advertisement=advertisement,
            connectable=connectable,
            time=time.monotonic(),
        )

        adv_callback(service_info)

    hass.services.async_register(
        DOMAIN,
        SERVICE_INJECT,
        _handle_inject,
        schema=INJECT_SERVICE_SCHEMA,
    )

    _LOGGER.info("BLE Spoofer integration loaded. Use %s.%s to inject advertisements.", DOMAIN, SERVICE_INJECT)
    return True


async def async_unload(hass: HomeAssistant) -> bool:
    """Unload the BLE Spoofer integration."""
    stored = hass.data.pop(DOMAIN, {})
    cancel_scanner = stored.get("cancel_scanner")
    if cancel_scanner is not None:
        cancel_scanner()

    hass.services.async_remove(DOMAIN, SERVICE_INJECT)
    _LOGGER.info("BLE Spoofer integration unloaded.")
    return True
