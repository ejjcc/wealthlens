#!/usr/bin/env python3
"""Session-aware IBKR stock order entry. Dry-run unless --execute is present."""

import argparse
import asyncio
import math
import os
import sys

from _ib import ExecutionFilter, IB, LimitOrder, MarketOrder, StopLimitOrder, StopOrder

from _client_ids import client_id_for

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trade_quota  # noqa: E402
from _stock import (  # noqa: E402
    DONE_ORDER_STATUSES,
    FAILED_ORDER_STATUSES,
    active_stock_trades,
    classify_us_stock_session,
    exclusive_order_lock,
    fetch_stock_quote,
    is_fresh_live_quote,
    is_marketable,
    order_to_dict,
    qualify_stock,
    quote_age_seconds,
    redirect_bypass_applied,
    route_is_live,
    sell_stop_is_safe,
    snapshot_position,
)


class OrderAbort(RuntimeError):
    def __init__(self, message, code=2):
        super().__init__(message)
        self.code = code


class ErrorCollector:
    def __init__(self):
        self.rows = []

    def __call__(self, req_id, code, message, contract=None, *unused):
        # Connectivity/farm status messages are not order diagnostics.
        if code not in {2104, 2106, 2107, 2108, 2158}:
            self.rows.append({"reqId": req_id, "code": code, "message": message})


def parse_args():
    ap = argparse.ArgumentParser(
        description="IBKR 正股下单；实盘必须显式指定 SMART/OVERNIGHT 路由"
    )
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--action", choices=["BUY", "SELL"], required=True)
    size = ap.add_mutually_exclusive_group(required=True)
    size.add_argument("--qty", type=int, help="本次订单股数")
    size.add_argument("--target-position", type=float,
                      help="目标最终持仓；安全改单必须使用此项")
    price = ap.add_mutually_exclusive_group(required=True)
    price.add_argument("--limit", type=float, help="限价")
    price.add_argument("--market", action="store_true", help="市价单（仅常规时段）")
    ap.add_argument("--stop", type=float, metavar="TRIGGER",
                    help="止损触发价；配 --limit 为 STP LMT（保护带），配 --market 为 STP。"
                         "仅支持 SELL；默认仅 RTH 触发（可显式 --outside-rth 覆盖）")
    ap.add_argument("--route", type=str.upper, choices=["SMART", "OVERNIGHT"],
                    help="实盘必填；必须与确认卡一致")
    ap.add_argument("--exchange", type=str.upper, choices=["SMART", "OVERNIGHT"],
                    help=argparse.SUPPRESS)  # backward-compatible alias
    ap.add_argument("--tif", default="DAY")
    ap.set_defaults(outside_rth=None, price_mgmt=True)
    ap.add_argument("--outside-rth", dest="outside_rth", action="store_true")
    ap.add_argument("--no-outside-rth", dest="outside_rth", action="store_false")
    ap.add_argument("--price-mgmt", dest="price_mgmt", action="store_true")
    ap.add_argument("--no-price-mgmt", dest="price_mgmt", action="store_false")
    ap.add_argument("--currency", default="USD")
    ap.add_argument("--client-id", type=int, default=client_id_for("stock_order"))
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--allow-queued", action="store_true",
                    help="显式允许路由当前不活跃的排队单")
    ap.add_argument("--require-marketable", action="store_true",
                    help="要求限价穿过当前买一/卖一；紧急防御/平仓使用")
    ap.add_argument("--max-quote-age", type=float, default=10.0)
    ap.add_argument("--replace", type=int, metavar="ORDER_OR_PERM_ID",
                    help="安全撤换已有订单；必须同时使用 --target-position")
    ap.add_argument("--wait-seconds", type=float, default=5.0)
    ap.add_argument("--quota-override", metavar="REASON",
                    help="窗外 BUY 的显式人工例外，需提供具体理由；默认仍阻止；SELL 不受买入周窗限制")
    args = ap.parse_args()

    if args.route and args.exchange and args.route != args.exchange:
        ap.error("--route 与旧 --exchange 参数冲突")
    if args.replace is not None and args.target_position is None:
        ap.error("--replace 必须配合 --target-position，防止改单期间成交后重复下单")
    if args.qty is not None and args.qty <= 0:
        ap.error("--qty 必须大于 0")
    if args.limit is not None and args.limit <= 0:
        ap.error("--limit 必须大于 0")
    if args.stop is not None:
        if args.stop <= 0:
            ap.error("--stop 必须大于 0")
        if args.action != "SELL":
            ap.error("--stop 目前仅支持 SELL 止损单")
        if args.require_marketable:
            ap.error("--require-marketable 不适用于止损挂单")
        if args.limit is not None and args.limit > args.stop:
            ap.error("SELL STP LMT 的 --limit 不应高于 --stop 触发价")
    # Stop orders default to RTH-only triggering; plain orders keep outsideRth on.
    if args.outside_rth is None:
        args.outside_rth = args.stop is None
    return args


