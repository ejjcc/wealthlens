#!/usr/bin/env python3
"""
找 roll / 开仓候选（position-aware，只读）。

连 IB Gateway 读持仓，自动判模式：
  - 有短期权     → roll 模式：算 buy-to-close + 各到期/strike 的 net credit/debit
  - 无短、有长期权 → spread 模式：找 short 腿组垂直价差（OTM 相对长腿 strike）
  - 无期权、有正股 → new_short 模式：找 covered call / cash-covered put 候选

用法:
  python option_roll.py META                     # 自动判模式（META 有短 650C → roll）
  python option_roll.py NVDA                      # 正股 → 找 covered call
  python option_roll.py AMD --right P            # CSP 短腿 → roll（put）
  python option_roll.py TSLA --strike 460 --expiry 20260717 --right C  # 手动指定短腿
  python option_roll.py META --json

指标:
  net      = 新腿 bid − 旧腿 ask     （roll：正=net credit，负=debit）
  ann%     = bid/strike × 365/dte    （new_short/spread：卖方年化）
  接货价    = strike − bid            （CSP）/ 被行权价 = strike + bid（CC）

注意:
  - reqMarketDataType(2) frozen：盘后/休市返回最后已知报价，不会全 -1（对齐 option_chain.py）
  - earnings 不取（本机无 yfinance）；撞财报风险请配合 quote_option.py / 日历自查
  - 改编自 staskh/trading_skills（MIT）roll.py：换 ib_insync、复用 _gw、加 frozen、
    去掉 strike %5==0 过滤（低价标的 $0.5/$1 档会被误删）
"""
import argparse
import asyncio
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _ib import IB, Option, Stock
from _gw import connect


def _dte(exp: str) -> int:
    return (datetime.datetime.strptime(exp[:8], "%Y%m%d").date() - datetime.date.today()).days


async def _spot(ib: IB, symbol: str) -> float:
    stk = Stock(symbol, "SMART", "USD")
    await ib.qualifyContractsAsync(stk)
    [t] = await ib.reqTickersAsync(stk)
    px = t.marketPrice() or t.close
    return px if px and px == px else 0.0  # nan guard


async def _chain(ib: IB, symbol: str) -> dict:
    stk = Stock(symbol, "SMART", "USD")
    await ib.qualifyContractsAsync(stk)
    chains = await ib.reqSecDefOptParamsAsync(stk.symbol, "", "STK", stk.conId)
    if not chains:
        return {"expirations": [], "strikes": []}
    c = next((x for x in chains if x.exchange == "SMART"), chains[0])
    return {"expirations": sorted(c.expirations), "strikes": sorted(c.strikes)}


async def _quotes(ib: IB, symbol: str, expiry: str, strikes: list, right: str) -> list:
    """Fetch bid/ask/mid for strikes at one expiry. Drops non-existent contracts."""
    cons = [Option(symbol, expiry, k, right, "SMART") for k in strikes]
    try:
        await asyncio.wait_for(ib.qualifyContractsAsync(*cons), timeout=10)
    except asyncio.TimeoutError:
        return []
    valid = [c for c in cons if c.conId]
    if not valid:
        return []
    try:
        tickers = await asyncio.wait_for(ib.reqTickersAsync(*valid), timeout=15)
    except asyncio.TimeoutError:
        return []
    out = []
    for t in tickers:
        if t.contract is None:
            continue
        bid = t.bid if t.bid and t.bid > 0 else 0
        ask = t.ask if t.ask and t.ask > 0 else 0
        mid = (bid + ask) / 2 if bid and ask else (bid or ask)
        out.append({"strike": t.contract.strike, "expiry": t.contract.lastTradeDateOrContractMonth,
                    "bid": round(bid, 2), "ask": round(ask, 2), "mid": round(mid, 2)})
    return sorted(out, key=lambda x: x["strike"])


def _evaluate_short(quotes: list, spot: float, right: str, dte: float) -> list:
    """Score short-open candidates (covered call / CSP / spread short leg)."""
    cands = []
    for q in quotes:
        if q["bid"] <= 0:
            continue
        k, premium = q["strike"], q["bid"]
        otm = ((k - spot) / spot * 100) if right == "C" else ((spot - k) / spot * 100)
        if otm < 0:  # skip ITM
            continue
        ann = (premium / spot) * (365 / max(dte, 1 / 24)) * 100
        # score: OTM safety (cap 10%) + premium + 21-60 DTE sweet spot
        safety = min(otm, 10) * 2 - (max(otm - 15, 0) * 0.5)
        score = safety + min(premium * 10, 30) + (10 if 21 <= dte <= 60 else 5)
        cands.append({"strike": k, "expiry": q["expiry"], "bid": premium, "ask": q["ask"],
                      "otm_pct": round(otm, 1), "ann_pct": round(ann, 1),
                      "assign_px": round(k - premium if right == "P" else k + premium, 2),
                      "dte": int(dte), "score": round(score, 1)})
    return sorted(cands, key=lambda x: x["score"], reverse=True)


