#!/usr/bin/env python3
"""
标的价格阈值「一次性」盘中提醒。

盘中 cron 反复跑：拉标的现价，越过阈值就推 Telegram，并把该提醒标记为已触发
（写 logs/price_alert.json），之后同一提醒不再重复推。RTH 之外静默退出。

用法:
  python3 price_alert.py --symbol AAPL --above 300 --note "收复 $300 → 可卖 PMCC 短腿"
  python3 price_alert.py --symbol XYZ  --below 50  --note "跌破支撑"

触发后想再次启用：把 logs/price_alert.json 里对应的 key 删掉即可。
Gateway 离线 / 非 RTH：静默 exit 0（等下一次 cron）。
"""
import os, sys, json, argparse, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _client_ids import ProcessSlotBusy, process_slot
from _gw import _check_alive, get_gateway_config, run, connect, ensure_gateway
from option_risk import notify_telegram        # 复用 Telegram 推送

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
STATE = os.path.join(REPO, 'logs', 'price_alert.json')


def in_rth():
    """当前是否在美股常规时段（ET 9:30-16:00，周一到周五）。"""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.datetime.now(ZoneInfo('America/New_York'))
    except Exception:
        return True
    if now.weekday() >= 5:
        return False
    m = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= m <= (16 * 60)


def load_fired():
    try:
        return set(json.load(open(STATE)))
    except Exception:
        return set()


def save_fired(fired):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(sorted(fired), open(STATE, 'w'), ensure_ascii=False, indent=0)


async def get_spot(sym):
    ib = await connect(role="price_alert")
    try:
        from _ib import Stock
        ib.reqMarketDataType(1)
        c = Stock(sym, 'SMART', 'USD')
        await ib.qualifyContractsAsync(c)
        [t] = await ib.reqTickersAsync(c)
        px = t.marketPrice()
        if px is None or (isinstance(px, float) and px != px) or px <= 0:
            px = t.close or 0
        return px
    finally:
        ib.disconnect()


def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--symbol', required=True)
    ap.add_argument('--above', type=float)
    ap.add_argument('--below', type=float)
    ap.add_argument('--note', default='')
    args = ap.parse_args()
    if (args.above is None) == (args.below is None):
        print('ERROR: 必须且只能给一个 --above 或 --below', file=sys.stderr); sys.exit(1)

    if not in_rth():
        sys.exit(0)

    gw = ensure_gateway()
    if gw != 'up':
        if gw == 'timeout':
            # 真正发起过启动尝试（已推 2FA）才通知；suppressed 一律静默等下次
            notify_telegram('⚠️ price_alert: Gateway 离线，已自动触发启动，'
                            '请确认手机 IBKR 2FA；未确认前价格监控暂停。')
        sys.exit(0)

    direction = 'above' if args.above is not None else 'below'
    thr = args.above if args.above is not None else args.below
    key = f'{args.symbol}:{direction}:{thr:g}'
    fired = load_fired()
    if key in fired:
        sys.exit(0)   # 已触发过，不重复

    try:
        spot = run(get_spot(args.symbol))
    except Exception:
        sys.exit(0)
    if not spot:
        sys.exit(0)

    crossed = (direction == 'above' and spot >= thr) or (direction == 'below' and spot <= thr)
    if not crossed:
        sys.exit(0)

    sign = '≥' if direction == 'above' else '≤'
    msg = (f'🔔 价格提醒 · {args.symbol} ${spot:.2f} {sign} ${thr:g}\n'
           f'{args.note}' if args.note else
           f'🔔 价格提醒 · {args.symbol} ${spot:.2f} {sign} ${thr:g}')
    print(msg)
    notify_telegram(msg)
    fired.add(key)
    save_fired(fired)


def main():
    """Serialize the alert family: all cron rows share client 77 and one state file."""
    try:
        with process_slot("job-price-alert-family", wait_seconds=600):
            return _main()
    except ProcessSlotBusy:
        # A stuck predecessor should not create another overlapping cron tree.
        return None


if __name__ == '__main__':
    main()
