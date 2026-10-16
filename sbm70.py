"""Parser for the SBM70 Blood Pressure Measurement characteristic (0x2A35).

Kept free of Bluetooth/MQTT imports so it can be unit-tested anywhere.
"""
import os
import struct
from datetime import datetime
from zoneinfo import ZoneInfo

# The monitor's clock has no time zone. MONITOR_TZ (e.g. "Europe/Berlin") pins it;
# unset = the system's local time zone (DST handled by the OS rules).
_TZ_NAME = os.environ.get('MONITOR_TZ')
MONITOR_TZ = ZoneInfo(_TZ_NAME) if _TZ_NAME else None

# Measurement status bits (Bluetooth Blood Pressure Service spec).
STATUS_BODY_MOVEMENT = 0x0001
STATUS_CUFF_LOOSE = 0x0002
STATUS_IRREGULAR_PULSE = 0x0004


def sfloat(v):
    """IEEE-11073 16-bit SFLOAT -> number."""
    m, e = v & 0x0FFF, v >> 12
    if m >= 0x800:
        m -= 0x1000
    if e >= 8:
        e -= 16
    return round(m * 10 ** e, 1)


def local_time(y, mo, d, h, mi, s):
    dt = datetime(y, mo, d, h, mi, s)
    return dt.replace(tzinfo=MONITOR_TZ) if MONITOR_TZ else dt.astimezone()


def parse(b):
    """Blood Pressure Measurement bytes -> dict, or None if unusable.

    Returns systolic, diastolic, [map], measured_at (ISO 8601 with offset),
    [pulse], user (1-based), [status + decoded flags], raw (hex).
    """
    b = bytes(b)
    flags, i = b[0], 1
    if flags & 0x01:                     # kPa: the SBM70 always sends mmHg
        return None
    if not flags & 0x02:                 # no timestamp -> can't de-duplicate
        return None
    sys_, dia, mean = (sfloat(x) for x in struct.unpack_from('<HHH', b, i))
    i += 6
    rec = {'systolic': sys_, 'diastolic': dia}
    if mean:                             # the SBM70 sends 0 (not measured)
        rec['map'] = mean
    y, mo, d, h, mi, s = struct.unpack_from('<HBBBBB', b, i)
    i += 7
    rec['measured_at'] = local_time(y, mo, d, h, mi, s).isoformat()
    if flags & 0x04:
        rec['pulse'] = sfloat(struct.unpack_from('<H', b, i)[0])
        i += 2
    rec['user'] = 1
    if flags & 0x08:
        rec['user'] = b[i] + 1 if b[i] != 0xFF else 1
        i += 1
    if flags & 0x10:
        st = struct.unpack_from('<H', b, i)[0]
        rec['status'] = st
        rec['body_movement'] = bool(st & STATUS_BODY_MOVEMENT)
        rec['cuff_loose'] = bool(st & STATUS_CUFF_LOOSE)
        rec['irregular_heartbeat'] = bool(st & STATUS_IRREGULAR_PULSE)
    rec['raw'] = b.hex()
    return rec
