#!/usr/bin/env python3
"""
期权下单：单腿 或 combo（PMCC / spread）。readonly=False。

安全设计：
  - 默认 --dry-run（只 qualify + 打印，不真下单）；加 --execute 才真正下单
  - 下单前 assert 每条腿合约（symbol/right/strike/expiry）匹配
  - 默认 DAY 限价单（当日有效，没成交收盘取消、不自动重挂；不发市价单避免点差）
  - 用户偏好：默认允许盘前/盘后成交 outsideRth=True，并开启 IBKR Price Management Algo
  - 下单后查 openTrades / orderStatus 确认

单腿用法:
  # 卖 CSP（dry-run 先看）
  python option_order.py --symbol AMD --expiry 20260618 --strike 200 --right P \
      --action SELL --qty 1 --limit 8.20
  # 确认无误后真下
  python option_order.py --symbol AMD --expiry 20260618 --strike 200 --right P \
      --action SELL --qty 1 --limit 8.20 --execute

  # 买 call
  python option_order.py --symbol PLTR --expiry 20260717 --strike 20 --right C \
      --action BUY --qty 2 --limit 1.85 --execute

combo 用法（PMCC：买远期 deep-ITM call + 卖近期 OTM call）:
  python option_order.py --symbol TSLA --combo \
      --leg BUY:20280121:350:C:1 \
      --leg SELL:20260717:460:C:1 \
      --net-action BUY --net-limit 138.00 --execute

  net-limit 是 combo 净 debit（BUY combo 为正=净付出）。leg 格式: ACTION:EXPIRY:STRIKE:RIGHT:RATIO

港股期权（如腾讯 700，期权代码 TCH；港股期权无盘前盘后，关 outsideRth）:
  python option_order.py --symbol 700 --exchange SEHK --currency HKD --trading-class TCH \
      --expiry 20261127 --strike 450 --right P --action SELL --qty 1 --limit 43.00 --no-outside-rth

combo 方向语义（IB 官方约定，2026-08-03 实盘踩坑）:
  - BUY combo = 按 leg 定义的方向执行；SELL combo = 所有腿反向执行
  - 限价符号：BUY 收 credit 用负限价（-8.10 = 至少收 $8.10）；BUY 付 debit 用正限价
  - 因此 credit roll（买回近腿+卖出远腿）必须 --net-action BUY --net-limit -X
  - 用 SELL + 正限价挂 credit 包 = 反向腿 + 不可能成交的价格，永远 Submitted 不动
"""
import asyncio, argparse, sys
from _ib import IB, Option, Contract, ComboLeg, LimitOrder

from _client_ids import client_id_for


