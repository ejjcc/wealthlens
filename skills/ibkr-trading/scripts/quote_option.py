#!/usr/bin/env python3
"""
查询 IBKR 期权报价 (bid/ask/mid/IV/Greeks)

用法:
  # 单合约
  quote_option.py NVDA -e 2027-01-15 -k 320 -r C

  # 多个 strikes (一个到期, 多个 strike, 看 chain)
  quote_option.py NVDA -e 2027-01-15 --strikes 280,300,320,340 -r C

  # 多个到期 (一个 strike, 多个到期, 看 term structure)
  quote_option.py NVDA -k 320 --expiries 2027-01-15,2027-06-18 -r C

  # JSON 输出
  quote_option.py NVDA -e 2027-01-15 -k 320 -r C --json

输出字段:
  bid / ask / mid / IV / Delta / Theta / Days / OTM%

注意:
  - IB Gateway 必须运行
  - 周末 / 收盘后 bid/ask 可能为 0 或 None, 用 close price 兜底
  - 期权数据订阅 OPRA 必须激活
"""
import argparse
import asyncio
import json
import math
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _gw import connect, run


def _is_nan(x):
    """IB 客户端用 nan 表示无数据"""
    return x is None or (isinstance(x, float) and math.isnan(x))


def _val(x):
    """IB 客户端用 nan / -1 表示无数据"""
    if _is_nan(x):
        return None
    if isinstance(x, (int, float)) and x == -1:
        return None
    return x


async def query_option(ib, symbol, expiry, strike, right, exchange='SMART'):
    from _ib import Option
    opt = Option(symbol, expiry, strike, right, exchange)
    await ib.qualifyContractsAsync(opt)
    ticker = ib.reqMktData(opt, '', False, False)
    await asyncio.sleep(2.5)

    result = {
        'symbol': symbol,
        'expiry': expiry,
        'strike': strike,
        'right': right,
        'bid': _val(ticker.bid),
        'ask': _val(ticker.ask),
        'last': _val(ticker.last),
        'close': _val(ticker.close),
        'iv': _val(ticker.impliedVolatility),
    }
    if ticker.modelGreeks:
        result['delta'] = _val(ticker.modelGreeks.delta)
        result['theta'] = _val(ticker.modelGreeks.theta)
        result['vega'] = _val(ticker.modelGreeks.vega)
        result['gamma'] = _val(ticker.modelGreeks.gamma)
        # ticker.impliedVolatility 经常空, fallback 到 modelGreeks.impliedVol
        if result['iv'] is None:
            result['iv'] = _val(ticker.modelGreeks.impliedVol)
    else:
        result['delta'] = result['theta'] = result['vega'] = result['gamma'] = None
    return result


async def query_underlying(ib, symbol):
    from _ib import Stock
    stk = Stock(symbol, 'SMART', 'USD')
    await ib.qualifyContractsAsync(stk)
    t = ib.reqMktData(stk, '', False, False)
    await asyncio.sleep(2)
    bid = _val(t.bid)
    ask = _val(t.ask)
    mid = (bid + ask) / 2 if (bid is not None and ask is not None) else None
    return _val(t.last) or _val(t.close) or mid


async def main_async(args):
    ib = await connect(role="quote_option")

    spot = await query_underlying(ib, args.symbol)

    # Parse expiries / strikes (allow comma-separated)
    expiries = [e.strip().replace('-', '') for e in (args.expiries or args.expiry).split(',')]
    strike_strs = (args.strikes or str(args.strike)).split(',')
    strikes = [float(s.strip()) for s in strike_strs]

    results = []
    for exp in expiries:
        for strike in strikes:
            try:
                r = await query_option(ib, args.symbol, exp, strike, args.right.upper())
                if spot is not None:
                    if args.right.upper() == 'C':
                        r['moneyness'] = (strike - spot) / spot
                    else:
                        r['moneyness'] = (spot - strike) / spot
                results.append(r)
            except Exception as e:
                results.append({
                    'symbol': args.symbol, 'expiry': exp, 'strike': strike,
                    'right': args.right.upper(), 'error': str(e)[:80],
                })

    ib.disconnect()
    return spot, results


