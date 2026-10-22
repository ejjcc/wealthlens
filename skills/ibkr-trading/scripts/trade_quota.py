#!/usr/bin/env python3
"""
正股 BUY「周交易窗」台账。对抗买入过急 / 山顶堆仓（2026-06-11 用户自查后确立，
2026-08-18 明确收缩为只限制 BUY）。

规则：每 ISO 周（按美东日期）只允许 **1 个交易日** 主动买入正股——首笔非豁免
BUY 真单落地的那天自动成为本周买入日，当日内不限 BUY 笔数；其余天禁止非豁免
BUY。SELL 永远允许且不打开、不消耗买入窗；期权与显式豁免的策略 BUY 不受限，
后者不写入本台账。stock_order.py 在 --execute BUY 时强制检查；券商接受后按 permId
upsert，成交回读更新同一行，新拒单不记账。窗外 BUY 默认阻止；显式传入
--quota-override <具体理由> 可作为人工例外放行，override 仍记账并参与定窗。

用法:
  python3 trade_quota.py status                      # 本周 BUY 窗状态 + 最近记录
  python3 trade_quota.py check --action BUY          # exit 0=可买, 2=BUY 窗已关
  python3 trade_quota.py check --action SELL         # 永远 exit 0；SELL 不占窗
  python3 trade_quota.py log --symbol AVGO --action BUY --note "stage-2 主力批"
"""
import os, sys, json, argparse, datetime, tempfile

WEEKLY_LIMIT = 1
REJECTED_STATUSES = {'ApiCancelled', 'Cancelled', 'Inactive', 'Rejected'}

STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     '..', '..', '..', 'logs', 'trade_quota.json')
STATE = os.path.normpath(STATE)


def et_today():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo('America/New_York')).date()
    except Exception:
        return datetime.date.today()


def iso_week(d=None):
    d = d or et_today()
    y, w, _ = d.isocalendar()
    return f'{y}-W{w:02d}'


def load():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {'limit': WEEKLY_LIMIT, 'ops': []}


def save(data):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='trade_quota.', suffix='.json',
                               dir=os.path.dirname(STATE))
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.write('\n')
        os.replace(tmp, STATE)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def week_ops(data=None, week=None):
    """Return every audit row for a week, including non-consuming SELL rows."""
    data = data or load()
    week = week or iso_week()
    return [o for o in data['ops'] if o['week'] == week]


def action_consumes_quota(action):
    """Only stock BUY consumes the weekly window; SELL is always exempt."""
    return str(action or '').upper() == 'BUY'


def operation_consumes_quota(op):
    """Read explicit flags when present; infer old ledger rows from action.

    `consumesQuota` lets a deliberately exempt BUY remain an audit row without
    opening the window. Exempt-strategy orders normally stay out of this ledger
    altogether, per repository policy.
    """
    # Enforce the policy invariant even if an old/manual row has a bad flag:
    # no SELL row may ever establish the buy window.
    if not action_consumes_quota(op.get('action')):
        return False
    if op.get('quotaExempt'):
        return False
    if 'consumesQuota' in op:
        return bool(op['consumesQuota'])
    return action_consumes_quota(op.get('action'))


def consuming_week_ops(data=None, week=None):
    """Return only the BUY rows that actually establish the weekly window."""
    return [o for o in week_ops(data=data, week=week) if operation_consumes_quota(o)]


def quota_available(action='BUY'):
    """返回 (ok, message)。仅 BUY 受窗限制；SELL 永远允许且不参与定窗。

    对 BUY，本周首笔非豁免 BUY 的 ET 日期 = 本周唯一买入日。override BUY
    同样记账并参与定窗——窗口反映真实买入手速。
    """
    action = str(action or '').upper()
    if action not in {'BUY', 'SELL'}:
        raise ValueError(f'unsupported stock action: {action!r}')
    if action == 'SELL':
        return True, '正股 SELL 不受买入周窗限制；本单不会打开或消耗买入窗。'

    ops = consuming_week_ops()
    week = iso_week()
    today = str(et_today())
    if not ops:
        return True, (f'本周({week})买入窗未开，今天可 BUY'
                      f'（当日内不限 BUY 笔数；SELL 不受限）。')
    day = ops[0]['date']
    if today == day:
        return True, (f'本周({week})买入日={day}（今天），窗口开着，'
                      f'可继续 BUY；SELL 不受限。')
    detail = '; '.join(f"{o['action']} {o['symbol']}" for o in ops)
    return False, (f'本周({week})买入日已用：{day}（{len(ops)} 笔 BUY：{detail}）。'
                   f'今天 {today} 禁止非豁免正股 BUY，SELL 不受限；'
                   f'下一买入窗=下周一(ET)。如要在窗外 BUY，'
                   f'须显式使用 --quota-override "<具体理由>"；默认仍阻止。')


def log_op(symbol, action, note='', override=False):
    consumes_quota = action_consumes_quota(action)
    data = load()
    data['ops'].append({'week': iso_week(), 'date': str(et_today()), 'symbol': symbol,
                        'action': action, 'note': note,
                        'override': consumes_quota and bool(override),
                        'consumesQuota': consumes_quota,
                        'status': 'MANUAL'})
    save(data)


