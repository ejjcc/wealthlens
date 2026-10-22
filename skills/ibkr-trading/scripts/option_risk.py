#!/usr/bin/env python3
"""
IBKR 期权头寸风险检查 + 推荐操作。

每天美股开盘后跑：拉所有期权头寸 + 标的现价，规则化检测风险（短腿被穿 /
CSP 接货 / 临近到期 / 买 call theta 衰减 / PMCC 长腿健康 / 裸卖覆盖），
给 roll / 平仓 / 加仓 / 行动信号。可选推送 Telegram。

用法:
  python3 option_risk.py                 # 打印 markdown 报告
  python3 option_risk.py --notify        # 推送 Telegram（需 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID）
  python3 option_risk.py --notify --guard # cron 用：仅美股开盘后 ~30min 才跑

风险分级: 🔴 紧急(本日处理) / 🟡 注意(本周关注) / 🟢 正常

Gateway 离线时输出报警但 exit 0（不算失败）。
"""
import os, sys, math, socket, argparse, datetime, urllib.request, urllib.parse


def _clean(x):
    """把 None / nan / 负价归零（nan 在 Python 里是真值，会让 `nan or fallback` 失效）。"""
    if x is None or (isinstance(x, float) and math.isnan(x)) or x < 0:
        return 0
    return x

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _gw import _check_alive, get_gateway_config, run, connect, ensure_gateway

TG_TOKEN_KEY = 'TELEGRAM_BOT_TOKEN'
TG_CHAT_KEY = 'TELEGRAM_CHAT_ID'


# ---------- 美股开盘时段守卫（自动适配夏令时）----------
def in_market_open_window():
    """当前是否在美股开盘后 ~15-45 min（ET 9:45-10:15）。用 zoneinfo 算 ET。"""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.datetime.now(ZoneInfo('America/New_York'))
    except Exception:
        return True  # 无法判断则不拦
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return (9 * 60 + 40) <= mins <= (10 * 60 + 20)


# ---------- Telegram ----------
def notify_telegram(text):
    token, chat_id = os.getenv(TG_TOKEN_KEY), os.getenv(TG_CHAT_KEY)
    if not token or not chat_id:
        print(f'WARN: {TG_TOKEN_KEY} / {TG_CHAT_KEY} not set, skip notify', file=sys.stderr); return False
    url = f'https://api.telegram.org/bot{token}/sendMessage'
    # Telegram 单条上限 4096 字
    data = urllib.parse.urlencode({'chat_id': chat_id, 'text': text[:4096],
                                   'disable_web_page_preview': 'true'}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20)
        return True
    except Exception as e:
        print(f'WARN: telegram send failed: {e}', file=sys.stderr); return False


# ---------- 数据 ----------
EXCHANGE_BY_CURRENCY = {'USD': 'SMART', 'HKD': 'SEHK'}
CURRENCY_LABEL = {'USD': '$', 'HKD': 'HK$'}


def _cur(r):
    return CURRENCY_LABEL.get(r.get('cur', 'USD'), r.get('cur', '') + ' ')


async def _price(ib, contract, mark=0):
    """返回 (ticker, 现价)。非美元标的（港股期权）IB 没订阅实时行情，且 cron 在美股时段跑时港股已收盘，
    实时取不到就退到延迟冻结报价（type 4），再退到 IB 持仓估值 mark；美股保持原行为。"""
    [t] = await ib.reqTickersAsync(contract)
    px = _clean(t.marketPrice()) or _clean(t.close)
    if px or contract.currency == 'USD':
        return t, px
    ib.reqMarketDataType(4)
    try:
        [t] = await ib.reqTickersAsync(contract)
    finally:
        ib.reqMarketDataType(1)
    return t, _clean(t.marketPrice()) or _clean(t.close) or _clean(mark)


