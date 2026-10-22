#!/usr/bin/env python3
"""
查询 IBKR 当日成交（IB Gateway 实时 API）

用法:
  python live_fills.py [--symbol NET] [--asset STK|OPT] [--json]

参数:
  --symbol     按 symbol 过滤
  --asset      按资产类型过滤（STK 股票/ETF, OPT 期权, BOND 债券, CASH 货币）
  --json       JSON 输出

注意:
  - 仅返回当日成交。昨日及更早请用 flex_trades.py
  - 需要 IB Gateway 运行在 4001 端口（见 _gw.py 启动指引）
  - 不需要 OPRA / 市场数据订阅，只需要账户级 API 权限
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _gw import connect, run


async def fetch():
    ib = await connect(role="live_fills")
    try:
        fills = ib.fills()
        out = []
        for f in fills:
            c = f.contract
            e = f.execution
            out.append({
                "time": str(e.time),
                "symbol": c.symbol,
                "secType": c.secType,
                "right": getattr(c, "right", "") or "",
                "strike": getattr(c, "strike", 0) or 0,
                "expiry": getattr(c, "lastTradeDateOrContractMonth", "") or "",
                "side": e.side,
                "qty": e.shares,
                "price": e.price,
                "avgPrice": e.avgPrice,
                "currency": c.currency,
                "exchange": e.exchange,
                "execId": e.execId,
                "permId": e.permId,
            })
        return out
    finally:
        ib.disconnect()


def main():
    p = argparse.ArgumentParser(description="Today fills via IB Gateway")
    p.add_argument("--symbol", help="Filter by symbol")
    p.add_argument("--asset", choices=["STK", "OPT", "BOND", "CASH"], help="Asset filter")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    fills = run(fetch())
    if args.symbol:
        fills = [f for f in fills if f["symbol"] == args.symbol]
    if args.asset:
        fills = [f for f in fills if f["secType"] == args.asset]

    if args.json:
        print(json.dumps({"count": len(fills), "fills": fills}, ensure_ascii=False, indent=2, default=str))
        return

    if not fills:
        msg = "No fills today"
        if args.symbol:
            msg += f" for {args.symbol}"
        print(msg)
        return

    print(f"=== {len(fills)} fills today ===")
    for f in fills:
        sym = f["symbol"]
        if f["secType"] == "OPT":
            sym = f"{sym} {f['expiry']} {int(f['strike'])}{f['right']}"
        print(
            f"{str(f['time'])[:19]:<20} {f['side']:<4} {sym:<28} "
            f"qty={f['qty']:<8} px={f['price']:<10} {f['exchange']}"
        )


if __name__ == "__main__":
    main()