def upsert_order(symbol, action, perm_id, order_id=None, status='Submitted',
                 qty=None, filled_qty=0, limit_price=None, avg_fill_price=None,
                 route=None, note='', override=False, replaces_perm_id=None):
    """Insert/update a live order by permanent IBKR id.

    A brand-new rejected order (permId=0 or terminal rejection with no fill) is
    deliberately not written. Repeated readbacks of the same permId update one
    row rather than creating duplicate quota entries.
    """
    try:
        perm_id = int(perm_id)
    except (TypeError, ValueError):
        return False
    filled_qty = float(filled_qty or 0)
    consumes_quota = action_consumes_quota(action)
    data = load()
    existing = next((o for o in data['ops']
                     if int(o.get('permId') or 0) == perm_id and perm_id > 0), None)
    if existing is None and (perm_id <= 0 or (status in REJECTED_STATUSES and filled_qty <= 0)):
        return False

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    fields = {
        'symbol': symbol,
        'action': action,
        'consumesQuota': consumes_quota,
        'permId': perm_id,
        'orderId': order_id,
        'status': status,
        'qty': qty,
        'filledQty': filled_qty,
        'limitPrice': limit_price,
        'avgFillPrice': avg_fill_price,
        'route': route,
        'note': note,
        'override': consumes_quota and bool(override),
        'updatedAt': now,
    }
    if replaces_perm_id:
        fields['replacesPermId'] = int(replaces_perm_id)
    if existing is None:
        fields.update({
            'week': iso_week(),
            'date': str(et_today()),
            'submittedAt': now,
        })
        data['ops'].append(fields)
    else:
        # Preserve original quota week/date and submission timestamp.
        existing.update({k: v for k, v in fields.items() if v is not None})
    save(data)
    return True


def reconcile_existing(order_rows, fill_rows):
    """Refresh existing permId rows from read-only broker snapshots.

    Never creates a new quota operation; this makes stock_status.py safe for
    bookkeeping while preventing unrelated historical fills from opening a
    weekly trading window.
    """
    data = load()
    orders = {int(row.get('permId') or 0): row for row in order_rows
              if int(row.get('permId') or 0) > 0}
    fills = {}
    for row in fill_rows:
        perm_id = int(row.get('permId') or 0)
        if perm_id <= 0:
            continue
        fills.setdefault(perm_id, []).append(row)
    changed = 0
    for op in data.get('ops', []):
        perm_id = int(op.get('permId') or 0)
        if perm_id <= 0 or (perm_id not in orders and perm_id not in fills):
            continue
        old = dict(op)
        order = orders.get(perm_id)
        rows = fills.get(perm_id, [])
        execution_qty = sum(float(row.get('shares') or 0) for row in rows)
        order_filled = float(order.get('filled') or 0) if order else 0
        filled_qty = max(float(op.get('filledQty') or 0), execution_qty, order_filled)
        qty = float(op.get('qty') or (order.get('quantity') if order else 0) or 0)
        if rows and execution_qty > 0:
            avg = sum(float(row.get('shares') or 0) * float(row.get('price') or 0)
                      for row in rows) / execution_qty
            op['avgFillPrice'] = avg
        if filled_qty > 0:
            op['filledQty'] = filled_qty
        if qty > 0 and filled_qty >= qty:
            op['status'] = 'Filled'
        elif filled_qty > 0:
            op['status'] = 'PartiallyFilled'
        elif order and op.get('status') != 'Filled':
            op['status'] = order.get('status') or op.get('status')
        if op != old:
            op['updatedAt'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            changed += 1
    if changed:
        save(data)
    return changed


def status_text(short=False):
    ops = consuming_week_ops()
    today = str(et_today())
    if not ops:
        line = '正股买入周窗: 未开（本周无非豁免 BUY；SELL 不占窗）'
    elif ops[0]['date'] == today:
        line = f"正股买入周窗: 今天({ops[0]['date']})开着，已 {len(ops)} 笔 BUY"
    else:
        line = (f"正股买入周窗: 已关（{ops[0]['date']} 用过 {len(ops)} 笔 BUY；"
                f"SELL/期权不受限；窗外 BUY 可由人工显式 override）")
    if short:
        return line
    out = [line, '']
    for o in load()['ops'][-8:]:
        flag = ' [override]' if o.get('override') else ''
        quota_flag = '' if operation_consumes_quota(o) else ' [不占买入窗]'
        state = f" [{o['status']}]" if o.get('status') else ''
        out.append(f"  {o['week']} {o['date']} {o['action']:<4} {o['symbol']:<6} "
                   f"{o.get('note','')}{state}{flag}{quota_flag}")
    return '\n'.join(out)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('status')
    ck = sub.add_parser('check')
    ck.add_argument('--action', choices=['BUY', 'SELL'], default='BUY')
    lg = sub.add_parser('log')
    lg.add_argument('--symbol', required=True)
    lg.add_argument('--action', required=True, choices=['BUY', 'SELL'])
    lg.add_argument('--note', default='')
    lg.add_argument('--override', action='store_true')
    args = ap.parse_args()

    if args.cmd == 'status':
        print(status_text())
    elif args.cmd == 'check':
        ok, msg = quota_available(args.action)
        print(msg)
        sys.exit(0 if ok else 2)
    elif args.cmd == 'log':
        log_op(args.symbol, args.action, args.note, args.override)
        print(status_text(short=True))


if __name__ == '__main__':
    main()