async def fetch():
    ib = await connect(role="option_risk")
    try:
        from _ib import Stock
        ib.reqMarketDataType(1)
        positions = ib.positions()
        opts = [p for p in positions if p.contract.secType == 'OPT']
        stks = {p.contract.symbol: p.position for p in positions if p.contract.secType == 'STK'}
        marks = {it.contract.conId: it.marketPrice for it in ib.portfolio()}
        # 标的现价（去重）
        syms = sorted({(p.contract.symbol, p.contract.currency) for p in opts})
        spots = {}
        for s, cur in syms:
            try:
                c = Stock(s, EXCHANGE_BY_CURRENCY.get(cur, 'SMART'), cur); await ib.qualifyContractsAsync(c)
                _, spots[s] = await _price(ib, c, marks.get(c.conId, 0))
            except Exception:
                spots[s] = 0
        # 期权 greeks + 现价
        rows = []
        for p in opts:
            c = p.contract
            try:
                await ib.qualifyContractsAsync(c)
                t, px = await _price(ib, c, marks.get(c.conId, 0))
                iv = t.modelGreeks.impliedVol if t.modelGreeks and t.modelGreeks.impliedVol else 0
                dl = t.modelGreeks.delta if t.modelGreeks and t.modelGreeks.delta else 0
            except Exception:
                px = iv = dl = 0
            rows.append(dict(symbol=c.symbol, cur=c.currency, exp=c.lastTradeDateOrContractMonth,
                             strike=float(c.strike), right=c.right, qty=p.position,
                             avg=p.avgCost, mult=float(c.multiplier or 100),
                             px=px, iv=iv, delta=dl,
                             spot=spots.get(c.symbol, 0),
                             shares=stks.get(c.symbol, 0)))
        return rows
    finally:
        ib.disconnect()


# ---------- 风险规则 ----------
# 短腿判断三条线（与「换仓判断框架」一致）：
#   delta ≥ 0.45  → 防御（被穿/接货威胁）→ roll，只在收 net credit 时滚
#   利润 ≥ 50%    → 收割 → 平了重卖下一轮，别贪尾部
#   DTE ≤ 21      → 时间检查点 → 平/让其过期/roll 下月，避最后一周 gamma
# 收盘后 Gateway 不返回 greeks（delta=0）→ 先用 Black-Scholes 从 spot/strike/dte/px
# 反推 delta 兜底（显示标「Δ估」）；连 px 都缺（深夜无 close）才退到 moneyness 代理（标「Δ收盘缺」）。

def _bs_delta_fallback(spot, strike, dte, right, price):
    """盘后 IBKR 无 greeks 时，用 BS 从期权市价反推 delta。失败返回 None。"""
    try:
        from bs_greeks import calculate_greeks
        res = calculate_greeks(spot=spot, strike=strike,
                               option_type='call' if right == 'C' else 'put',
                               dte=dte, market_price=price)
        return res.get('greeks', {}).get('delta')
    except Exception:
        return None


def _short_metrics(r):
    """短腿：返回 (利润占比 prof, |delta| dabs)。prof = 已吃掉的权利金比例；px 无效则 None。"""
    mult = r.get('mult') or 100
    entry = r['avg'] / mult if r['avg'] else 0          # 卖开均价/股（avgCost 是总额）
    prof = (entry - r['px']) / entry if (entry and r['px'] > 0) else None  # 短腿：成本跌得越多赚越多
    dabs = abs(r['delta']) if r['delta'] else 0.0       # 0 = 收盘缺 greeks
    return prof, dabs


