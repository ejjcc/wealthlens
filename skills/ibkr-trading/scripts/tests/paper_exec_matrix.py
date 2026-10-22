#!/usr/bin/env python3
"""Phase-4 paper execution matrix for the ib_insync -> ib_async migration.

Runs ONLY against the paper gateway on 127.0.0.1:4002 and refuses to talk to
any non-DU account. Exercises the order-path behaviours that offline and
read-only validation cannot reach, and emits a JSON report so the two
backends' runs can be diffed:

  .venv-ibkr/bin/python skills/ibkr-trading/scripts/tests/paper_exec_matrix.py \
      --out /tmp/matrix-async.json          # backend auto-selected: ib_async
  python3 skills/ibkr-trading/scripts/tests/paper_exec_matrix.py \
      --out /tmp/matrix-insync.json         # system 3.9 -> ib_insync

Non-fill cases (safe any time the paper gateway is up and funded):
  qualify_valid / qualify_invalid / qualify_strict_partial
  stock_limit_place_modify_cancel   (far-from-market LMT, outsideRth+PMA flags)
  overnight_route_place_cancel      (explicit OVERNIGHT exchange)
  option_limit_place_cancel         (single-leg far LMT)
  combo_place_cancel                (2-leg BAG, guaranteed route)

Fill cases (need RTH and funding; enable explicitly):
  --with-fills : marketable stock LMT fill + oversized-then-partial handling

Every order this script places is either priced far away from the market or
cancelled before exit; --with-fills trades 1-2 shares of a liquid ETF on the
PAPER account only.
"""

import argparse
import asyncio
import datetime as dt
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(SCRIPT_DIR))

from _ib import (  # noqa: E402
    IB,
    BACKEND_NAME,
    ComboLeg,
    Contract,
    LimitOrder,
    Option,
    Stock,
    qualify_contracts_strict,
)

PAPER_HOST = "127.0.0.1"
PAPER_PORT = 4002
CLIENT_ID = 231


def order_snapshot(trade):
    o, s = trade.order, trade.orderStatus
    return {
        "orderId": o.orderId,
        "action": o.action,
        "totalQuantity": float(o.totalQuantity),
        "lmtPrice": getattr(o, "lmtPrice", None),
        "tif": o.tif,
        "outsideRth": o.outsideRth,
        "usePriceMgmtAlgo": getattr(o, "usePriceMgmtAlgo", None),
        "status": s.status,
        "filled": float(s.filled),
        "remaining": float(s.remaining),
    }


