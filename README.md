# BLE Spoofer

A Home Assistant custom integration that lets you **inject arbitrary Bluetooth LE advertisements** into a running HA instance.  No real BLE hardware required.

> **Intended use:** development, testing, and debugging only.  Do not run in production.

---

## What it does

Home Assistant's Bluetooth integration exposes an _external scanner_ API that allows code to push `BluetoothServiceInfoBleak` objects directly into HA's Bluetooth manager.  BLE Spoofer exploits this mechanism:

1. At startup it registers a virtual ("spoofed") scanner so HA's manager accepts injections.
2. It exposes a service **`ble_spoofer.inject`** that accepts advertisement parameters, builds the required `BLEDevice` / `AdvertisementData` / `BluetoothServiceInfoBleak` objects, and fires them through the Bluetooth manager's advertisement callback.
3. Any integration that uses `async_step_bluetooth` (or listens for BLE advertisements) will receive the injected advertisement as if it came from real hardware.

The canonical use case: you run HA Core in a devcontainer, have no BLE adapter, but want a device (e.g. `victron_ble`) to appear under **Settings → Devices & services → Discovered**.

---

## Installation

### Option A – Manual (recommended for dev)

1. Copy the `custom_components/ble_spoofer` directory into your HA `config/custom_components/` folder.
2. Restart Home Assistant.

### Option B – HACS (Manual repository)

1. In HACS → **Integrations** → ⋮ → **Custom repositories**, add `https://github.com/rajlaud/ha-ble-spoofer` with category **Integration**.
2. Install "BLE Spoofer" from the HACS store.
3. Restart Home Assistant.

---

## Enabling the integration

Add the following to your `configuration.yaml`:

```yaml
ble_spoofer:
```

Restart HA.  You will see a log entry:

```
INFO (MainThread) [custom_components.ble_spoofer] BLE Spoofer integration loaded.
Use ble_spoofer.inject to inject advertisements.
```

---

## Service: `ble_spoofer.inject`

Call this service from **Developer Tools → Services** (or automations, scripts) to inject an advertisement.

### Parameters

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `address` | string | ✅ | — | MAC address (`AA:BB:CC:DD:EE:FF` or `AA-BB-CC-DD-EE-FF`) |
| `name` | string | | `""` | Device local name |
| `rssi` | int | | `-60` | Signal strength in dBm (−127 … 20) |
| `connectable` | bool | | `false` | Whether to mark the device as connectable |
| `manufacturer_data` | mapping | | `{}` | Manufacturer ID → bytes (see encoding below) |
| `service_data` | mapping | | `{}` | Service UUID → bytes (see encoding below) |
| `service_uuids` | list | | `[]` | List of advertised service UUIDs |
| `source` | string | | `"ble_spoofer"` | Scanner source label (rarely needs changing) |

### Byte-string encodings

Both `manufacturer_data` values and `service_data` values accept the following formats:

| Format | Example |
|---|---|
| Plain hex | `"0102030405"` |
| Hex with `0x` prefix | `"0x0102030405"` |
| Hex with `:` separators | `"01:02:03:04:05"` |
| Hex with `-` separators | `"01-02-03-04-05"` |
| Standard base64 | `"AQIDBA=="` |
| base64 without padding | `"AQIDBA"` |

### UUID formats (service_data keys and service_uuids)

| Format | Example | Notes |
|---|---|---|
| Full 128-bit | `"0000fe95-0000-1000-8000-00805f9b34fb"` | Standard Bluetooth SIG form |
| Short 16-bit (hex string) | `"fe95"` | Expanded automatically to full form |
| Short 16-bit (0x-prefixed) | `"0xfe95"` | Expanded automatically to full form |

### Manufacturer ID formats (manufacturer_data keys)

| Format | Example |
|---|---|
| Integer | `741` |
| 0x-prefixed hex string | `"0x02E1"` |
| Decimal string | `"741"` |

---

## Example service calls

### Minimal advertisement (discovery by service UUID)

```yaml
service: ble_spoofer.inject
data:
  address: "AA:BB:CC:DD:EE:FF"
  name: "My BLE Device"
  rssi: -65
  service_uuids:
    - "0000fe95-0000-1000-8000-00805f9b34fb"
```

### victron_ble discovery advertisement

Victron devices advertise with manufacturer ID `0x02E1` (737).  Substitute the
actual manufacturer bytes from a real device capture or Victron documentation.

```yaml
service: ble_spoofer.inject
data:
  address: "D8:0D:17:12:34:56"
  name: "SmartSolar HQ12345AB"
  rssi: -55
  manufacturer_data:
    0x02E1: "1000000000000000000000000000000000000000000000000000000000000000"
```

### Full example with service data and manufacturer data

```yaml
service: ble_spoofer.inject
data:
  address: "AA:BB:CC:DD:EE:01"
  name: "Test Device"
  rssi: -70
  connectable: false
  manufacturer_data:
    # decimal key, hex bytes
    741: "0102030405060708"
  service_data:
    # short UUID, base64 payload
    "fe95": "AQIDBA=="
  service_uuids:
    - "0000fe95-0000-1000-8000-00805f9b34fb"
    - "0000180f-0000-1000-8000-00805f9b34fb"
  source: "ble_spoofer"
```

### Simulate device going out of range

Simply stop calling `inject` for that address.  HA's Bluetooth manager will
eventually mark the device as unavailable based on its stale-advertisement
timeout.  You can also inject with a different address to simulate a second
device.

---

## How discovery works

1. You call `ble_spoofer.inject` with the correct service UUIDs / manufacturer
   data for a target integration (e.g. `victron_ble`).
2. HA's Bluetooth manager receives the advertisement and runs it through its
   integration matching logic.
3. If the advertisement matches the discovery criteria of an installed
   integration, that integration's `async_step_bluetooth` config-flow step is
   called and the device appears under **Settings → Devices & services →
   Discovered**.
4. You can then proceed through the config flow exactly as with real hardware.

To find out what advertisement data an integration expects, check its
`bluetooth` key in `manifest.json` (service UUID matchers) or look at its
`async_step_bluetooth` / parser code.

---

## Troubleshooting

* **Nothing appears in Discovered** – the advertisement data doesn't match the
  target integration's matcher.  Enable `DEBUG` logging for
  `custom_components.ble_spoofer` and `homeassistant.components.bluetooth` to
  see what is being sent and matched.

  ```yaml
  # configuration.yaml
  logger:
    default: warning
    logs:
      custom_components.ble_spoofer: debug
      homeassistant.components.bluetooth: debug
  ```

* **"Invalid MAC address"** – use the `AA:BB:CC:DD:EE:FF` format with colons.

* **"Cannot decode bytes"** – check the encoding section above; mixed
  hex/base64 in a single value is not supported.

* **"Manufacturer ID out of range"** – Bluetooth manufacturer IDs must be
  0–65535 (16-bit unsigned).

---

## Security & privacy considerations

* BLE Spoofer injects **fake** device information.  Any integration that acts
  on those advertisements will behave as if a real device is present.
* Do **not** enable this integration in a production environment or expose its
  services externally.
* The service does not transmit any RF signals; it only manipulates in-memory
  data structures within the HA process.
* Remove the `ble_spoofer:` line from `configuration.yaml` when not needed.

---

## License

MIT
