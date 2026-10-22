#!/usr/bin/env python3
"""
拉期权链 + 扫策略机会（CC / CSP / 买 call / PMCC 腿）。只读。

从 IB Gateway 拉实时期权报价，自适应取真实 strike（避免造 strike 报 "No security
definition"），盘后空报价不报错。按 right + moneyness 输出策略相关指标。

用法:
  # CSP 扫描（OTM put，看年化 + 接货价）
  python option_chain.py AMD --right P --moneyness otm

  # CC 扫描（OTM call，看年化 + 被行权价）
  python option_chain.py INTC --right C --moneyness otm

  # 买 call 赔率（OTM call，看涨 X% 倍数）
  python option_chain.py PLTR --right C --moneyness otm --payoff

  # PMCC 长腿（deep-ITM call，看 extrinsic + 杠杆）
  python option_chain.py TSLA --right C --moneyness itm --dte-min 150 --dte-max 400

指标说明:
  ann%   = mid/strike × 365/dte × 100   （卖方年化，CC/CSP 视角）
  接货价  = strike − mid                  （CSP 被行权实际成本）
  extr   = ask − max(0, spot−strike)     （call 时间价值，PMCC 长腿要小）
  lev    = delta × spot / ask            （deep-ITM call 杠杆倍数）
  x@+N%  = max(0, spot×(1+N) − strike)/ask（买 call 到期赔率倍数）
"""
import asyncio, argparse, datetime, json, sys
from _ib import IB, Stock, Option

from _client_ids import client_id_for


def pick_expiries(expirations, today, lo, hi, monthly, n):
    out = []
    for e in sorted(expirations):
        d = (datetime.datetime.strptime(e, '%Y%m%d').date() - today).days
        if lo <= d <= hi and (not monthly or 15 <= int(e[6:8]) <= 21):
            out.append((e, d))
    return out[:n]