# ---------- position detection ----------
def _short_opt(ib, symbol, account):
    ps = ib.positions(account=account) if account else ib.positions()
    shorts = [p for p in ps if p.contract.symbol == symbol
              and p.contract.secType == "OPT" and p.position < 0]
    if not shorts:
        return None
    shorts.sort(key=lambda p: p.contract.lastTradeDateOrContractMonth)
    c = shorts[0].contract
    mult = int(c.multiplier) if c.multiplier else 100
    return {"strike": c.strike, "expiry": c.lastTradeDateOrContractMonth, "right": c.right,
            "quantity": int(shorts[0].position), "avg_cost": shorts[0].avgCost / mult}


def _long_opt(ib, symbol, right, account):
    ps = ib.positions(account=account) if account else ib.positions()
    longs = [p for p in ps if p.contract.symbol == symbol and p.contract.secType == "OPT"
             and p.position > 0 and p.contract.right == right]
    if not longs:
        return None
    longs.sort(key=lambda p: p.contract.lastTradeDateOrContractMonth)
    c = longs[0].contract
    return {"strike": c.strike, "expiry": c.lastTradeDateOrContractMonth,
            "right": c.right, "quantity": int(longs[0].position)}


def _long_stock(ib, symbol, account):
    ps = ib.positions(account=account) if account else ib.positions()
    for p in ps:
        if p.contract.symbol == symbol and p.contract.secType == "STK" and p.position > 0:
            return {"shares": int(p.position), "avg_cost": p.avgCost}
    return None


# ---------- modes ----------
async def _mode_roll(ib, symbol, cur, chain):
    spot = await _spot(ib, symbol)
    cq = await _quotes(ib, symbol, cur["expiry"], [cur["strike"]], cur["right"])
    if not cq:
        return {"error": f"无法取 {symbol} {cur['strike']}{cur['right']} {cur['expiry']} 报价"}
    buy_to_close = cq[0]["ask"] or cq[0]["mid"]
    future = [e for e in chain["expirations"] if e > cur["expiry"]][:5]
    if not future:
        return {"error": "无更远到期可滚"}
    k0 = cur["strike"]
    if cur["right"] == "C":
        ks = [s for s in chain["strikes"] if k0 - 10 <= s <= k0 + 50]
    else:
        ks = [s for s in chain["strikes"] if k0 - 50 <= s <= k0 + 10]
    ks = sorted(ks)[:12]
    roll = {}
    for e in future:
        qs = await _quotes(ib, symbol, e, ks, cur["right"])
        roll[e] = [{"strike": q["strike"], "expiry": e, "sell": q["bid"], "buy": buy_to_close,
                    "net": round(q["bid"] - buy_to_close, 2),
                    "net_type": "credit" if q["bid"] - buy_to_close > 0 else "debit"}
                   for q in qs if q["bid"] > 0]
    return {"mode": "roll", "symbol": symbol, "spot": round(spot, 2), "current": cur,
            "buy_to_close": buy_to_close, "roll_candidates": roll, "expirations_analyzed": future}


async def _mode_new_short(ib, symbol, stock, right, chain):
    spot = await _spot(ib, symbol)
    today = datetime.date.today().strftime("%Y%m%d")
    future = [e for e in chain["expirations"] if e > today][:6]
    if right == "C":
        ks = [s for s in chain["strikes"] if spot <= s <= spot * 1.20]
    else:
        ks = [s for s in chain["strikes"] if spot * 0.80 <= s <= spot]
    ks = sorted(ks)[:15]
    by_exp = {}
    for e in future:
        qs = await _quotes(ib, symbol, e, ks, right)
        c = _evaluate_short(qs, spot, right, _dte(e))
        if c:
            by_exp[e] = c
    return {"mode": "new_short", "symbol": symbol, "spot": round(spot, 2), "right": right,
            "long_stock": stock, "candidates_by_expiry": by_exp, "expirations_analyzed": future}


async def _mode_spread(ib, symbol, long_opt, right, chain):
    spot = await _spot(ib, symbol)
    if not spot:
        spot = long_opt["strike"]
    target = [e for e in chain["expirations"] if e >= long_opt["expiry"]][:3]
    k0 = long_opt["strike"]
    if right == "C":
        ks = [s for s in chain["strikes"] if k0 < s <= spot * 2.0]
    else:
        ks = [s for s in chain["strikes"] if spot * 0.5 <= s < k0]
    ks = sorted(ks)[:15]
    by_exp = {}
    for e in target:
        qs = await _quotes(ib, symbol, e, ks, right)
        c = _evaluate_short(qs, spot, right, _dte(e))
        if c:
            by_exp[e] = c
    return {"mode": "spread", "symbol": symbol, "spot": round(spot, 2), "right": right,
            "long_option": long_opt, "candidates_by_expiry": by_exp, "expirations_analyzed": target}


