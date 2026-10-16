"""Parser tests with synthetic records (not real measurements). Run: python -m unittest"""
import os
import struct
import unittest

os.environ['MONITOR_TZ'] = 'Europe/Berlin'
import sbm70  # noqa: E402


def record(sys_, dia, pulse, ts, user=0, status=0, flags=0x1E):
    y, mo, d, h, mi, s = ts
    return (bytes([flags]) + struct.pack('<HHH', sys_, dia, 0) + struct.pack('<HBBBBB', y, mo, d, h, mi, s)
            + struct.pack('<H', pulse) + bytes([user]) + struct.pack('<H', status))


class ParseTest(unittest.TestCase):
    def test_full_record(self):
        r = sbm70.parse(record(120, 80, 70, (2026, 1, 2, 7, 30, 0)))
        self.assertEqual((r['systolic'], r['diastolic'], r['pulse'], r['user']), (120, 80, 70, 1))
        self.assertEqual(r['measured_at'], '2026-01-02T07:30:00+01:00')
        self.assertNotIn('map', r)
        self.assertFalse(r['irregular_heartbeat'])

    def test_summer_time_offset(self):
        r = sbm70.parse(record(118, 76, 64, (2026, 7, 1, 18, 5, 0)))
        self.assertEqual(r['measured_at'], '2026-07-01T18:05:00+02:00')

    def test_status_bits_and_user2(self):
        r = sbm70.parse(record(135, 88, 81, (2026, 3, 4, 9, 0, 0), user=1, status=0x4004))
        self.assertEqual(r['user'], 2)
        self.assertTrue(r['irregular_heartbeat'])
        self.assertFalse(r['body_movement'])
        self.assertEqual(r['status'], 0x4004)

    def test_kpa_and_missing_timestamp_rejected(self):
        self.assertIsNone(sbm70.parse(record(16, 10, 70, (2026, 1, 1, 0, 0, 0), flags=0x1F)))
        self.assertIsNone(sbm70.parse(bytes([0x00]) + struct.pack('<HHH', 120, 80, 0)))

    def test_sfloat(self):
        self.assertEqual(sbm70.sfloat(0x0078), 120)
        self.assertEqual(sbm70.sfloat(0xF4D2), 123.4)   # exponent -1
        self.assertEqual(sbm70.sfloat(0x0FFF), -1)      # negative mantissa


if __name__ == '__main__':
    unittest.main()
