# sbm70-mqtt

Get readings from the **Sensiplast / HealthForYou SBM 70** Bluetooth blood pressure monitor (sold by Lidl) into **Home Assistant**, with no phone or vendor app in the loop.

A small Python service on a Linux box with Bluetooth (a Raspberry Pi is ideal) waits for the monitor to advertise after a measurement. It then connects, pulls the stored readings and publishes them to MQTT with Home Assistant discovery. A reading shows up in HA about 3 seconds after the cuff deflates.

```
SBM70 ──BLE (bonded)── bpm2mqtt (Pi, BlueZ) ──MQTT── broker ── Home Assistant (MQTT discovery)
```

Why not an existing integration? The Medisana-style HA integrations match the same Bluetooth service. However, the SBM70 needs **passkey pairing** (a 6-digit code on its display), and that can't be done through an ESPHome Bluetooth proxy. This service uses the host's BlueZ stack directly, so a one-time `bluetoothctl` pairing is enough.

## What you get in Home Assistant

The device is called **SBM70** (from `HA_DEVICE_NAME`) and has these entities:

| Entity | |
|---|---|
| `sensor.sbm70_user_1_systolic`, `sensor.sbm70_user_1_diastolic` | mmHg, `state_class: measurement` (history + long-term statistics); the systolic sensor carries the full record as attributes |
| `sensor.sbm70_user_1_pulse` | bpm |
| `sensor.sbm70_user_1_measured_at` | timestamp from the monitor's clock (not the arrival time) |
| `binary_sensor.sbm70_user_1_irregular_heartbeat` | from the measurement status |
| `binary_sensor.sbm70_bridge` | is the service online (MQTT last will) |

The monitor has 2 user memories. The `user_2` entities appear automatically with the first reading taken under user 2.

[`homeassistant/`](homeassistant/) contains an optional twice-a-day routine: a dashboard plus counters and reminder automations. The reminders say "take your morning/afternoon readings" and clear themselves once the readings arrive.

## Requirements

- A Linux machine with Bluetooth LE and BlueZ ≥ 5.6x, within range of where you measure. Tested on a Raspberry Pi 3 B with Raspberry Pi OS (Debian 13 "Trixie"), BlueZ 5.82 and Python 3.13.
- An MQTT broker that Home Assistant uses (e.g. the Mosquitto add-on).
- The monitor must **not** stay paired to the HealthForYou app. Remove it from your phone first.

## Install

```sh
git clone https://github.com/bandiv/sbm70-mqtt && cd sbm70-mqtt
sudo ./install.sh                     # apt packages, /opt/bpm2mqtt, systemd unit (not started yet)
```

### Pairing (once)

Pairing uses a passkey, and the monitor shows it for only ~30 s, so do this at an interactive terminal:

```
$ bluetoothctl
[bluetooth]# agent KeyboardOnly
[bluetooth]# default-agent
[bluetooth]# scan le
        -- take a measurement; wait for: [NEW] Device AA:BB:CC:DD:EE:FF SBM70
[bluetooth]# pair AA:BB:CC:DD:EE:FF
        -- type the 6-digit code shown on the monitor
[bluetooth]# trust AA:BB:CC:DD:EE:FF
[bluetooth]# quit
```

Later connections reuse the bond and never ask for the code again.

### Configure and start

```sh
sudoedit /etc/default/bpm2mqtt        # SBM70_MAC=<address from above>, MQTT_HOST/USER/PASSWORD
sudo systemctl start bpm2mqtt
journalctl -u bpm2mqtt -f             # take a measurement and watch it arrive
```

All settings are listed in [`bpm2mqtt.env.example`](bpm2mqtt.env.example).

## MQTT topics