async def find(symbol, account, strike, expiry, right):
    ib = await connect(role="option_roll")
    try:
        ib.reqMarketDataType(2)  # frozen: 盘后返回最后已知报价（null 加固）
        chain = await _chain(ib, symbol)
        if not chain["expirations"]:
            return {"error": f"{symbol} 无期权链"}
        if strike and expiry:  # 手动指定短腿
            cur = {"strike": strike, "expiry": expiry, "right": right, "quantity": -1, "avg_cost": 0}
            return await _mode_roll(ib, symbol, cur, chain)
        cur = _short_opt(ib, symbol, account)
        if cur:
            return await _mode_roll(ib, symbol, cur, chain)
        lopt = _long_opt(ib, symbol, right, account)
        if lopt:
            return await _mode_spread(ib, symbol, lopt, right, chain)
        stk = _long_stock(ib, symbol, account)
        if stk:
            return await _mode_new_short(ib, symbol, stk, right, chain)
        return {"error": f"{symbol} 无相关持仓；用 --strike/--expiry 手动指定短腿"}
    finally:
        ib.disconnect()


# ---------- premium floor filter ----------
def _apply_min_total(res, min_total):
    """Drop candidates whose total premium (per-share x100 x contracts) < min_total USD.
    Credit-collecting legs only; debit rolls (defensive) are kept."""
    if min_total <= 0 or "error" in res:
        return res
    m = res["mode"]
    if m == "roll":
        qty = abs(res["current"].get("quantity") or 0) or 1
        res["roll_candidates"] = {
            e: [x for x in cands if x["net"] <= 0 or x["net"] * 100 * qty >= min_total]
            for e, cands in res["roll_candidates"].items()}
    else:
        if m == "new_short":
            qty = (res.get("long_stock") or {}).get("shares", 0) // 100 or 1
        else:
            qty = abs((res.get("long_option") or {}).get("quantity") or 0) or 1
        res["candidates_by_expiry"] = {
            e: kept for e, cands in res["candidates_by_expiry"].items()
            if (kept := [x for x in cands if x["bid"] * 100 * qty >= min_total])}
    res["min_total_filter"] = {"min_total": min_total, "contracts": qty}
    return res


# ---------- print ----------
def _print(res):
    if "error" in res:
        print(f"ERROR: {res['error']}")
        return
    mt = res.get("min_total_filter")
    if mt:
        print(f"(已隐藏总权利金 < ${mt['min_total']:.0f} 的收租候选，按 ×{mt['contracts']} 张计；--min-total 0 关闭)")
    m, sym, spot = res["mode"], res["symbol"], res["spot"]
    if m == "roll":
        c = res["current"]
        print(f"{sym} ${spot}  roll {c['strike']:.0f}{c['right']} {c['expiry']} "
              f"(×{c['quantity']:+d})  buy-to-close ${res['buy_to_close']:.2f}")
        for e, cands in res["roll_candidates"].items():
            if not cands:
                continue
            print(f"  --- {e} ({_dte(e)}d) ---")
            print(f"  {'STRK':>7s} {'SELL':>6s} {'NET':>7s}  类型")
            for x in cands:
                print(f"  {x['strike']:>7.1f} {x['sell']:>6.2f} {x['net']:>+7.2f}  {x['net_type']}")
        return
    # new_short / spread
    tag = "covered call/put" if m == "new_short" else "vertical spread short leg"
    print(f"{sym} ${spot}  {m} ({tag})  right={res['right']}")
    for e, cands in res["candidates_by_expiry"].items():
        print(f"  --- {e} ({_dte(e)}d) ---")
        print(f"  {'STRK':>7s} {'BID':>6s} {'OTM%':>6s} {'ann%':>6s} {'接货/行权':>9s} {'score':>6s}")
        for x in cands:
            print(f"  {x['strike']:>7.1f} {x['bid']:>6.2f} {x['otm_pct']:>5.1f}% "
                  f"{x['ann_pct']:>5.0f}% {x['assign_px']:>9.2f} {x['score']:>6.1f}")


def main():
    ap = argparse.ArgumentParser(description="Position-aware roll / covered-call finder (read-only)")
    ap.add_argument("symbol")
    ap.add_argument("--strike", type=float, help="手动指定短腿 strike")
    ap.add_argument("--expiry", help="手动指定短腿到期 YYYYMMDD")
    ap.add_argument("--right", choices=["C", "P"], default="C")
    ap.add_argument("--account", help="指定账户 ID")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--min-total", type=float, default=500.0,
                    help="过滤总权利金（net/bid ×100×张数）低于此金额的候选，USD；0 关闭（默认 500）")
    args = ap.parse_args()

    from _gw import run
    res = run(find(args.symbol.upper(), args.account, args.strike, args.expiry, args.right))
    res = _apply_min_total(res, args.min_total)
    res["generated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False, default=str))
    else:
        _print(res)


if __name__ == "__main__":
    main()
