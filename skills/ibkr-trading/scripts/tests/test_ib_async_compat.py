#!/usr/bin/env python3
"""Offline tests for the reversible ib_insync -> ib_async adapter."""

import asyncio
import os
import pathlib
import sys
import unittest
from unittest import mock


SCRIPT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import _ib  # noqa: E402


class ExportSurfaceTests(unittest.TestCase):
    def test_selected_backend_follows_policy_and_is_supported(self):
        import importlib.util

        explicit = os.getenv(_ib.BACKEND_ENV, "").strip()
        if explicit:
            expected = explicit
        else:
            # Default policy: prefer ib_async when importable, else ib_insync.
            expected = next(
                (
                    name
                    for name in ("ib_async", "ib_insync")
                    if importlib.util.find_spec(name) is not None
                ),
                "ib_async",
            )
        self.assertEqual(expected, _ib.BACKEND_NAME)
        self.assertIn(_ib.BACKEND_NAME, _ib.SUPPORTED_BACKENDS)

    def test_current_script_import_surface_is_available(self):
        for name in (
            "IB",
            "Contract",
            "Stock",
            "Option",
            "ComboLeg",
            "LimitOrder",
            "MarketOrder",
            "ExecutionFilter",
        ):
            self.assertTrue(hasattr(_ib, name), name)

    def test_contract_and_order_construction_is_compatible(self):
        stock = _ib.Stock("AAPL", "SMART", "USD")
        option = _ib.Option("AAPL", "20261218", 200, "C", "SMART")
        order = _ib.LimitOrder("BUY", 1, 100.0)
        order.outsideRth = True
        order.usePriceMgmtAlgo = True

        self.assertEqual("AAPL", stock.symbol)
        self.assertEqual("OPT", option.secType)
        self.assertEqual("LMT", order.orderType)
        self.assertTrue(order.outsideRth)
        self.assertTrue(order.usePriceMgmtAlgo)


class QualificationCompatibilityTests(unittest.TestCase):
    def test_adapter_filters_ib_async_none_like_legacy_ib_insync(self):
        first = _ib.Stock("AAPL", "SMART", "USD")
        second = _ib.Stock("NO_SUCH_CONTRACT", "SMART", "USD")

        async def fake_qualify(_instance, *_contracts):
            return [first, None]

        with mock.patch.object(
            _ib.BACKEND_MODULE.IB,
            "qualifyContractsAsync",
            new=fake_qualify,
        ):
            result = asyncio.run(_ib.IB().qualifyContractsAsync(first, second))

        self.assertEqual([first], result)

    def test_strict_helper_rejects_partial_result(self):
        first = _ib.Stock("AAPL", "SMART", "USD")
        second = _ib.Stock("NO_SUCH_CONTRACT", "SMART", "USD")

        class FakeIB:
            async def qualifyContractsAsync(self, *_contracts):
                return [first, None]

        with self.assertRaises(_ib.ContractQualificationError):
            asyncio.run(
                _ib.qualify_contracts_strict(FakeIB(), first, second)
            )

    def test_strict_helper_preserves_all_resolved_contracts(self):
        first = _ib.Stock("AAPL", "SMART", "USD")
        second = _ib.Stock("MSFT", "SMART", "USD")

        class FakeIB:
            async def qualifyContractsAsync(self, *_contracts):
                return [first, second]

        result = asyncio.run(
            _ib.qualify_contracts_strict(FakeIB(), first, second)
        )
        self.assertEqual([first, second], result)


if __name__ == "__main__":
    unittest.main()
