#!/usr/bin/env python3
"""Reversible Python client compatibility layer for the IBKR scripts.

Read-only/monitor scripts import their IB client surface from this module.
Backend selection:

1. ``IBKR_PYTHON_BACKEND=ib_async|ib_insync`` wins when set (explicit override
   and rollback lever).
2. Otherwise prefer the maintained fork ``ib_async`` when the interpreter has
   it installed (the pinned 3.12 venv ``.venv-ibkr``), falling back to
   legacy ``ib_insync`` (e.g. macOS system Python 3.9 without ib_async).

Order-execution scripts (``stock_order.py`` / ``option_order.py`` and their
shared ``_stock.py`` / ``stock_status.py``) also route through this module
since 2026-08-07, after the Paper execution matrix passed; see
``references/ib-async-migration.md``. Run live order entry with the venv
interpreter so ib_async is actually selected.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import os
from typing import Any, Iterable, List, Optional


BACKEND_ENV = "IBKR_PYTHON_BACKEND"
SUPPORTED_BACKENDS = ("ib_insync", "ib_async")


def _select_backend() -> str:
    name = os.getenv(BACKEND_ENV, "").strip()
    if name:
        if name not in SUPPORTED_BACKENDS:
            choices = ", ".join(SUPPORTED_BACKENDS)
            raise RuntimeError(
                f"Unsupported {BACKEND_ENV}={name!r}; expected one of: {choices}"
            )
        return name
    # No explicit choice: prefer the maintained fork when this interpreter can
    # import it, else fall back to legacy ib_insync.  BACKEND_NAME stays
    # observable in script output so the effective backend is never silent.
    for candidate in SUPPORTED_BACKENDS[::-1]:
        if importlib.util.find_spec(candidate) is not None:
            return candidate
    return "ib_async"


BACKEND_NAME = _select_backend()
try:
    BACKEND_MODULE = importlib.import_module(BACKEND_NAME)
except ModuleNotFoundError as exc:
    if exc.name != BACKEND_NAME:
        raise
    if BACKEND_NAME == "ib_async":
        install_hint = (
            "Use Python 3.12 and install "
            "skills/ibkr-trading/requirements.txt."
        )
    else:
        install_hint = "Install ib_insync==0.9.86 or select ib_async explicitly."
    raise ModuleNotFoundError(
        f"Selected IBKR Python backend {BACKEND_NAME!r} is not installed. "
        f"{install_hint}"
    ) from exc

BACKEND_VERSION = getattr(BACKEND_MODULE, "__version__", "unknown")


def _legacy_qualification_result(values: Iterable[Any]) -> List[Any]:
    """Normalize ib_async 2.x qualification results to ib_insync semantics.

    ``ib_insync`` omits unknown or ambiguous contracts.  ``ib_async`` 2.x
    preserves request positions and inserts ``None`` instead.  Existing code
    often checks ``if not qualified`` and then indexes element zero, so
    returning ``[None]`` would turn a safe failure into an AttributeError.

    Nested lists are possible only with ib_async's opt-in ``returnAll`` mode;
    the legacy API has no equivalent, so they are treated as unresolved here.
    """

    return [value for value in values if value is not None and not isinstance(value, list)]


class IB(BACKEND_MODULE.IB):
    """Selected backend with legacy-safe contract qualification behaviour."""

    async def qualifyContractsAsync(self, *contracts: Any) -> List[Any]:
        values = await super().qualifyContractsAsync(*contracts)
        return _legacy_qualification_result(values)


# Re-export the currently used public surface.  Keeping this list explicit
# prevents a backend upgrade from silently changing what the scripts import.
Contract = BACKEND_MODULE.Contract
Stock = BACKEND_MODULE.Stock
Option = BACKEND_MODULE.Option
ComboLeg = BACKEND_MODULE.ComboLeg
LimitOrder = BACKEND_MODULE.LimitOrder
MarketOrder = BACKEND_MODULE.MarketOrder
StopOrder = BACKEND_MODULE.StopOrder
StopLimitOrder = BACKEND_MODULE.StopLimitOrder
ExecutionFilter = BACKEND_MODULE.ExecutionFilter
util = BACKEND_MODULE.util


class ContractQualificationError(RuntimeError):
    """Raised when strict qualification cannot resolve every input contract."""


async def qualify_contracts_strict(
    ib: Any,
    *contracts: Any,
    timeout: Optional[float] = None,
) -> List[Any]:
    """Qualify every contract or fail closed.

    This helper is intended for order-entry and route-selection code.  It
    accepts either the adapter ``IB`` above or a raw backend instance, and
    catches both the legacy "short result" and ib_async's position-aligned
    ``None`` result.
    """

    pending = ib.qualifyContractsAsync(*contracts)
    if timeout is None:
        values = await pending
    else:
        values = await asyncio.wait_for(pending, timeout=timeout)

    resolved = _legacy_qualification_result(values)
    if len(resolved) != len(contracts):
        raise ContractQualificationError(
            f"IBKR resolved {len(resolved)} of {len(contracts)} contract(s); "
            "refusing to continue with a partial or ambiguous qualification"
        )
    return resolved


__all__ = [
    "BACKEND_ENV",
    "BACKEND_MODULE",
    "BACKEND_NAME",
    "BACKEND_VERSION",
    "ComboLeg",
    "Contract",
    "ContractQualificationError",
    "ExecutionFilter",
    "IB",
    "LimitOrder",
    "MarketOrder",
    "Option",
    "Stock",
    "StopLimitOrder",
    "StopOrder",
    "SUPPORTED_BACKENDS",
    "qualify_contracts_strict",
    "util",
]
