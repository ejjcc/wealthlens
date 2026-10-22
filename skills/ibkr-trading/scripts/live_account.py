#!/usr/bin/env python3
"""
查询 IBKR 账户资金摘要

用法:
  python live_account.py [--json]

输出关键字段:
  NetLiquidation       净资产（=权益总值）
  TotalCashValue       现金（负数=已用 margin）
  AvailableFunds       可用资金（开新仓不会触发追保的额度）
  BuyingPower          购买力（margin 杠杆后的可用买入额度）
  GrossPositionValue   持仓市值
  OptionMarketValue    期权头寸市值（负数=净 short 期权）
  InitMarginReq        初始保证金需求
  MaintMarginReq       维持保证金需求
  EquityWithLoanValue  权益含贷款价值
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _gw import connect, run

KEYS = [
    "NetLiquidation",
    "TotalCashValue",
    "AvailableFunds",
    "BuyingPower",
    "GrossPositionValue",
    "OptionMarketValue",
    "InitMarginReq",
    "MaintMarginReq",
    "EquityWithLoanValue",
    "ExcessLiquidity",
]


async def fetch():
    ib = await connect(role="live_account")
    try:
        summary = await ib.accountSummaryAsync()
        out = {}
        for s in summary:
            if s.tag in KEYS and s.currency in ("USD", "BASE"):
                out[s.tag] = {"value": s.value, "currency": s.currency}
        return out
    finally:
        ib.disconnect()


def main():
    p = argparse.ArgumentParser(description="Account summary via IB Gateway")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    acct = run(fetch())

    if args.json:
        print(json.dumps(acct, ensure_ascii=False, indent=2))
        return

    print("=== Account Summary ===")
    for k in KEYS:
        if k in acct:
            v = acct[k]
            try:
                amount = float(v["value"])
                print(f"  {k:<22} {amount:>15,.2f} {v['currency']}")
            except (ValueError, TypeError):
                print(f"  {k:<22} {v['value']:>15} {v['currency']}")


if __name__ == "__main__":
    main()
