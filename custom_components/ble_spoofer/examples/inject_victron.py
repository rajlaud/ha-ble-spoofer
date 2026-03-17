#!/usr/bin/env python3
"""Inject Victron BLE advertisements via the ble_spoofer service.

Connects to a running Home Assistant instance over WebSocket,
authenticates with a long-lived access token (or a JWT derived
from a refresh token), and calls ble_spoofer.inject.

Usage:
    # Set your HA access token (create one in your HA profile)
    export HA_TOKEN="your_long_lived_access_token"

    # Inject a single valid VEBus advertisement
    python inject_victron.py

    # Inject bad advertisements to trigger reauth (3 consecutive failures)
    python inject_victron.py --bad --count 4

    # Inject a specific device type
    python inject_victron.py --device solar_charger

    # Connect to a different host
    python inject_victron.py --host 192.168.1.100 --port 8123

If you don't have a long-lived access token, you can generate a JWT
from a refresh token stored in .storage/auth (see --jwt-from-auth flag).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

try:
    import websockets
except ImportError:
    print("Install websockets: pip install websockets")
    sys.exit(1)


# --- Victron fixture data ---
# These are real encrypted BLE advertisement payloads from test fixtures.

DEVICES = {
    "vebus": {
        "address": "D8:0D:17:00:00:01",
        "name": "Inverter Charger",
        "manufacturer_data_hex": "100380270c1252dad26f0b8eb39162074d140df410",
        "token": "da3f5fa2860cb1cf86ba7a6d1d16b9dd",
    },
    "solar_charger": {
        "address": "D8:0D:17:00:00:02",
        "name": "Solar Charger",
        "manufacturer_data_hex": "100242a0016207adceb37b605d7e0ee21b24df5c",
        "token": "adeccb947395801a4dd45a2eaa44bf17",
    },
}

VICTRON_MANUFACTURER_ID = 737  # 0x02E1


async def inject(
    host: str,
    port: int,
    access_token: str,
    device_key: str,
    count: int,
    bad: bool,
    interval: float,
) -> None:
    """Connect to HA and inject advertisements."""
    device = DEVICES[device_key]
    uri = f"ws://{host}:{port}/api/websocket"

    async with websockets.connect(uri) as ws:
        # Auth handshake
        msg = json.loads(await ws.recv())
        if msg["type"] != "auth_required":
            print(f"Unexpected message: {msg['type']}")
            return

        await ws.send(json.dumps({"type": "auth", "access_token": access_token}))
        msg = json.loads(await ws.recv())
        if msg["type"] != "auth_ok":
            print(f"Auth failed: {msg}")
            return
        print(f"Authenticated to {host}:{port}")

        for i in range(count):
            if bad:
                # Keep device type header (first 5 bytes) but garble the rest
                raw = bytes.fromhex(device["manufacturer_data_hex"])
                payload_hex = raw[:5].hex() + os.urandom(len(raw) - 5).hex()
            else:
                payload_hex = device["manufacturer_data_hex"]

            msg_id = i + 1
            await ws.send(
                json.dumps(
                    {
                        "id": msg_id,
                        "type": "call_service",
                        "domain": "ble_spoofer",
                        "service": "inject",
                        "service_data": {
                            "address": device["address"],
                            "name": device["name"],
                            "rssi": -55,
                            "manufacturer_data": {
                                str(VICTRON_MANUFACTURER_ID): payload_hex
                            },
                        },
                    }
                )
            )
            msg = json.loads(await ws.recv())
            status = "ok" if msg.get("success") else f"FAILED: {msg.get('error')}"
            label = "bad" if bad else "good"
            print(f"  [{i + 1}/{count}] Injected {label} {device_key}: {status}")

            if i < count - 1:
                await asyncio.sleep(interval)

    print("Done.")
    if not bad:
        print(f"Encryption key for {device['name']}: {device['token']}")


async def get_jwt_from_auth(auth_path: str) -> str:
    """Generate a JWT access token from the HA auth store."""
    try:
        import jwt as pyjwt
    except ImportError:
        print("Install PyJWT: pip install PyJWT")
        sys.exit(1)

    with open(auth_path) as f:
        auth_data = json.load(f)

    for token in auth_data["data"]["refresh_tokens"]:
        if token["token_type"] == "normal":
            return pyjwt.encode(
                {
                    "iss": token["id"],
                    "iat": int(time.time()),
                    "exp": int(time.time()) + 1800,
                },
                token["jwt_key"],
                algorithm="HS256",
            )

    print("No normal refresh token found in auth store")
    sys.exit(1)


def main() -> None:
    """Parse args and run."""
    parser = argparse.ArgumentParser(
        description="Inject Victron BLE advertisements into Home Assistant"
    )
    parser.add_argument(
        "--host", default="localhost", help="HA host (default: localhost)"
    )
    parser.add_argument(
        "--port", type=int, default=8123, help="HA port (default: 8123)"
    )
    parser.add_argument(
        "--device",
        choices=list(DEVICES.keys()),
        default="vebus",
        help="Device type to inject (default: vebus)",
    )
    parser.add_argument(
        "--count", type=int, default=1, help="Number of advertisements to inject"
    )
    parser.add_argument(
        "--bad",
        action="store_true",
        help="Inject bad (undecryptable) advertisements to trigger reauth",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Seconds between injections (default: 1.0)",
    )
    parser.add_argument(
        "--jwt-from-auth",
        metavar="PATH",
        help="Generate JWT from HA .storage/auth file instead of using HA_TOKEN",
    )
    args = parser.parse_args()

    if args.jwt_from_auth:
        access_token = asyncio.run(get_jwt_from_auth(args.jwt_from_auth))
    else:
        access_token = os.environ.get("HA_TOKEN")
        if not access_token:
            print("Set HA_TOKEN env var or use --jwt-from-auth")
            sys.exit(1)

    asyncio.run(
        inject(
            host=args.host,
            port=args.port,
            access_token=access_token,
            device_key=args.device,
            count=args.count,
            bad=args.bad,
            interval=args.interval,
        )
    )


if __name__ == "__main__":
    main()
