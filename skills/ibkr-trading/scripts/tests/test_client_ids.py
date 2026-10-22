#!/usr/bin/env python3

import multiprocessing
import os
import sys
import tempfile
import unittest


SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPT_DIR)

import _client_ids  # noqa: E402


def _try_slot(lock_dir, queue):
    try:
        with _client_ids.process_slot("test-role", lock_dir=lock_dir):
            queue.put("acquired")
    except _client_ids.ProcessSlotBusy:
        queue.put("busy")


class ClientIdMapTests(unittest.TestCase):
    def test_active_ids_are_unique_and_order_owners_are_fixed(self):
        mapping = _client_ids.validate_client_ids()
        self.assertEqual(len(mapping), len(set(mapping.values())))
        self.assertEqual(90, mapping["option_order"])
        self.assertEqual(91, mapping["stock_order"])
        self.assertFalse(set(mapping.values()) & _client_ids.RETIRED_CLIENT_IDS)

    def test_duplicate_and_order_owner_changes_fail_closed(self):
        duplicate = dict(_client_ids.CLIENT_IDS)
        duplicate["stock_status"] = duplicate["option_order"]
        with self.assertRaises(_client_ids.ClientIdConfigurationError):
            _client_ids.validate_client_ids(duplicate)

        moved_owner = dict(_client_ids.CLIENT_IDS)
        moved_owner["stock_order"] = 191
        with self.assertRaises(_client_ids.ClientIdConfigurationError):
            _client_ids.validate_client_ids(moved_owner)

    def test_process_slot_is_mutually_exclusive_and_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = multiprocessing.Queue()
            with _client_ids.process_slot("test-role", lock_dir=tmp):
                proc = multiprocessing.Process(target=_try_slot, args=(tmp, queue))
                proc.start()
                proc.join(5)
                self.assertEqual(0, proc.exitcode)
                self.assertEqual("busy", queue.get(timeout=1))

            with _client_ids.process_slot("test-role", lock_dir=tmp):
                pass


if __name__ == "__main__":
    unittest.main()
