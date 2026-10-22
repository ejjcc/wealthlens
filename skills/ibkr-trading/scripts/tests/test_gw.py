#!/usr/bin/env python3

import asyncio
import os
import sys
import unittest
from unittest import mock


SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPT_DIR)

import _gw  # noqa: E402


class FakeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def emit(self):
        for handler in list(self.handlers):
            handler()


class FakeIB:
    should_fail = False

    def __init__(self):
        self.disconnectedEvent = FakeEvent()
        self.connection = None

    async def connectAsync(self, host, port, clientId, readonly):
        self.connection = (host, port, clientId, readonly)
        if self.should_fail:
            raise RuntimeError("connect failed")
        return self

    async def reqPositionsAsync(self):
        return []

    def disconnect(self):
        self.disconnectedEvent.emit()


class FakeSlot:
    def __init__(self):
        self.entered = 0
        self.exited = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *_args):
        self.exited += 1


class GatewayClientSlotTests(unittest.TestCase):
    def test_named_role_holds_slot_until_disconnect(self):
        slot = FakeSlot()
        with (
            mock.patch.object(_gw, "_check_alive", return_value=True),
            mock.patch.object(_gw, "api_client_slot", return_value=slot) as acquire,
        ):
            ib = asyncio.run(_gw.connect(role="stock_status", ib_factory=FakeIB))

        acquire.assert_called_once_with(92, wait_seconds=0.0)
        self.assertEqual(("127.0.0.1", 4001, 92, True), ib.connection)
        self.assertEqual((1, 0), (slot.entered, slot.exited))
        ib.disconnect()
        self.assertEqual((1, 1), (slot.entered, slot.exited))
        ib.disconnect()
        self.assertEqual(1, slot.exited)

    def test_failed_connection_releases_slot(self):
        slot = FakeSlot()
        failing = type("FailingIB", (FakeIB,), {"should_fail": True})
        with (
            mock.patch.object(_gw, "_check_alive", return_value=True),
            mock.patch.object(_gw, "api_client_slot", return_value=slot),
        ):
            with self.assertRaisesRegex(RuntimeError, "connect failed"):
                asyncio.run(_gw.connect(role="live_fills", ib_factory=failing))
        self.assertEqual((1, 1), (slot.entered, slot.exited))


    def test_unsynced_gateway_raises_and_releases_slot(self):
        slot = FakeSlot()

        async def never(self):
            await asyncio.sleep(3600)

        wedged = type("WedgedIB", (FakeIB,), {"reqPositionsAsync": never})
        with (
            mock.patch.object(_gw, "_check_alive", return_value=True),
            mock.patch.object(_gw, "api_client_slot", return_value=slot),
            mock.patch.object(_gw, "_SYNC_PROBE_TIMEOUT", 0.01),
        ):
            with self.assertRaisesRegex(ConnectionError, "not synced"):
                asyncio.run(_gw.connect(role="option_risk", ib_factory=wedged))
        self.assertEqual((1, 1), (slot.entered, slot.exited))

if __name__ == "__main__":
    unittest.main()