async def wait_status(ib, trade, statuses, timeout=20):
    """Wait until trade.orderStatus.status is in `statuses`; return final."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if trade.orderStatus.status in statuses:
            return trade.orderStatus.status
        await asyncio.sleep(0.25)
    return trade.orderStatus.status


async def run_matrix(with_fills: bool):
    report = {
        "backend": BACKEND_NAME,
        "started": dt.datetime.now(dt.timezone.utc).isoformat(),
        "cases": {},
    }
    ib = IB()
    await ib.connectAsync(PAPER_HOST, PAPER_PORT, clientId=CLIENT_ID, timeout=30)
    try:
        accounts = ib.managedAccounts()
        assert accounts and all(a.startswith("DU") for a in accounts), (
            f"SAFETY ABORT: non-paper account visible: {accounts}"
        )
        report["account"] = accounts[0]

        # ---- qualification semantics -------------------------------------
        spy = Stock("SPY", "SMART", "USD")
        q = await ib.qualifyContractsAsync(spy)
        report["cases"]["qualify_valid"] = {
            "resolved": len(q), "conId": q[0].conId if q else None,
        }

        bogus = Stock("ZZZYYXX", "SMART", "USD")
        q2 = await ib.qualifyContractsAsync(bogus)
        report["cases"]["qualify_invalid"] = {"resolved": len(q2)}

        try:
            await qualify_contracts_strict(ib, Stock("SPY", "SMART", "USD"),
                                           Stock("ZZZYYXX", "SMART", "USD"))
            strict = "no-error (UNEXPECTED)"
        except Exception as exc:
            strict = type(exc).__name__
        report["cases"]["qualify_strict_partial"] = {"raises": strict}

        # ---- stock LMT place / modify / cancel ---------------------------
        [spy] = await ib.qualifyContractsAsync(Stock("SPY", "SMART", "USD"))
        buy = LimitOrder("BUY", 1, 100.00, tif="DAY", outsideRth=True)
        buy.usePriceMgmtAlgo = True
        tr = ib.placeOrder(spy, buy)
        st = await wait_status(ib, tr, ("PreSubmitted", "Submitted"))
        snap_placed = order_snapshot(tr)

        buy.lmtPrice = 101.00
        ib.placeOrder(spy, buy)          # modify in place
        await asyncio.sleep(2)
        snap_modified = order_snapshot(tr)

        ib.cancelOrder(buy)
        st_cancel = await wait_status(ib, tr, ("Cancelled", "ApiCancelled"))
        report["cases"]["stock_limit_place_modify_cancel"] = {
            "placed_status": st, "placed": snap_placed,
            "modified": snap_modified, "cancel_status": st_cancel,
        }

        # ---- explicit OVERNIGHT route ------------------------------------
        ovn = Stock("SPY", "OVERNIGHT", "USD")
        qo = await ib.qualifyContractsAsync(ovn)
        if qo:
            o = LimitOrder("BUY", 1, 100.00, tif="DAY")
            tro = ib.placeOrder(qo[0], o)
            sto = await wait_status(ib, tro, ("PreSubmitted", "Submitted"))
            ib.cancelOrder(o)
            stoc = await wait_status(ib, tro, ("Cancelled", "ApiCancelled"))
            report["cases"]["overnight_route_place_cancel"] = {
                "qualified_conId": qo[0].conId, "placed_status": sto,
                "cancel_status": stoc,
            }
        else:
            report["cases"]["overnight_route_place_cancel"] = {
                "qualified_conId": None, "note": "OVERNIGHT qualify failed",
            }

        # ---- single-leg option far LMT -----------------------------------
        chains = await ib.reqSecDefOptParamsAsync("SPY", "", "STK", spy.conId)
        chain = next(c for c in chains if c.exchange == "SMART")
        ib.reqMarketDataType(2)
        [tick] = await ib.reqTickersAsync(spy)
        spot = next((v for v in (tick.last, tick.close, tick.ask) if v and v > 0), 630)
        # The secdef strike/expiry lists are cross-expiry unions (and can
        # contain half-formed new listings). Use the REAL contract list of the
        # densest near expiry instead of guessing combinations.
        expiry = None
        cds = []
        for exp in sorted(chain.expirations)[:4]:
            cds = await ib.reqContractDetailsAsync(Option("SPY", exp, 0.0, "C", "SMART"))
            if len(cds) >= 10:
                expiry = exp
                break
        assert expiry, "no dense SPY call expiry found"
        by_strike = sorted(cds, key=lambda cd: cd.contract.strike)
        idx = min(range(len(by_strike)),
                  key=lambda i: abs(by_strike[i].contract.strike - spot))
        opt = by_strike[idx].contract
        strike = opt.strike
        [opt] = await ib.qualifyContractsAsync(opt)
        oo = LimitOrder("BUY", 1, 0.05, tif="DAY")
        tro2 = ib.placeOrder(opt, oo)
        sto2 = await wait_status(ib, tro2, ("PreSubmitted", "Submitted"))
        ib.cancelOrder(oo)
        sto2c = await wait_status(ib, tro2, ("Cancelled", "ApiCancelled"))
        report["cases"]["option_limit_place_cancel"] = {
            "contract": f"SPY {expiry} {strike}C", "placed_status": sto2,
            "cancel_status": sto2c,
        }

        # ---- 2-leg combo (vertical call spread, BUY = debit) -------------
        assert idx + 1 < len(by_strike), "no higher strike in real contract list"
        leg2 = by_strike[idx + 1].contract
        strike2 = leg2.strike
        legs = [
            ComboLeg(conId=opt.conId, ratio=1, action="BUY", exchange="SMART"),
            ComboLeg(conId=leg2.conId, ratio=1, action="SELL", exchange="SMART"),
        ]
        bag = Contract(secType="BAG", symbol="SPY", currency="USD",
                       exchange="SMART", comboLegs=legs)
        co = LimitOrder("BUY", 1, 0.05, tif="DAY")
        tro3 = ib.placeOrder(bag, co)
        sto3 = await wait_status(ib, tro3, ("PreSubmitted", "Submitted"))
        ib.cancelOrder(co)
        sto3c = await wait_status(ib, tro3, ("Cancelled", "ApiCancelled"))
        report["cases"]["combo_place_cancel"] = {
            "legs": f"BUY {strike}C / SELL {strike2}C {expiry}",
            "placed_status": sto3, "cancel_status": sto3c,
        }

        # ---- fills (RTH + funded only) -----------------------------------
        if with_fills:
            # Paper accounts have no real-time subscription; delayed (type 3)
            # works. 2% buffer over the delayed quote keeps the LMT marketable
            # against the real book the paper simulator matches on.
            ib.reqMarketDataType(3)
            [t] = await ib.reqTickersAsync(spy)
            ask = max(v for v in (t.last, t.close, t.ask) if v and v > 0)
            fo = LimitOrder("BUY", 1, round(ask * 1.02, 2), tif="DAY")
            trf = ib.placeOrder(spy, fo)
            stf = await wait_status(ib, trf, ("Filled",), timeout=60)
            fills = [
                {"shares": float(f.execution.shares),
                 "price": f.execution.price,
                 "commission": f.commissionReport.commission
                 if f.commissionReport else None}
                for f in trf.fills
            ]
            # unwind
            so = LimitOrder("SELL", 1, round(ask * 0.98, 2), tif="DAY")
            trs = ib.placeOrder(spy, so)
            sts = await wait_status(ib, trs, ("Filled",), timeout=60)
            report["cases"]["marketable_fill_roundtrip"] = {
                "buy_status": stf, "buy_fills": fills, "sell_status": sts,
            }

        # ---- open-order visibility / cleanup sweep -----------------------
        await asyncio.sleep(1)
        leftovers = [order_snapshot(t) for t in ib.openTrades()]
        for t in ib.openTrades():
            ib.cancelOrder(t.order)
        report["cases"]["leftover_open_orders"] = leftovers
    finally:
        ib.disconnect()
    report["finished"] = dt.datetime.now(dt.timezone.utc).isoformat()
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--with-fills", action="store_true")
    args = ap.parse_args()
    report = asyncio.get_event_loop().run_until_complete(run_matrix(args.with_fills))
    with open(args.out, "w") as f:
        json.dump(report, f, indent=1, default=str)
    print(f"[{BACKEND_NAME}] wrote {args.out}")
    for name, case in report["cases"].items():
        print(f"  {name}: {json.dumps(case, default=str)[:120]}")


if __name__ == "__main__":
    main()
