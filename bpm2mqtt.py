#!/usr/bin/env python3
"""SBM70 blood pressure monitor -> MQTT, with Home Assistant discovery.

The SBM70 advertises for ~60 s after a measurement. We connect (the monitor
must be bonded once with its on-screen passkey, see README), subscribe to
Blood Pressure Measurement (0x2A35), receive the stored records as
indications, and the monitor hangs up. Records are de-duplicated against a
retained MQTT topic and published as retained JSON per user.

Configuration via environment variables (see bpm2mqtt.env.example).
"""
import json
import logging
import os
import sys
from datetime import datetime

import dbus
import dbus.mainloop.glib
import paho.mqtt.client as mqtt
from gi.repository import GLib

from sbm70 import parse

MAC = os.environ.get('SBM70_MAC', '').upper()
ADAPTER = os.environ.get('BT_ADAPTER', 'hci0')
MQTT_HOST = os.environ.get('MQTT_HOST', 'localhost')
MQTT_PORT = int(os.environ.get('MQTT_PORT', '1883'))
MQTT_USER = os.environ.get('MQTT_USER')
MQTT_PASSWORD = os.environ.get('MQTT_PASSWORD')
MQTT_CLIENT_ID = os.environ.get('MQTT_CLIENT_ID', 'bpm2mqtt')
BASE = os.environ.get('MQTT_BASE_TOPIC', 'sbm70')
DISCOVERY_PREFIX = os.environ.get('HA_DISCOVERY_PREFIX', 'homeassistant')
DEVICE_NAME = os.environ.get('HA_DEVICE_NAME', 'SBM70')
CONNECT_RETRIES = 4
TRANSFER_TIMEOUT_S = 25
COOLDOWN_S = 30                          # ignore adverts right after a transfer

BLUEZ = 'org.bluez'
ADAPTER_PATH = f'/org/bluez/{ADAPTER}'
DEV_PATH = f"{ADAPTER_PATH}/dev_{MAC.replace(':', '_')}"
PROPS = 'org.freedesktop.DBus.Properties'
BP_SERVICE_UUID = '00001810-0000-1000-8000-00805f9b34fb'
BP_MEAS_UUID = '00002a35-0000-1000-8000-00805f9b34fb'
DEVICE_ID = 'sbm70_' + MAC.replace(':', '').lower()

log = logging.getLogger('bpm2mqtt')


