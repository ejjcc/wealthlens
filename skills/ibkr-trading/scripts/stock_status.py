#!/usr/bin/env python3
"""Read-only, session-aware stock preflight: quote + position + orders + fills."""

import argparse
import asyncio
import json
import os
import sys

from _ib import ExecutionFilter, IB

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _client_ids import client_id_for  # noqa: E402
import trade_quota  # noqa: E402
from _stock import (  # noqa: E402
    active_stock_trades,
    classify_us_stock_session,
    execution_to_dict,
    fetch_stock_quote,
    is_marketable,
    order_to_dict,
    qualify_stock,
    route_is_live,
    snapshot_position,
)


async def main():
    ap = argparse.ArgumentParser(
        description="IBKR 正股会话感知状态：实时盘口、持仓、活动订单、当日成交"
    )
    ap.add_argument("symbol")
    ap.add_argument("--route", type=str.upper,
                    choices=["AUTO", "SMART", "OVERNIGHT"], default="AUTO")
    ap.add_argument("--currency", default="USD")
    ap.add_argument("--action", type=str.upper, choices=["BUY", "SELL"])
    ap.add_argument("--limit", type=float, help="可选：判断该限价是否可成交")
    ap.add_argument("--client-id", type=int, default=client_id_for("stock_status"))
    ap.add_argument("--quote-timeout", type=float, default=4.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if (args.action is None) != (args.limit is None):
        ap.error("--action 与 --limit 必须一起提供")

    symbol = args.symbol.upper()
    session = classify_us_stock_session()
    route = session.route if args.route == "AUTO" else args.route
    route = route or "SMART"  # closed/gap: show SMART diagnostics, never imply live

    ib = IB()
    try:
        await ib.connectAsync("127.0.0.1", 4001, clientId=args.client_id,
                              readonly=True, timeout=10)
    except Exception as exc:
        print("ERROR: cannot connect IB Gateway ({})".format(exc), file=sys.stderr)
        return 1

    try:
        contract = await qualify_stock(ib, symbol, route, args.currency)
        await ib.reqAllOpenOrdersAsync()
        fills = await ib.reqExecutionsAsync(ExecutionFilter(symbol=symbol))
        quote = await fetch_stock_quote(ib, contract, args.quote_timeout)
        orders = [order_to_dict(t) for t in active_stock_trades(ib, symbol)]
        fill_rows = [execution_to_dict(fill) for fill in fills]
        ledger_updates = trade_quota.reconcile_existing(orders, fill_rows)
        position = snapshot_position(ib, symbol)
        result = {
            "symbol": symbol,
            "contract": {
                "conId": contract.conId,
                "route": contract.exchange,
                "primaryExchange": contract.primaryExchange,
                "currency": contract.currency,
            },
            "session": session.to_dict(),
            "selectedRoute": route,
            "routeLiveNow": route_is_live(route, session),
            "quote": quote,
            "position": position,
            "openOrders": orders,
            "todayFills": fill_rows,
            "ledgerUpdates": ledger_updates,
        }
        if args.limit is not None:
            result["limitCheck"] = {
                "action": args.action,
                "limit": args.limit,
                "marketable": is_marketable(
                    args.action, args.limit, quote.get("bid"), quote.get("ask")
                ),
            }

        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        print("{} · ET session={} · route={} ({})".format(
            symbol, session.name, route,
            "LIVE" if result["routeLiveNow"] else "NOT LIVE",
        ))
        q = quote
        print("Quote [{}] bid={} x {} / ask={} x {} / last={} · {} · age={}s".format(
            q["source"], q["bid"], q["bidSize"], q["ask"], q["askSize"],
            q["last"], q["time"], q["ageSeconds"],
        ))
        print("Position qty={} avgCost={}".format(
            position["quantity"], position["avgCost"]
        ))
        print("Open orders: {}".format(len(orders)))
        for row in orders:
            print("  #{orderId} permId={permId} {action} {remaining}/{quantity} "
                  "@ {limitPrice} {route} {status}".format(**row))
        print("Today fills: {}".format(len(result["todayFills"])))
        for row in result["todayFills"]:
            print("  {time} {side} {shares} @ {price} permId={permId}".format(**row))
        if ledger_updates:
            print("Ledger rows updated: {}".format(ledger_updates))
        if "limitCheck" in result:
            row = result["limitCheck"]
            print("Limit check: {action} LMT {limit} marketable={marketable}".format(**row))
        return 0
    finally:
        ib.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
