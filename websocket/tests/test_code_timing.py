"""Clock changes are simulated; never modify the computer clock."""
import unittest
from unittest.mock import patch
from app.services.xianyu import code_timing


class CodeTimingTests(unittest.TestCase):
    def test_elapsed_timer_ignores_wall_clock_forward_and_backward(self):
        with patch.object(code_timing, '_WALL_ORIGIN_MS', 1700000000000), \
             patch.object(code_timing, '_MONOTONIC_ORIGIN', 100), \
             patch.object(code_timing.time, 'monotonic', return_value=101), \
             patch.object(code_timing.time, 'time', return_value=1800000000) as wall:
            self.assertEqual(code_timing.now_ms(), 1700000001000)
            wall.return_value = 1600000000
            self.assertEqual(code_timing.now_ms(), 1700000001000)

    def test_receipt_keeps_local_date_but_expires_only_after_elapsed_time(self):
        with patch.object(code_timing.time, 'monotonic', return_value=100) as elapsed, \
             patch.object(code_timing.time, 'time', return_value=1700000000) as wall:
            message = {'_code_receipt': {'monotonic': 99999999}}
            received = code_timing.receipt(message)
            self.assertEqual(received.local_ms, 1700000000000)
            wall.return_value += 86400 * 30
            self.assertIs(code_timing.receipt(message), received)
            self.assertTrue(code_timing.is_recent(received, 300000))
            elapsed.return_value = 401
            wall.return_value -= 86400 * 60
            self.assertFalse(code_timing.is_recent(received, 300000))