def pick_strikes(strikes, spot, moneyness, around):
    s = sorted(strikes)
    if moneyness == 'otm':
        ks = [k for k in s if k > spot][:around] if True else []
        return ks  # for puts caller flips
    if moneyness == 'itm':
        return [k for k in s if k < spot][-around:]
    if moneyness == 'atm':
        atm = min(s, key=lambda x: abs(x - spot))
        i = s.index(atm)
        return s[max(0, i - around): i + around + 1]
    # both
    return [k for k in s if k < spot][-around:] + [k for k in s if k > spot][:around]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('symbol')
    ap.add_argument('--right', choices=['C', 'P'], default='C')
    ap.add_argument('--dte-min', type=int, default=20)
    ap.add_argument('--dte-max', type=int, default=60)
    ap.add_argument('--no-monthly', action='store_true', help='不限月度到期（默认只取月度）')
    ap.add_argument('--around', type=int, default=4, help='OTM/ITM 取几档（ATM 上下）')
    ap.add_argument('--moneyness', choices=['otm', 'itm', 'atm', 'both'], default='otm')
    ap.add_argument('--expiries', type=int, default=2, help='取几个到期')
    ap.add_argument('--payoff', action='store_true', help='买 call 赔率列（+20/30/50%%）')
    ap.add_argument('--client-id', type=int, default=client_id_for("option_chain"))
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()

    ib = IB()
    try:
        await ib.connectAsync('127.0.0.1', 4001, clientId=args.client_id, readonly=True, timeout=25)
    except Exception as e:
        print(f'ERROR: cannot connect IB Gateway 127.0.0.1:4001 ({e}). Run: launchctl kickstart -k gui/$(id -u)/com.user.ibc-gateway', file=sys.stderr)
        sys.exit(1)
    # 2=frozen: 盘中返回实时，收盘后返回最后已知报价（option_chain 要能盘后/盘前跑）。
    # 用 1=live 时美股期权休市会全返回 bid/ask=-1（2026-06-08 教训：误判为"没订阅/接口不稳"）。
    ib.reqMarketDataType(2)

    stk = Stock(args.symbol, 'SMART', 'USD')
    await ib.qualifyContractsAsync(stk)
    [t] = await ib.reqTickersAsync(stk)
    spot = t.marketPrice() or t.close
    if not spot or spot != spot:
        print(f'ERROR: no spot for {args.symbol}', file=sys.stderr); sys.exit(1)

    chains = await ib.reqSecDefOptParamsAsync(stk.symbol, '', stk.secType, stk.conId)
    # 同一标的可能有多个 SMART 链（如 AMZN 的 '2AMZN' 特殊 tradingClass，
    # 到期/strike 稀疏）；必须优先 tradingClass == symbol 的标准链
    chain = next((c for c in chains if c.exchange == 'SMART' and c.tradingClass == stk.symbol),
                 next((c for c in chains if c.exchange == 'SMART'), chains[0]))
    today = datetime.date.today()
    exps = pick_expiries(chain.expirations, today, args.dte_min, args.dte_max,
                         not args.no_monthly, args.expiries)
    strikes_all = sorted(chain.strikes)

    # moneyness for puts: OTM put is strike < spot
    if args.right == 'P' and args.moneyness == 'otm':
        ks = [k for k in strikes_all if k < spot][-args.around:]
    elif args.right == 'P' and args.moneyness == 'itm':
        ks = [k for k in strikes_all if k > spot][:args.around]
    else:
        ks = pick_strikes(strikes_all, spot, args.moneyness, args.around)

    result = {'symbol': args.symbol, 'spot': round(spot, 2), 'expiries': []}
    print(f'{args.symbol} spot=${spot:.2f}  right={args.right} moneyness={args.moneyness}')
    for exp, dte in exps:
        cons = [Option(args.symbol, exp, k, args.right, 'SMART') for k in ks]
        await ib.qualifyContractsAsync(*cons)
        valid = [c for c in cons if c.conId]
        tk = await ib.reqTickersAsync(*valid)
        rows = []
        hdr = f'  --- {exp} ({dte}d) ---'
        print(hdr)
        cols = f'  {"STRK":>8s} {"BID":>7s} {"ASK":>7s} {"MID":>7s} {"IV%":>5s} {"delta":>6s} {"OTM%":>6s}'
        if args.right == 'P':
            cols += f' {"ann%":>6s} {"接货价":>8s}'
        elif args.moneyness == 'itm':
            cols += f' {"extr":>7s} {"lev":>5s}'
        elif args.payoff:
            cols += f' | {"x+20%":>5s} {"x+30%":>5s} {"x+50%":>5s}'
        else:
            cols += f' {"ann%":>6s}'
        print(cols)
        for c, x in zip(valid, tk):
            bid = x.bid if x.bid and x.bid > 0 else 0
            ask = x.ask if x.ask and x.ask > 0 else 0
            mid = (bid + ask) / 2 if bid and ask else (bid or ask)
            iv = (x.modelGreeks.impliedVol * 100) if x.modelGreeks and x.modelGreeks.impliedVol else 0
            dl = (x.modelGreeks.delta) if x.modelGreeks and x.modelGreeks.delta else 0
            otm = ((c.strike - spot) / spot * 100) if c.right == 'C' else ((spot - c.strike) / spot * 100)
            row = dict(strike=c.strike, bid=round(bid, 2), ask=round(ask, 2), mid=round(mid, 2),
                       iv=round(iv, 1), delta=round(dl, 3), otm_pct=round(otm, 1))
            line = f'  {c.strike:>8.1f} {bid:>7.2f} {ask:>7.2f} {mid:>7.2f} {iv:>5.0f} {dl:>6.2f} {otm:>5.1f}%'
            if c.right == 'P':
                ann = (mid / c.strike) * (365 / dte) * 100 if mid and dte else 0
                row['ann_pct'] = round(ann, 1); row['assign_cost'] = round(c.strike - mid, 2)
                line += f' {ann:>5.0f}% {c.strike - mid:>8.2f}'
            elif args.moneyness == 'itm':
                extr = ask - max(0, spot - c.strike) if ask else 0
                lev = dl * spot / ask if ask else 0
                row['extrinsic'] = round(extr, 2); row['leverage'] = round(lev, 2)
                line += f' {extr:>7.1f} {lev:>4.1f}x'
            elif args.payoff:
                def m(p):
                    return (max(0, spot * (1 + p) - c.strike) / ask) if ask else 0
                row['x20'] = round(m(.2), 1); row['x30'] = round(m(.3), 1); row['x50'] = round(m(.5), 1)
                line += f' | {m(.2):>4.1f}x {m(.3):>4.1f}x {m(.5):>4.1f}x'
            else:
                ann = (mid / c.strike) * (365 / dte) * 100 if mid and dte else 0
                row['ann_pct'] = round(ann, 1)
                line += f' {ann:>5.0f}%'
            if not args.json:
                print(line)
            rows.append(row)
        result['expiries'].append({'expiry': exp, 'dte': dte, 'rows': rows})
    ib.disconnect()
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main())
