#!/usr/bin/env python3
"""
查询 IBKR 当前持仓（含 markPrice / marketValue / unrealizedPNL）

用法:
  python live_positions.py [--symbol NET] [--asset STK|OPT] [--json]

参数:
  --symbol   按 symbol 过滤
  --asset    按资产类型过滤
  --json     JSON 输出

注意:
  - 走 IB Gateway portfolio()，包含 markPrice 和 PnL 实时计算
  - 不需要 OPRA 订阅；股票 markPrice 用免费 IBKR-PRO 实时报价
  - 期权 markPrice 在没订阅 OPRA 时可能返回 0 或最近收盘
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _ib import BACKEND_NAME, IB
from _gw import connect, run


async def fetch():
    # Read-only migration pilot (phase 2). Backend selection lives in _ib
    # (ib_async preferred when importable; IBKR_PYTHON_BACKEND overrides).
    ib = await connect(role="live_positions", ib_factory=IB)
    try:
        port = ib.portfolio()
        out = []
        for p in port:
            c = p.contract
            out.append({
                "symbol": c.symbol,
                "secType": c.secType,
                "right": getattr(c, "right", "") or "",
                "strike": getattr(c, "strike", 0) or 0,
                "expiry": getattr(c, "lastTradeDateOrContractMonth", "") or "",
                "qty": p.position,
                "avgCost": p.averageCost,
                "marketPrice": p.marketPrice,
                "marketValue": p.marketValue,
                "unrealizedPNL": p.unrealizedPNL,
                "realizedPNL": p.realizedPNL,
                "currency": c.currency,
                "account": p.account,
            })
        return out
    finally:
        ib.disconnect()


def main():
    p = argparse.ArgumentParser(description="Current positions via IB Gateway")
    p.add_argument("--symbol", help="Filter by symbol")
    p.add_argument("--asset", choices=["STK", "OPT", "BOND"], help="Asset filter")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    positions = run(fetch())
    if args.symbol:
        positions = [pp for pp in positions if pp["symbol"] == args.symbol]
    if args.asset:
        positions = [pp for pp in positions if pp["secType"] == args.asset]

    if args.json:
        print(json.dumps({"backend": BACKEND_NAME, "count": len(positions), "positions": positions}, ensure_ascii=False, indent=2, default=str))
        return

    if not positions:
        msg = "No positions"
        if args.symbol:
            msg += f" for {args.symbol}"
        print(msg)
        return

    print(f"=== {len(positions)} positions ===")
    for p in positions:
        sym = p["symbol"]
        if p["secType"] == "OPT":
            sym = f"{sym} {p['expiry']} {int(p['strike'])}{p['right']}"
        print(
            f"{sym:<28} qty={p['qty']:<10.4f} avg=${p['avgCost']:<10.4f} "
            f"mark=${p['marketPrice']:<10.4f} mv=${p['marketValue']:<12.2f} "
            f"pnl=${p['unrealizedPNL']:<10.2f}"
        )


if __name__ == "__main__":
    main()