def assess(r):
    """返回 (level, action). level: 3=🔴 2=🟡 1=🟢"""
    today = datetime.date.today()
    exp = datetime.datetime.strptime(r['exp'][:8], '%Y%m%d').date()
    dte = (exp - today).days
    spot, k, qty = r['spot'], r['strike'], r['qty']
    right, shares = r['right'], r['shares']
    r['dte'] = dte
    # 盘后 IBKR delta=0 → 有期权市价就用 BS 反推兜底，让 Δ≥0.45 防御线盘后也生效
    if not r.get('delta') and r['px'] > 0 and dte > 0 and spot > 0 and k > 0:
        est = _bs_delta_fallback(spot, k, dte, right, r['px'])
        if est is not None:
            r['delta'] = est
            r['delta_est'] = True
    r['dabs'] = abs(r['delta']) if r['delta'] else 0.0
    short = qty < 0

    # ---- Short Call (CC / PMCC 短腿 / 裸) ----
    if right == 'C' and short:
        covered = shares >= abs(qty) * 100
        kind = 'CC' if covered else ('PMCC短腿' if shares < abs(qty) * 100 else 'naked-C')
        prof, dabs = _short_metrics(r)
        r['prof'] = prof
        r['mny'] = (1 - spot / k) if k else 0           # OTM 垫
        if spot >= k:
            return (3, f'{kind} 已被穿 ITM(spot {spot:.0f}≥K {k:.0f}) → roll up&out 或平仓（收 net credit）')
        if dabs >= 0.45:
            return (3, f'{kind} 被穿威胁 Δ{dabs:.2f}（过 0.45）→ roll up&out，只在收 net credit 时滚')
        if not dabs and k and spot / k >= 0.95:
            return (2, f'{kind} 接近被穿 {spot/k*100:.0f}%·Δ收盘缺 → 盯紧，冲破 {k:.0f} roll up')
        if prof is not None and prof >= 0.50:
            return (2, f'{kind} 利润达标 {prof*100:.0f}%（过 50%）→ 平了重卖下一轮，别贪尾部')
        if dte <= 21:
            return (2, f'{kind} 进 21DTE（{dte}d）→ 平/让其过期/roll 下月，避最后一周 gamma')
        return (1, f'{kind} 正常收租（离 Δ0.45 / 50%利润 / 21d 都远）')

    # ---- Short Put (CSP) ----
    if right == 'P' and short:
        prof, dabs = _short_metrics(r)
        r['prof'] = prof
        cushion = (spot - k) / k if k else 0
        r['mny'] = cushion
        if spot <= k:
            return (3, f'CSP 已 ITM(spot {spot:.0f}≤K {k:.0f}) → 接 {abs(qty)*100:.0f} 股≈{_cur(r)}{k*abs(qty)*100:,.0f} 或 roll down&out')
        if dabs >= 0.45:
            return (3, f'CSP 接货威胁 Δ{dabs:.2f}（过 0.45）→ roll down&out 或准备接货')
        if not dabs and k and cushion <= 0.05:
            return (2, f'CSP 垫薄 {cushion*100:.0f}%·Δ收盘缺 → 盯紧，跌破 {k:.0f} roll/接货')
        if prof is not None and prof >= 0.50:
            return (2, f'CSP 利润达标 {prof*100:.0f}%（过 50%）→ 平了重卖下一轮')
        if dte <= 21:
            return (2, f'CSP 进 21DTE（{dte}d）→ 平/让其过期/roll 下月')
        return (1, f'CSP 正常收租（离 Δ0.45 / 50%利润 / 21d 都远）')

    # ---- Long Call (买call / PMCC 长腿) ----
    if right == 'C' and not short:
        is_leap = dte > 120
        d = r['delta']
        if is_leap:
            if d and d < 0.55:
                return (2, f'PMCC长腿偏弱 Δ{d:.2f}（<0.55 趋 ATM）→ 考虑 roll 更深 ITM / 更远期')
            if 0 < dte <= 270:
                return (1, f'PMCC长腿 {dte}d → 健康收租；DTE<270 进 theta 加速区，留意 roll 远期')
            return (1, f'PMCC长腿 {dte}d → 健康，继续 roll 短腿收租')
        otm = spot < k
        r['mny'] = (k - spot) / spot if spot else 0
        if dte <= 14:
            return (3, f'买call 临近到期 {dte}d {"OTM" if otm else "ITM"} → theta 急衰，止盈/止损 或 roll 远期')
        if dte <= 30 and otm:
            return (2, f'买call {dte}d 仍 OTM → 时间不多，盯催化兑现')
        return (1, f'买call {dte}d {"OTM" if otm else "ITM"} → 持有观察')

    # ---- Long Put (对冲) ----
    if right == 'P' and not short:
        if dte <= 14:
            return (2, f'对冲 put 临近到期 {dte}d → 尾部保险将到期，决定 roll 否')
        return (1, f'对冲 put {dte}d → 持有')

    return (1, '未分类 —')


LEVEL_EMOJI = {3: '🔴', 2: '🟡', 1: '🟢'}


def _delta_str(r):
    d = r.get('dabs') or 0
    if not d:
        return 'Δ—'
    return f'Δ估{d:.2f}' if r.get('delta_est') else f'Δ{d:.2f}'


