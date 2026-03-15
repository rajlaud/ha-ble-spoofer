"""Constants for the BLE Spoofer integration."""

DOMAIN = "ble_spoofer"

# Scanner source identifier used internally
SCANNER_SOURCE = "ble_spoofer"
SCANNER_ADAPTER = "ble_spoofer"

# Service name
SERVICE_INJECT = "inject"

# Service field names
ATTR_ADDRESS = "address"
ATTR_NAME = "name"
ATTR_RSSI = "rssi"
ATTR_CONNECTABLE = "connectable"
ATTR_MANUFACTURER_DATA = "manufacturer_data"
ATTR_SERVICE_DATA = "service_data"
ATTR_SERVICE_UUIDS = "service_uuids"
ATTR_SOURCE = "source"

# Defaults
DEFAULT_RSSI = -60
DEFAULT_CONNECTABLE = False
DEFAULT_SOURCE = "ble_spoofer"