class Bridge:
    def __init__(self):
        self.bus = dbus.SystemBus()
        self.adapter = dbus.Interface(self.bus.get_object(BLUEZ, ADAPTER_PATH), 'org.bluez.Adapter1')
        self.adapter_props = dbus.Interface(self.bus.get_object(BLUEZ, ADAPTER_PATH), PROPS)
        self.busy = False
        self.cooldown_until = 0
        self.attempt = 0
        self.records = []
        self.char_signal = None
        self.timeout_id = None
        self.last = {}                   # "user1" -> ISO measured_at of the last published record
        self.announced = set()
        self.bus.add_signal_receiver(self.on_dev_props, signal_name='PropertiesChanged',
                                     dbus_interface=PROPS, path=DEV_PATH)

        self.mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=MQTT_CLIENT_ID)
        if MQTT_USER:
            self.mqtt.username_pw_set(MQTT_USER, MQTT_PASSWORD)
        self.mqtt.will_set(f'{BASE}/status', 'offline', qos=1, retain=True)
        self.mqtt.on_connect = self.on_mqtt_connect
        self.mqtt.on_message = self.on_mqtt_message
        self.mqtt.reconnect_delay_set(1, 60)
        self.mqtt.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
        self.mqtt.loop_start()

    # ---------- MQTT ----------
    def on_mqtt_connect(self, client, userdata, flags, reason, props):
        log.info('MQTT connected (%s)', reason)
        client.subscribe(f'{BASE}/last', qos=1)
        client.publish(f'{BASE}/status', 'online', qos=1, retain=True)
        self.announce_bridge()
        for user in {1} | {int(k[4:]) for k in self.last} | set(self.announced):
            self.announce_user(user, force=True)

    def on_mqtt_message(self, client, userdata, msg):
        if msg.topic == f'{BASE}/last' and msg.payload:
            try:
                stored = json.loads(msg.payload)
                for k, v in stored.items():   # keep the newer of stored / in-memory
                    if k not in self.last or datetime.fromisoformat(v) > datetime.fromisoformat(self.last[k]):
                        self.last[k] = v
                log.info('last published: %s', self.last)
            except (ValueError, TypeError) as e:
                log.warning('bad %s/last payload: %s', BASE, e)

    def device_block(self):
        return {'identifiers': [DEVICE_ID], 'name': DEVICE_NAME, 'manufacturer': 'Sensiplast / HealthForYou',
                'model': 'SBM 70 blood pressure monitor', 'connections': [['bluetooth', MAC.lower()]]}

    def announce_bridge(self):
        cfg = {'name': 'Bridge', 'unique_id': f'{DEVICE_ID}_bridge', 'device_class': 'connectivity',
               'entity_category': 'diagnostic', 'state_topic': f'{BASE}/status',
               'payload_on': 'online', 'payload_off': 'offline', 'device': self.device_block()}
        self.mqtt.publish(f'{DISCOVERY_PREFIX}/binary_sensor/{DEVICE_ID}_bridge/config',
                          json.dumps(cfg), qos=1, retain=True)

    def announce_user(self, user, force=False):
        if user in self.announced and not force:
            return
        self.announced.add(user)
        st = f'{BASE}/user{user}'
        dev = self.device_block()
        sensors = [
            ('systolic', 'Systolic', {'unit_of_measurement': 'mmHg', 'state_class': 'measurement', 'icon': 'mdi:heart-pulse'}),
            ('diastolic', 'Diastolic', {'unit_of_measurement': 'mmHg', 'state_class': 'measurement', 'icon': 'mdi:heart-pulse'}),
            ('pulse', 'Pulse', {'unit_of_measurement': 'bpm', 'state_class': 'measurement', 'icon': 'mdi:heart'}),
            ('measured_at', 'Measured at', {'device_class': 'timestamp'}),
        ]
        for key, label, extra in sensors:
            uid = f'{DEVICE_ID}_user{user}_{key}'
            cfg = {'name': f'User {user} {label.lower()}', 'unique_id': uid, 'state_topic': st,
                   'value_template': f'{{{{ value_json.{key} }}}}', 'device': dev, **extra}
            if key == 'systolic':
                cfg['json_attributes_topic'] = st
            self.mqtt.publish(f'{DISCOVERY_PREFIX}/sensor/{uid}/config', json.dumps(cfg), qos=1, retain=True)
        uid = f'{DEVICE_ID}_user{user}_irregular_heartbeat'
        cfg = {'name': f'User {user} irregular heartbeat', 'unique_id': uid, 'state_topic': st,
               'value_template': "{{ 'ON' if value_json.irregular_heartbeat else 'OFF' }}",
               'icon': 'mdi:heart-flash', 'device_class': 'problem', 'device': dev}
        self.mqtt.publish(f'{DISCOVERY_PREFIX}/binary_sensor/{uid}/config', json.dumps(cfg), qos=1, retain=True)

    def publish(self, recs):
        recs = sorted(recs, key=lambda r: datetime.fromisoformat(r['measured_at']))
        sent = 0
        for r in recs:
            key = f"user{r['user']}"
            if key in self.last and datetime.fromisoformat(r['measured_at']) <= datetime.fromisoformat(self.last[key]):
                log.info('skip already published %s %s', key, r['measured_at'])
                continue
            self.announce_user(r['user'])
            self.mqtt.publish(f'{BASE}/{key}', json.dumps(r), qos=1, retain=True)
            self.last[key] = r['measured_at']
            sent += 1
            log.info('published %s %s', key, r['measured_at'])
        if sent:
            self.mqtt.publish(f'{BASE}/last', json.dumps(self.last), qos=1, retain=True)

    # ---------- BlueZ ----------
    def start_discovery(self):
        if self.busy:
            return
        try:
            if not self.adapter_props.Get('org.bluez.Adapter1', 'Discovering'):
                self.adapter.SetDiscoveryFilter({'Transport': 'le', 'DuplicateData': True,
                                                 'UUIDs': dbus.Array([BP_SERVICE_UUID], signature='s')})
                self.adapter.StartDiscovery()
                log.info('discovery started')
        except dbus.DBusException as e:
            log.warning('start discovery: %s', e.get_dbus_message())

    def stop_discovery(self):
        try:
            self.adapter.StopDiscovery()
        except dbus.DBusException:
            pass

    def watchdog(self):
        self.start_discovery()           # recovers from bluetoothd restarts / adapter resets
        return True

    def dev(self, iface='org.bluez.Device1'):
        return dbus.Interface(self.bus.get_object(BLUEZ, DEV_PATH), iface)

    def on_dev_props(self, iface, changed, invalidated):
        if iface != 'org.bluez.Device1':
            return
        if 'Connected' in changed and not changed['Connected'] and self.busy:
            log.info('monitor disconnected')
            self.finish()
        elif changed.get('ServicesResolved') and self.busy and self.char_signal is None:
            self.subscribe()
        elif ('RSSI' in changed or 'ManufacturerData' in changed) and not self.busy:
            if GLib.get_monotonic_time() < self.cooldown_until:
                return
            log.info('SBM70 advertising (RSSI %s), connecting', changed.get('RSSI'))
            self.busy = True
            self.attempt = 0
            self.records = []
            self.stop_discovery()
            self.timeout_id = GLib.timeout_add_seconds(TRANSFER_TIMEOUT_S + 30, self.on_timeout)
            self.connect()

    def connect(self):
        self.attempt += 1
        self.dev().Connect(reply_handler=self.on_connected, error_handler=self.on_connect_error, timeout=20)
        return False

    def on_connected(self):
        log.info('connected (attempt %d)', self.attempt)
        if self.dev(PROPS).Get('org.bluez.Device1', 'ServicesResolved') and self.char_signal is None:
            self.subscribe()

    def on_connect_error(self, e):
        log.warning('connect attempt %d failed: %s', self.attempt, e.get_dbus_message())
        if self.attempt < CONNECT_RETRIES and self.busy:
            GLib.timeout_add(500, self.connect)
        else:
            self.finish()

    def subscribe(self):
        om = dbus.Interface(self.bus.get_object(BLUEZ, '/'), 'org.freedesktop.DBus.ObjectManager')
        char_path = next((p for p, ifs in om.GetManagedObjects().items()
                          if p.startswith(DEV_PATH + '/') and 'org.bluez.GattCharacteristic1' in ifs
                          and ifs['org.bluez.GattCharacteristic1']['UUID'] == BP_MEAS_UUID), None)
        if not char_path:
            log.error('0x2A35 not found')
            self.disconnect()
            return
        self.char_signal = self.bus.add_signal_receiver(self.on_char_props, signal_name='PropertiesChanged',
                                                        dbus_interface=PROPS, path=char_path)
        char = dbus.Interface(self.bus.get_object(BLUEZ, char_path), 'org.bluez.GattCharacteristic1')
        char.StartNotify(reply_handler=lambda: log.info('subscribed to 0x2A35'),
                         error_handler=lambda e: (log.error('StartNotify: %s', e.get_dbus_message()), self.disconnect()))

    def on_char_props(self, iface, changed, invalidated):
        if 'Value' in changed:
            self.records.append(bytes(changed['Value']))

    def disconnect(self):
        try:
            self.dev().Disconnect(timeout=10)
        except dbus.DBusException:
            self.finish()

    def on_timeout(self):
        log.warning('transfer timeout, disconnecting')
        self.timeout_id = None
        self.disconnect()
        GLib.timeout_add_seconds(5, lambda: (self.busy and self.finish(), False)[1])
        return False

    def finish(self):
        if not self.busy:
            return
        if self.timeout_id:
            GLib.source_remove(self.timeout_id)
            self.timeout_id = None
        if self.char_signal is not None:
            self.char_signal.remove()
            self.char_signal = None
        recs = [r for r in (parse(b) for b in self.records) if r]
        log.info('transfer done: %d indications, %d records', len(self.records), len(recs))
        if recs:
            self.publish(recs)
        self.records = []
        self.busy = False
        self.cooldown_until = GLib.get_monotonic_time() + COOLDOWN_S * 1_000_000
        GLib.timeout_add_seconds(2, lambda: (self.start_discovery(), False)[1])


def main():
    logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO'), stream=sys.stdout,
                        format='%(levelname)s %(message)s')
    if len(MAC) != 17:
        sys.exit('SBM70_MAC is not set (e.g. SBM70_MAC=AA:BB:CC:DD:EE:FF), see README')
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bridge = Bridge()
    bridge.start_discovery()
    GLib.timeout_add_seconds(60, bridge.watchdog)
    GLib.MainLoop().run()


if __name__ == '__main__':
    main()