def _prof_str(r):
    p = r.get('prof')
    return f'利润{p*100:.0f}%' if p is not None else '利润—'


def _metrics_line(r):
    """每行第二行：把三条判断线的输入值摆出来，让人能对着框架自查。"""
    right, dte = r['right'], r['dte']
    short = r['qty'] < 0
    if short and right == 'C':
        return f"{_delta_str(r)} · {_prof_str(r)} · OTM{r.get('mny',0)*100:.0f}% · {dte}d"
    if short and right == 'P':
        return f"{_delta_str(r)} · {_prof_str(r)} · 垫{r.get('mny',0)*100:.0f}% · {dte}d"
    if not short and right == 'C':
        if dte > 120:
            return f"{_delta_str(r)} · {dte}d"
        return f"{'OTM' if r.get('mny',0) > 0 else 'ITM'} · {dte}d"
    return f"{dte}d"


def build_report(rows):
    for r in rows:
        r['level'], r['action'] = assess(r)
    rows.sort(key=lambda x: (-x['level'], x['dte']))
    n_crit = sum(1 for r in rows if r['level'] == 3)
    n_warn = sum(1 for r in rows if r['level'] == 2)
    n_ok = len(rows) - n_crit - n_warn
    head_emoji = '🔴' if n_crit else ('🟡' if n_warn else '🟢')
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')

    try:
        import trade_quota
        quota_line = trade_quota.status_text(short=True)
    except Exception:
        quota_line = None
    lines = [f'{head_emoji} IBKR 期权风险检查 — {now}',
             f'共 {len(rows)} 头寸 | 🔴{n_crit} 紧急 · 🟡{n_warn} 该动 · 🟢{n_ok} 正常',
             '判断线: Δ≥0.45 防御 / 利润≥50% 收割 / ≤21DTE 检查点']
    if quota_line:
        lines.append(quota_line)
    lines.append('')
    for r in rows:
        e = LEVEL_EMOJI[r['level']]
        side = '空' if r['qty'] < 0 else '多'
        exp = r['exp'][:8]
        lines.append(f"{e} {r['symbol']} {exp[4:6]}/{exp[6:8]} {r['strike']:.0f}{r['right']} "
                     f"×{r['qty']:+.0f}({side}) · 现{_cur(r)}{r['spot']:.0f}")
        lines.append(f"    {_metrics_line(r)}")
        lines.append(f"    👉 {r['action']}")
    return '\n'.join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--notify', action='store_true', help='推送 Telegram')
    ap.add_argument('--guard', action='store_true', help='仅美股开盘后 ~30min 才跑（cron 用）')
    args = ap.parse_args()

    if args.guard and not in_market_open_window():
        sys.exit(0)  # 非开盘窗口，静默退出

    gw = ensure_gateway()
    if gw != 'up':
        if gw == 'timeout':
            detail = ('已自动触发 Gateway 启动，等待 5 分钟未完成登录。\n'
                      '请确认手机 IBKR 2FA 推送；下一轮 cron 会自动重试。')
        else:
            detail = ('Gateway 离线，自动启动已静默（夜间时段 / 40 分钟内已尝试 / 连续 2 次未确认已退避）。\n'
                      '需要时对 Claude 说「启动」或手动: launchctl kickstart -k gui/$(id -u)/com.user.ibc-gateway')
        msg = (f'🔴 IBKR 期权风险检查 — {datetime.datetime.now():%Y-%m-%d %H:%M}\n\n{detail}')
        print(msg)
        if args.notify:
            notify_telegram(msg)
        return

    try:
        rows = run(fetch())
    except Exception as e:
        msg = f'🔴 期权风险检查 — 连接异常: {e}'
        print(msg)
        if args.notify:
            notify_telegram(msg)
        return

    if not rows:
        msg = f'🟢 IBKR 期权风险检查 — {datetime.datetime.now():%Y-%m-%d %H:%M}\n\n当前无期权头寸。'
        print(msg)
        if args.notify:
            notify_telegram(msg)
        return

    report = build_report(rows)
    print(report)
    if args.notify:
        notify_telegram(report)


if __name__ == '__main__':
    main()