def quantity_for_target(action, current, target):
    delta = target - current if action == "BUY" else current - target
    if delta <= 1e-9:
        return 0
    rounded = int(round(delta))
    if rounded <= 0 or not math.isclose(delta, rounded, abs_tol=1e-6):
        raise OrderAbort(
            "target-position requires a whole-share delta; current={} target={} delta={}".format(
                current, target, delta
            )
        )
    return rounded


def quota_preflight(action, override_reason=None):
    """Apply the weekly discipline to BUY only and return display metadata."""
    override_applied = action == "BUY" and bool(override_reason)
    ok, message = trade_quota.quota_available(action)
    if not ok and not override_applied:
        raise OrderAbort("QUOTA BLOCKED: {}".format(message))
    return message, override_applied


async def wait_for_cancel(ib, trade, timeout=12.0):
    ib.cancelOrder(trade.order)
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if trade.orderStatus.status in DONE_ORDER_STATUSES:
            return trade.orderStatus.status
        await asyncio.sleep(0.2)
    raise OrderAbort(
        "cancel timeout for orderId={} status={}".format(
            trade.order.orderId, trade.orderStatus.status
        ), code=3,
    )


async def run(args):
    symbol = args.symbol.upper()
    quota_override_applied = False
    session = classify_us_stock_session()
    explicit_route = args.route or args.exchange
    if args.execute and not explicit_route:
        raise OrderAbort("--execute requires explicit --route SMART|OVERNIGHT")
    route = explicit_route or session.route or "SMART"

    live_now = route_is_live(route, session)
    if args.execute and not live_now and not args.allow_queued:
        raise OrderAbort(
            "route {} is not live in ET session {}. Refresh stock_status.py; "
            "use --allow-queued only when intentional.".format(route, session.name)
        )
    if route == "OVERNIGHT" and args.market:
        raise OrderAbort("OVERNIGHT only accepts protected limit orders")
    if route == "OVERNIGHT" and args.stop is not None:
        raise OrderAbort("OVERNIGHT does not accept stop orders")
    if args.market and session.name != "regular":
        raise OrderAbort("market orders are restricted to the regular session")
    if args.execute and route == "OVERNIGHT":
        ok, detail = redirect_bypass_applied()
        if not ok:
            raise OrderAbort("OVERNIGHT redirect preflight failed: {}".format(detail))
        print("  OVERNIGHT precaution: {}".format(detail))

    if args.execute:
        message, quota_override_applied = quota_preflight(
            args.action, args.quota_override
        )
        print("  额度: {}{}".format(
            message,
            " [override: {}]".format(args.quota_override)
            if quota_override_applied else "",
        ))

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    ib = IB()
    errors = ErrorCollector()
    ib.errorEvent += errors
    try:
        try:
            await ib.connectAsync("127.0.0.1", 4001, clientId=args.client_id,
                                  readonly=not args.execute, timeout=25)
        except Exception as exc:
            raise OrderAbort(
                "cannot connect IB Gateway ({}). Run: launchctl kickstart -k "
                "gui/$(id -u)/com.user.ibc-gateway".format(exc), code=1,
            )

        contract = await qualify_stock(ib, symbol, route, args.currency)
        if contract.symbol != symbol or contract.exchange != route:
            raise OrderAbort(
                "CONTRACT MISMATCH requested={}/{} got={}/{}".format(
                    symbol, route, contract.symbol, contract.exchange
                )
            )
        print("  contract: {} conId={} route={} primary={}/{}".format(
            contract.symbol, contract.conId, contract.exchange,
            contract.primaryExchange, contract.currency,
        ))
        print("  ET session: {} routeLiveNow={} ({})".format(
            session.name, live_now, session.et_time
        ))

        await ib.reqAllOpenOrdersAsync()
        active = active_stock_trades(ib, symbol)
        replaced_perm_id = None
        if active:
            if args.replace is None:
                details = "; ".join(
                    "#{} permId={} {} {} remaining={} @ {} {}".format(
                        t.order.orderId, t.order.permId, t.order.action,
                        t.order.totalQuantity, t.orderStatus.remaining,
                        t.order.lmtPrice, t.orderStatus.status,
                    ) for t in active
                )
                if args.execute:
                    raise OrderAbort(
                        "active {} order(s) already exist: {}. Use --replace with "
                        "--target-position after confirmation.".format(symbol, details), code=3,
                    )
                print("  WARNING active order(s): {}".format(details))
            else:
                matches = [t for t in active if args.replace in {
                    int(t.order.orderId or 0), int(t.order.permId or 0)
                }]
                if len(matches) != 1:
                    raise OrderAbort(
                        "--replace {} matched {} active orders".format(args.replace, len(matches)),
                        code=3,
                    )
                old = matches[0]
                if old.order.action != args.action:
                    raise OrderAbort("replacement action differs from existing order", code=3)
                replaced_perm_id = old.order.permId or None
                if args.execute:
                    status = await wait_for_cancel(ib, old)
                    print("  cancelled old order #{} permId={} -> {} filled={}".format(
                        old.order.orderId, old.order.permId, status,
                        old.orderStatus.filled,
                    ))
                    trade_quota.upsert_order(
                        symbol, old.order.action, old.order.permId,
                        order_id=old.order.orderId, status=status,
                        qty=old.order.totalQuantity, filled_qty=old.orderStatus.filled,
                        limit_price=old.order.lmtPrice,
                        avg_fill_price=old.orderStatus.avgFillPrice,
                        route=old.contract.exchange,
                        note="cancelled during safe replacement",
                        override=quota_override_applied,
                    )
                    await ib.reqAllOpenOrdersAsync()
                    leftovers = active_stock_trades(ib, symbol)
                    if leftovers:
                        raise OrderAbort("another active {} order remains after cancel".format(symbol),
                                         code=3)
                else:
                    print("  DRY-RUN would cancel #{} permId={}".format(
                        old.order.orderId, old.order.permId
                    ))

        # Re-read position after cancellation: the old order may have filled in
        # the race window. Target-position converts that race into fewer/no new shares.
        position = snapshot_position(ib, symbol)
        qty = args.qty if args.qty is not None else quantity_for_target(
            args.action, position["quantity"], args.target_position
        )
        if qty == 0:
            print("  NO ORDER: target position already reached; current={}".format(
                position["quantity"]
            ))
            return 0

        quote = await fetch_stock_quote(ib, contract)
        print("  quote [{}] {} bid={} ask={} last={} age={}s".format(
            quote["source"], quote["time"], quote["bid"], quote["ask"],
            quote["last"], quote["ageSeconds"],
        ))
        fresh_live_quote = is_fresh_live_quote(quote, args.max_quote_age)
        if args.execute and live_now and not fresh_live_quote:
            raise OrderAbort("fresh route-specific bid/ask unavailable; refusing live order")

        if args.stop is not None:
            # A stop rests server-side; refuse triggers at/above the live bid,
            # which would fire immediately as a disguised market/limit sell.
            safe = sell_stop_is_safe(args.stop, quote.get("bid"))
            if args.execute and safe is None:
                raise OrderAbort("cannot verify stop trigger against a live bid; refusing")
            if safe is False:
                raise OrderAbort(
                    "stop trigger {} >= live bid {}; would fire immediately. "
                    "Use a plain limit/market order to sell now.".format(
                        args.stop, quote.get("bid")
                    )
                )
            marketable = None
            if args.limit is not None:
                order = StopLimitOrder(args.action, qty, lmtPrice=args.limit,
                                       stopPrice=args.stop, tif=args.tif)
                price_description = "STP {} LMT {}".format(args.stop, args.limit)
            else:
                order = StopOrder(args.action, qty, stopPrice=args.stop, tif=args.tif)
                price_description = "STP {} MKT".format(args.stop)
        elif args.limit is not None:
            marketable = is_marketable(
                args.action, args.limit, quote.get("bid"), quote.get("ask")
            )
            if args.require_marketable and marketable is not True:
                raise OrderAbort(
                    "limit is not marketable on fresh {} quote: {} LMT {} bid={} ask={}".format(
                        route, args.action, args.limit, quote.get("bid"), quote.get("ask")
                    )
                )
            order = LimitOrder(args.action, qty, args.limit, tif=args.tif)
            price_description = "LMT {}".format(args.limit)
        else:
            marketable = True
            order = MarketOrder(args.action, qty)
            price_description = "MKT"
        order.tif = args.tif
        order.outsideRth = args.outside_rth if route == "SMART" else False
        # IB's price management algo applies to plain limit orders only.
        order.usePriceMgmtAlgo = args.price_mgmt if args.stop is None else False

        print("[{}] {} {} {} @ {} {} route={} outsideRth={} priceMgmt={} "
              "marketable={} currentPosition={} targetPosition={}".format(
                  mode, args.action, qty, symbol, price_description, args.tif,
                  route, order.outsideRth, order.usePriceMgmtAlgo, marketable,
                  position["quantity"], args.target_position,
              ))
        if not args.execute:
            print("  (dry-run; no order transmitted)")
            return 0

        before_errors = len(errors.rows)
        trade = ib.placeOrder(contract, order)
        deadline = asyncio.get_event_loop().time() + max(args.wait_seconds, 1.0)
        while asyncio.get_event_loop().time() < deadline:
            if trade.orderStatus.status in FAILED_ORDER_STATUSES or trade.orderStatus.status == "Filled":
                break
            await asyncio.sleep(0.2)
        order_errors = errors.rows[before_errors:]
        for row in order_errors:
            print("  IB {} reqId={}: {}".format(
                row["code"], row["reqId"], row["message"]
            ))

        status = trade.orderStatus.status
        perm_id = int(trade.order.permId or 0)
        fills = await ib.reqExecutionsAsync(ExecutionFilter(symbol=symbol))
        matching = [f for f in fills if int(f.execution.permId or 0) == perm_id]
        filled_qty = sum(float(f.execution.shares) for f in matching)
        avg_fill = (
            sum(float(f.execution.shares) * float(f.execution.price) for f in matching)
            / filled_qty if filled_qty else float(trade.orderStatus.avgFillPrice or 0)
        )
        if perm_id <= 0 or (status in FAILED_ORDER_STATUSES and filled_qty <= 0):
            raise OrderAbort(
                "ORDER NOT ACCEPTED: orderId={} permId={} status={}".format(
                    trade.order.orderId, perm_id, status
                ), code=4,
            )
        if filled_qty >= qty:
            status = "Filled"
        elif filled_qty > 0:
            status = ("PartiallyFilledCancelled" if status in FAILED_ORDER_STATUSES
                      else "PartiallyFilled")
        note = "{} @ {} route={} status={}".format(qty, price_description, route, status)
        trade_quota.upsert_order(
            symbol, args.action, perm_id,
            order_id=trade.order.orderId, status=status, qty=qty,
            filled_qty=filled_qty, limit_price=args.limit,
            avg_fill_price=avg_fill or None, route=route, note=note,
            override=quota_override_applied, replaces_perm_id=replaced_perm_id,
        )

        await ib.reqAllOpenOrdersAsync()
        final_position = snapshot_position(ib, symbol)
        remaining = [order_to_dict(t) for t in active_stock_trades(ib, symbol)]
        print("  ACCEPTED orderId={} permId={} status={} filled={} avgFill={}".format(
            trade.order.orderId, perm_id, status, filled_qty, avg_fill or 0
        ))
        print("  READBACK position={} avgCost={} activeOrders={}".format(
            final_position["quantity"], final_position["avgCost"], len(remaining)
        ))
        for row in remaining:
            print("  OPEN #{} permId={} {} remaining={} @ {} {}".format(
                row["orderId"], row["permId"], row["action"], row["remaining"],
                row["limitPrice"], row["status"],
            ))
        print("  {}".format(trade_quota.status_text(short=True)))
        return 0
    finally:
        if ib.isConnected():
            ib.disconnect()


def main():
    args = parse_args()
    try:
        if args.execute:
            with exclusive_order_lock():
                return asyncio.run(run(args))
        return asyncio.run(run(args))
    except OrderAbort as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return exc.code
    except RuntimeError as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