def format_table(spot, results, symbol):
    spot_str = f"${spot:.2f}" if spot is not None else "N/A"
    print(f"\n{symbol} spot: {spot_str}")
    print()
    header = f"{'Expiry':<12} {'Strike':>7} {'R':>2} {'Bid':>7} {'Ask':>7} {'Mid':>7} {'Close':>7} {'IV':>7} {'Δ':>6} {'Θ/d':>6} {'Days':>5} {'OTM%':>6}"
    print(header)
    print('-' * len(header))
    for r in results:
        if 'error' in r:
            print(f"  {r['expiry'][:8]} ${r['strike']:>5.0f} {r['right']} ERR: {r['error']}")
            continue
        exp = r['expiry']
        exp_fmt = f"{exp[:4]}-{exp[4:6]}-{exp[6:8]}"
        try:
            days = (datetime.strptime(exp, '%Y%m%d') - datetime.now()).days
        except:
            days = 0
        bid = r.get('bid')
        ask = r.get('ask')
        mid = (bid + ask) / 2 if (bid is not None and ask is not None) else None
        close = r.get('close')
        iv = r.get('iv')
        delta = r.get('delta')
        theta = r.get('theta')
        moneyness = r.get('moneyness')

        def fmt(v, w=6, p=2, suffix=''):
            return f"{v:>{w}.{p}f}{suffix}" if v is not None else f"{'—':>{w+len(suffix)}}"

        def fmt_pct(v, w=6):
            return f"{v:>{w-1}.1%}" if v is not None else f"{'—':>{w}}"

        print(f"{exp_fmt:<12} ${r['strike']:>5.0f} {r['right']:>2} "
              f"{fmt(bid)} {fmt(ask)} {fmt(mid)} {fmt(close)} "
              f"{fmt_pct(iv, 7)} {fmt(delta, 5)} {fmt(theta, 6)} "
              f"{days:>5} {fmt_pct(moneyness, 6)}")

    print()
    print("提示:")
    print("  Mid × 100 = 1 张合约现金量")
    print("  Δ delta = 标的涨 $1 期权涨多少 (Sell CC 时希望 |Δ| 小)")
    print("  Θ/d = 每天时间衰减 (Sell 方收 +, Buy 方付 -)")
    print("  IV = 隐含波动率 (Sell 期权希望 IV 高)")
    print("  OTM% = 行权价距现价的距离百分比")


def main():
    p = argparse.ArgumentParser(description="Query IBKR option quotes")
    p.add_argument('symbol', help='Underlying symbol (NVDA, AAPL, QQQ, etc)')
    p.add_argument('-e', '--expiry', help='Single expiry YYYY-MM-DD or YYYYMMDD')
    p.add_argument('--expiries', help='Multiple expiries, comma-separated')
    p.add_argument('-k', '--strike', type=float, help='Single strike')
    p.add_argument('--strikes', help='Multiple strikes, comma-separated (e.g. 280,300,320)')
    p.add_argument('-r', '--right', choices=['C', 'P', 'c', 'p'], required=True, help='C=Call, P=Put')
    p.add_argument('--json', action='store_true', help='JSON output')
    args = p.parse_args()

    if not args.expiry and not args.expiries:
        p.error('Must specify -e/--expiry or --expiries')
    if args.strike is None and not args.strikes:
        p.error('Must specify -k/--strike or --strikes')

    spot, results = run(main_async(args))

    if args.json:
        print(json.dumps({'spot': spot, 'results': results}, indent=2, default=str))
    else:
        format_table(spot, results, args.symbol)


if __name__ == '__main__':
    main()
