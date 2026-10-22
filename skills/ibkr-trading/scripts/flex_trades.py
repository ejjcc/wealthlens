#!/usr/bin/env python3
"""
查询 IBKR 历史成交记录（IBKR Flex Web Service）

用法:
  python flex_trades.py [--symbol AAPL] [--since 2025-01-01] [--until 2026-05-01]
                        [--asset STK|OPT|CASH|BOND] [--refresh] [--json]

参数:
  --symbol     按 symbol 过滤（精确匹配，如 AAPL、TSLA）
  --since      起始交易日 YYYY-MM-DD
  --until      结束交易日 YYYY-MM-DD
  --asset      按资产类型过滤（STK 股票/ETF, OPT 期权, CASH 货币兑换, BOND 债券）
  --refresh    强制刷新缓存（默认 1 小时缓存）
  --json       JSON 输出，便于机器解析

返回字段:
  date, time, symbol, description, qty, price, proceeds, commission,
  orderType, transactionType, notes, assetCategory, currency,
  putCall, strike, expiry, exchange

注意:
  - Flex Query 的 Period 设置决定能拉到的最大历史范围
  - 缓存到 ~/.cache/ibkr-flex/<hash>.xml，1 小时有效
  - 同一 query_id + token 触发频率 IBKR 限制约 1 次/分钟
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _flex import get_credentials, get_flex_xml, parse_trades


def main():
    p = argparse.ArgumentParser(description="Query IBKR trade history via Flex Web Service")
    p.add_argument("--symbol", help="Filter by symbol (exact match)")
    p.add_argument("--since", help="Start date YYYY-MM-DD")
    p.add_argument("--until", help="End date YYYY-MM-DD")
    p.add_argument("--asset", choices=["STK", "OPT", "CASH", "BOND"], help="Asset category filter")
    p.add_argument("--refresh", action="store_true", help="Force cache refresh")
    p.add_argument("--json", action="store_true", help="JSON output")
    args = p.parse_args()

    token, qid = get_credentials()
    xml_str = get_flex_xml(token, qid, force_refresh=args.refresh)
    trades = parse_trades(xml_str, symbol=args.symbol)

    if args.since:
        trades = [t for t in trades if t["date"] >= args.since]
    if args.until:
        trades = [t for t in trades if t["date"] <= args.until]
    if args.asset:
        trades = [t for t in trades if t["assetCategory"] == args.asset]

    if args.json:
        out = {
            "count": len(trades),
            "filters": {
                "symbol": args.symbol,
                "since": args.since,
                "until": args.until,
                "asset": args.asset,
            },
            "trades": trades,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return

    if not trades:
        print("No trades found.")
        return

    print(f"=== {len(trades)} trades ===")
    for t in trades:
        sym = t["symbol"]
        if t.get("putCall"):
            sym = f"{sym} {t.get('expiry','')} {t.get('strike','')}{t['putCall']}"
        comm = t.get("commission", 0)
        print(
            f"{t['date']} {t['orderType']:<6} {sym:<28} "
            f"qty={t['qty']:<10} px={t['price']:<10} "
            f"proceeds={t['proceeds']:<12} comm={comm:<8} {t.get('notes','')}"
        )

    # Summary if filtered to single symbol
    if args.symbol:
        total_qty = sum(t["qty"] for t in trades)
        total_cost = sum(-t["proceeds"] - t["commission"] for t in trades)
        print(f"\n--- Summary {args.symbol} ---")
        print(f"  Total qty:  {total_qty:.4f}")
        print(f"  Total cost: ${total_cost:.2f} (incl commission)")
        if total_qty:
            print(f"  Avg cost:   ${total_cost/total_qty:.4f}")


if __name__ == "__main__":
    main()