async def qualify_leg(ib, symbol, exp, strike, right, exchange='SMART', currency='USD', trading_class=''):
    c = Option(symbol, exp, float(strike), right, exchange, currency=currency, tradingClass=trading_class)
    # 硬超时：Gateway secdef 农场断连时 qualify 会无限挂死（2026-06-11 教训），
    # 静默僵死比报错危险得多——20s 不回就报错退出。
    try:
        await asyncio.wait_for(ib.qualifyContractsAsync(c), timeout=20)
    except asyncio.TimeoutError:
        print(f'ERROR: qualify timeout {symbol} {exp} {strike}{right} — Gateway secdef 可能断连，'
              f'检查 GUI 日志 "farms are connected"；已知 conId 可绕过 qualify 直下。', file=sys.stderr)
        sys.exit(3)
    if not c.conId:
        print(f'ERROR: cannot qualify {symbol} {exp} {strike}{right}', file=sys.stderr); sys.exit(1)
    # safety assert
    assert c.symbol == symbol and c.right == right and float(c.strike) == float(strike) \
        and c.lastTradeDateOrContractMonth.startswith(str(exp)), 'CONTRACT MISMATCH'
    return c


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--symbol', required=True)
    ap.add_argument('--execute', action='store_true', help='真正下单（否则 dry-run）')
    ap.add_argument('--tif', default='DAY', help='期权默认 DAY 当日有效；需挂久用 --tif GTC')
    ap.set_defaults(outside_rth=True, price_mgmt=True)
    ap.add_argument('--outside-rth', dest='outside_rth', action='store_true',
                    help='允许盘前/盘后成交（默认开；显式保留便于读命令）')
    ap.add_argument('--no-outside-rth', dest='outside_rth', action='store_false',
                    help='禁止盘前/盘后成交，只在常规交易时段成交')
    ap.add_argument('--price-mgmt', dest='price_mgmt', action='store_true',
                    help='启用 IBKR 价格管理算法（默认开；显式保留便于读命令）')
    ap.add_argument('--no-price-mgmt', dest='price_mgmt', action='store_false',
                    help='关闭 IBKR 价格管理算法')
    ap.add_argument('--client-id', type=int, default=client_id_for("option_order"))
    ap.add_argument('--exchange', default='SMART', help='港股期权用 SEHK')
    ap.add_argument('--currency', default='USD', help='港股期权用 HKD')
    ap.add_argument('--trading-class', default='', help='港股期权代码，如腾讯 TCH')
    # single-leg
    ap.add_argument('--expiry'); ap.add_argument('--strike', type=float)
    ap.add_argument('--right', choices=['C', 'P']); ap.add_argument('--action', choices=['BUY', 'SELL'])
    ap.add_argument('--qty', type=int, default=1); ap.add_argument('--limit', type=float)
    # combo
    ap.add_argument('--combo', action='store_true')
    ap.add_argument('--leg', action='append', default=[], help='ACTION:EXPIRY:STRIKE:RIGHT:RATIO')
    ap.add_argument('--net-action', choices=['BUY', 'SELL'], default='BUY')
    ap.add_argument('--net-limit', type=float)
    args = ap.parse_args()

    mode = 'DRY-RUN' if not args.execute else 'EXECUTE'
    ib = IB()
    try:
        await ib.connectAsync('127.0.0.1', 4001, clientId=args.client_id,
                              readonly=not args.execute, timeout=25)
    except Exception as e:
        print(f'ERROR: cannot connect IB Gateway ({e}). Run: launchctl kickstart -k gui/$(id -u)/com.user.ibc-gateway', file=sys.stderr); sys.exit(1)

    if args.combo:
        if not args.leg or args.net_limit is None:
            print('ERROR: combo needs --leg ... and --net-limit', file=sys.stderr); sys.exit(1)
        legs = []
        for spec in args.leg:
            act, exp, k, r, ratio = spec.split(':')
            c = await qualify_leg(ib, args.symbol, exp, k, r, args.exchange, args.currency, args.trading_class)
            legs.append((act, int(ratio), c))
            print(f'  leg {act} {ratio}x {c.symbol} {c.lastTradeDateOrContractMonth} {c.strike}{c.right} conId={c.conId}')
        bag = Contract(symbol=args.symbol, secType='BAG', exchange=args.exchange, currency=args.currency)
        bag.comboLegs = [ComboLeg(conId=c.conId, ratio=ratio, action=act, exchange=args.exchange)
                         for act, ratio, c in legs]
        if args.net_action == 'SELL':
            print('WARN: SELL combo 会把所有腿反向执行（IB 语义）。credit roll 应当用 '
                  '--net-action BUY --net-limit -X（负=收 credit）。确认你真的要反向腿再继续。',
                  file=sys.stderr)
        order = LimitOrder(args.net_action, 1, args.net_limit, tif=args.tif)
        order.outsideRth = args.outside_rth
        order.usePriceMgmtAlgo = args.price_mgmt
        print(f'[{mode}] COMBO {args.net_action} 1 {args.symbol} net limit {args.net_limit} {args.tif} '
              f'outsideRth={order.outsideRth} priceMgmt={order.usePriceMgmtAlgo}')
        if not args.execute:
            print('  (dry-run, not placed; add --execute to place)'); ib.disconnect(); return
        tr = ib.placeOrder(bag, order); await asyncio.sleep(4)
        print(f'  OrderId={tr.order.orderId} permId={tr.order.permId} Status={tr.orderStatus.status} filled={tr.orderStatus.filled}')
    else:
        for r in ['expiry', 'strike', 'right', 'action', 'limit']:
            if getattr(args, r) is None:
                print(f'ERROR: single-leg needs --{r}', file=sys.stderr); sys.exit(1)
        c = await qualify_leg(ib, args.symbol, args.expiry, args.strike, args.right,
                            args.exchange, args.currency, args.trading_class)
        print(f'  {c.symbol} {c.lastTradeDateOrContractMonth} {c.strike}{c.right} conId={c.conId}')
        order = LimitOrder(args.action, args.qty, args.limit, tif=args.tif)
        order.outsideRth = args.outside_rth
        order.usePriceMgmtAlgo = args.price_mgmt
        print(f'[{mode}] {args.action} {args.qty} {args.symbol} {args.expiry} {args.strike}{args.right} @ LMT {args.limit} {args.tif} '
              f'outsideRth={order.outsideRth} priceMgmt={order.usePriceMgmtAlgo}')
        if not args.execute:
            print('  (dry-run, not placed; add --execute to place)'); ib.disconnect(); return
        tr = ib.placeOrder(c, order); await asyncio.sleep(4)
        print(f'  OrderId={tr.order.orderId} permId={tr.order.permId} Status={tr.orderStatus.status} filled={tr.orderStatus.filled}')

    await asyncio.sleep(1)
    for t in ib.openTrades():
        if t.contract.symbol == args.symbol:
            sec = t.contract.secType
            print(f'  OPEN {sec} id={t.order.orderId} {t.order.action} @ {t.order.lmtPrice} -> {t.orderStatus.status}')
    ib.disconnect()


if __name__ == '__main__':
    asyncio.run(main())