| Topic | Retained | Payload |
|---|---|---|
| `sbm70/status` | yes | `online` / `offline` (last will) |
| `sbm70/user<N>` | yes | latest reading, e.g. `{"systolic": 120, "diastolic": 80, "measured_at": "2026-01-02T07:30:00+01:00", "pulse": 70, "user": 1, "status": 0, "body_movement": false, "cuff_loose": false, "irregular_heartbeat": false, "raw": "…"}` |
| `sbm70/last` | yes | last published `measured_at` per user, used for de-duplication |
| `homeassistant/…/config` | yes | discovery |

**The monitor resends its whole memory on every transfer** (up to 120 readings per user), not just the new ones. The service publishes only readings newer than `sbm70/last`. If you delete that retained topic, everything is republished on the next transfer.

Each new reading is published as its own state change, so HA history and statistics show every reading.

## How the SBM70 talks (protocol notes)

These were found with `btmon` / `bluetoothctl`, in case you want to build something else:

- **Advertising:** the monitor advertises for **~60 s after each measurement** only, as `ADV_IND` with name `SBM70`. The advertisement includes the Blood Pressure service UUID `0x1810` and manufacturer data from company 1555 (Hans Dinslage GmbH, the Sanitas maker), payload `0100`. It uses a public address with a Telink chip OUI.
- **GATT is standard:** Generic Access, Generic Attribute, Device Information (`0x180A`), and Blood Pressure (`0x1810`), which contains Measurement `0x2A35` (indicate) and Feature `0x2A49`.
- **Security:** any write to a CCC descriptor fails with *Insufficient Authentication* until the monitor is bonded. The monitor's IO capability is *DisplayOnly* and it uses legacy pairing with MITM protection. That means a passkey shown on the monitor and typed in on the host. If pairing fails, the monitor shows **Er 7**.
- **Transfer:** after you subscribe to `0x2A35`, the monitor indicates every stored record within ~0.4 s and then disconnects (reason `0x13`).
- **Record format:** 19 bytes, standard Blood Pressure Measurement with flags `0x1E`:
  - flags = mmHg + timestamp + pulse + user ID + status
  - systolic, diastolic and MAP as SFLOAT; MAP is always 0
  - timestamp in local time without a time zone
  - pulse
  - user ID (0 = user 1)
  - measurement status: bit 2 = irregular pulse. Bit 14 (`0x4000`) is set on many readings and isn't in the spec (unknown, possibly a vendor flag). It's kept in `status`.

## Tips

- **Raspberry Pi Wi-Fi + Bluetooth share one chip** (Pi 3/4/Zero W). On a Pi 3, continuous scanning at BlueZ's default 100% duty cycle raised Wi-Fi ping from ~11 ms to ~62 ms and dropped SSH sessions. `install.sh` therefore sets the discovery scan to 25% (`ScanIntervalDiscovery=96`, `ScanWindowDiscovery=24` in `/etc/bluetooth/main.conf`). That brought ping back to ~20 ms, and adverts were still caught within a second. Set `LOW_SCAN_DUTY=0` to skip this.
- **Read-only root (overlayroot / `raspi-config` overlay FS):** the bond lives in `/var/lib/bluetooth/<adapter>/<device>/`. Pair with the overlay disabled, or copy that directory into the lower layer afterwards; otherwise the pairing is lost at reboot. The same applies to installing the service.
- **BlueZ D-Bus policy:** Debian's default policy only lets root talk to `org.bluez`, so the unit runs as root, sandboxed with systemd hardening options.
- **Readings missing:** check `journalctl -u bpm2mqtt`. "connect attempt failed" usually means the monitor was too far away or stopped advertising. Anything you missed is sent with the next measurement, because the monitor resends its memory.

## Tests

```sh
python -m unittest      # parser tests with synthetic records, no Bluetooth needed
```

## Disclaimer

This is a hobby project, not a medical device or medical software. Readings shown in Home Assistant may be delayed, missing or wrong. Rely on the monitor's own display and talk to a doctor about your blood pressure. It is not affiliated with Lidl, Sensiplast, HealthForYou or Hans Dinslage GmbH.

## License

MIT
