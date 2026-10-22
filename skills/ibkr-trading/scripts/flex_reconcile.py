#!/usr/bin/env python3
"""
对账 IBKR 真实持仓 vs 本地 md frontmatter

用法:
  python flex_reconcile.py --symbol AAPL [--md-path docs/positions/US.AAPL.md]
                           [--refresh] [--json]

参数:
  --symbol     必填，要对账的 symbol
  --md-path    本地 md 文件路径。不指定时自动猜（docs/positions/{US|HK|CRYPTO}.{SYMBOL}.md）
  --refresh    强制刷新 Flex 缓存
  --json       JSON 输出

输出:
  - IBKR 当前持仓（OpenPosition 节点）
  - 历史成交累计（Trade 节点求和）
  - md frontmatter 中的 qty / avg_cost / last_updated
  - 差异列表（diff），如果 md 与 IBKR 不一致

退出码:
  0 - 完全对账成功
  1 - 凭证缺失或 API 错误
  2 - 检测到差异，或缺少持仓/frontmatter 导致对账不完整
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _flex import get_credentials, get_flex_xml, parse_trades, parse_open_positions  # noqa: E402


def parse_frontmatter(md_path: str):
    if not os.path.exists(md_path):
        return None
    with open(md_path) as f:
        text = f.read()
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return None
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.strip().startswith("-"):
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm


def guess_md_path(symbol: str) -> str:
    """Search docs/positions/<MARKET>.<SYMBOL>.md, walking up to find docs/."""
    cwd = os.getcwd()
    for d in [cwd] + [os.path.dirname(cwd), os.path.dirname(os.path.dirname(cwd))]:
        for prefix in ["US", "HK", "CRYPTO", "DE"]:
            cand = os.path.join(d, "docs", "positions", f"{prefix}.{symbol}.md")
            if os.path.exists(cand):
                return cand
    return ""


def main():
    p = argparse.ArgumentParser(description="Reconcile IBKR position vs local md")
    p.add_argument("--symbol", required=True)
    p.add_argument("--md-path", help="Local md file path (auto-guessed if omitted)")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    try:
        token, qid = get_credentials()
        xml_str = get_flex_xml(token, qid, force_refresh=args.refresh)

        # Current position from Flex
        positions = parse_open_positions(xml_str, symbol=args.symbol)
        pos = positions[0] if positions else None

        # Trade history aggregate
        trades = parse_trades(xml_str, symbol=args.symbol)
    except RuntimeError as exc:
        if args.json:
            print(json.dumps({
                "symbol": args.symbol,
                "error": str(exc),
                "reconciled": False,
            }, ensure_ascii=False, indent=2))
        else:
            print(f"ERROR: reconciliation aborted: {exc}", file=sys.stderr)
        sys.exit(1)
    total_qty = sum(t["qty"] for t in trades)
    total_cost = sum(-t["proceeds"] - t["commission"] for t in trades)
    avg_cost = total_cost / total_qty if total_qty else 0

    # md frontmatter
    md_path = args.md_path or guess_md_path(args.symbol)
    fm = parse_frontmatter(md_path) if md_path else None

    diffs = []
    if pos and fm:
        try:
            md_qty = float(fm.get("qty", "0"))
            if abs(md_qty - pos["qty"]) > 0.001:
                diffs.append({
                    "field": "qty",
                    "md": md_qty,
                    "flex": pos["qty"],
                    "delta": round(pos["qty"] - md_qty, 4),
                })
        except (ValueError, TypeError):
            pass
        try:
            md_avg = float(fm.get("avg_cost", "0"))
            if abs(md_avg - pos["avgCost"]) > 0.005:
                diffs.append({
                    "field": "avg_cost",
                    "md": md_avg,
                    "flex": round(pos["avgCost"], 4),
                    "delta": round(pos["avgCost"] - md_avg, 4),
                })
        except (ValueError, TypeError):
            pass

    result = {
        "symbol": args.symbol,
        "flex_position": pos,
        "flex_trades_summary": {
            "count": len(trades),
            "total_qty": round(total_qty, 4),
            "total_cost": round(total_cost, 2),
            "avg_cost": round(avg_cost, 4),
        },
        "md_path": md_path or None,
        "md_frontmatter": fm,
        "diffs": diffs,
        "reconciled": not diffs and pos is not None and fm is not None,
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"=== {args.symbol} reconciliation ===\n")
        if pos:
            print(f"IBKR (Flex OpenPosition):")
            print(f"  qty:        {pos['qty']}")
            print(f"  avg_cost:   ${pos['avgCost']:.4f}")
            print(f"  market:     ${pos['markPrice']:.4f}")
            print(f"  market_val: ${pos['markPrice']*pos['qty']:.2f}")
            print(f"  pnl:        ${pos['fifoPnlUnrealized']:.2f}")
        else:
            print(f"  ⚠ No open position for {args.symbol} in Flex")

        print(f"\nIBKR (Trade history aggregate):")
        print(f"  count:      {len(trades)}")
        print(f"  total_qty:  {total_qty:.4f}")
        print(f"  total_cost: ${total_cost:.2f}")
        if total_qty:
            print(f"  avg_cost:   ${avg_cost:.4f}")

        if md_path:
            print(f"\nLocal md ({md_path}):")
            if fm:
                print(f"  qty:          {fm.get('qty')}")
                print(f"  avg_cost:     {fm.get('avg_cost')}")
                print(f"  last_updated: {fm.get('last_updated')}")
            else:
                print(f"  ⚠ Failed to parse frontmatter")
        else:
            print(f"\n⚠ md file not found for {args.symbol}")

        if diffs:
            print(f"\n⚠ Diffs:")
            for d in diffs:
                print(f"  {d['field']}: md={d['md']} vs flex={d['flex']} (delta={d['delta']:+.4f})")
        elif pos and fm:
            print(f"\n✅ Reconciled (md matches IBKR)")

    # Exit cleanly only for a complete reconciliation. Missing Flex position or
    # local frontmatter is incomplete, not a successful no-diff result.
    sys.exit(0 if result["reconciled"] else 2)


if __name__ == "__main__":
    main()
