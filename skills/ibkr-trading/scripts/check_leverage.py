#!/usr/bin/env python3
"""
IBKR 杠杆健康周检查

每周自动运行，输出 markdown 报告判断杠杆与流动性是否满足账户级闸门。
- 常态目标: Gross/NetLiq <= 1.20x
- 可接受操作上限: <= 1.30x；本单后不得突破
- 停止新增: > 1.30x，减风险回到 1.20x 附近
- 压力测试线: 2.00x，仅用于极端情景，不是部署目标
- Base cash 只作融资使用信息；流动性闸门看 AvailableFunds / ExcessLiquidity

Gateway 离线时输出报警信息但不算失败 (exit 0).
"""
import os
import socket
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _gw import _check_alive, get_gateway_config, run, connect

# 风险闸门参数：按自己的风险政策调整
TARGET = 1.20
OPERATING_LIMIT = 1.30
STRESS_CEILING = 2.00

# 流动性警戒；Base TotalCashValue 不再作为固定买入门槛。
AVAILABLE_WARNING = 20_000.0
AVAILABLE_CRITICAL = 5_000.0
EXCESS_WARNING = 0.30
EXCESS_CRITICAL = 0.20


def report_gateway_offline():
    print(f"""# 🔴 IBKR 杠杆周检查 — {datetime.now().strftime('%Y-%m-%d %H:%M')}

**Gateway 离线** — 无法获取实时数据.

可能原因:
1. IB Gateway 未启动
2. 2FA 超时被踢出
3. 网络问题

手动恢复（由 launchd 托管进程组；绝不直接调 ibcstart.sh）:
```bash
launchctl kickstart -k gui/$(id -u)/com.user.ibc-gateway
```

启动后用手机 IBKR App 完成 2FA, 然后重跑:
```bash
python3 skills/ibkr-trading/scripts/check_leverage.py
```
""")


async def fetch():
    ib = await connect(role="check_leverage")
    try:
        summary = await ib.accountSummaryAsync()
        # AccountSummary may return both USD and BASE rows for the same tag.
        # v6.1 liquidity/leverage gates are account-wide, so BASE must win regardless
        # of IB's row order; otherwise USD cash can mask JPY/HKD borrowing.
        out = {}
        for row in summary:
            if row.currency not in ("USD", "BASE"):
                continue
            if row.tag not in out or row.currency == "BASE":
                out[row.tag] = row.value
        return out
    finally:
        ib.disconnect()


def main():
    host, port, _ = get_gateway_config()
    if not _check_alive(host, port):
        report_gateway_offline()
        return

    try:
        data = run(fetch())
    except Exception as e:
        print(f"# 🔴 IBKR 杠杆检查 — 连接异常\n\n错误: {e}\n")
        return

    try:
        net_liq = float(data.get('NetLiquidation', 0))
        gross = float(data.get('GrossPositionValue', 0))
        cash = float(data.get('TotalCashValue', 0))
        avail = float(data.get('AvailableFunds', 0))
        excess_liq = float(data.get('ExcessLiquidity', 0))
        init_margin = float(data.get('InitMarginReq', 0))
    except (TypeError, ValueError) as e:
        print(f"# 🔴 数据解析失败: {e}\n")
        return

    margin_debt = max(0.0, -cash)
    leverage = gross / net_liq if net_liq > 0 else 0
    debt_ratio = margin_debt / net_liq if net_liq > 0 else 0
    excess_ratio = excess_liq / net_liq if net_liq > 0 else 0
    cash_display = f"-${abs(cash):,.0f}" if cash < 0 else f"${cash:,.0f}"

    liquidity_critical = avail < AVAILABLE_CRITICAL or excess_ratio < EXCESS_CRITICAL
    liquidity_warning = avail < AVAILABLE_WARNING or excess_ratio < EXCESS_WARNING

    if leverage >= STRESS_CEILING:
        status = "🔴 触及 2.00× 压力测试线"
        delta = max(0.0, gross - net_liq * TARGET)
        action = (
            f"**立即停止新增并减风险 ~${delta:,.0f}**；先降到 1.30× 以下，再回到 1.20× 常态目标。"
            "2.00× 只用于极端压力测试，不是可部署目标"
        )
        emoji = "🔴"
    elif leverage > OPERATING_LIMIT:
        status = "🔴 超过 1.30× 操作上限"
        delta = max(0.0, gross - net_liq * TARGET)
        action = (
            f"**冻结新增并减风险 ~${delta:,.0f}**，目标压回 1.20×；"
            "若存在 5DTE 内的配对多腿，先核 post-settlement Gross，避免把即将消失的对冲腿绝对值误判成永久敞口"
        )
        emoji = "🔴"
    elif liquidity_critical:
        status = "🔴 流动性红线"
        action = (
            f"**停止新增并优先降低融资/交割义务**：AvailableFunds ${avail:,.0f}，"
            f"ExcessLiq / NetLiq {excess_ratio:.1%}；红线为 $5k / 20%"
        )
        emoji = "🔴"
    elif liquidity_warning:
        status = "🟠 流动性警戒"
        action = (
            f"**暂停新增风险，先恢复流动性**：AvailableFunds ${avail:,.0f}，"
            f"ExcessLiq / NetLiq {excess_ratio:.1%}；解除警戒需至少 $20k / 30%"
        )
        emoji = "🟠"
    elif leverage > TARGET:
        status = "🟡 1.20–1.30× 操作带"
        capacity = max(0.0, net_liq * OPERATING_LIMIT - gross)
        action = (
            f"**可持有并谨慎评估新增/置换**；本单后必须 <=1.30×，当前距上限约 ${capacity:,.0f} Gross，"
            "并继续通过 bucket、交易窗、事件和流动性检查"
        )
        emoji = "🟡"
    else:
        status = "🟢 常态目标内"
        target_room = max(0.0, net_liq * TARGET - gross)
        limit_room = max(0.0, net_liq * OPERATING_LIMIT - gross)
        action = (
            f"**可评估部署**（不是自动买入）；距 1.20× 常态目标约 ${target_room:,.0f} Gross，"
            f"距 1.30× 操作上限约 ${limit_room:,.0f}，仍须过 bucket、交易窗和事件检查"
        )
        emoji = "🟢"

    if leverage > OPERATING_LIMIT:
        leverage_ok = "🔴"
    elif leverage > TARGET:
        leverage_ok = "🟡"
    else:
        leverage_ok = "✅"
    avail_ok = "🔴" if avail < AVAILABLE_CRITICAL else ("⚠️" if avail < AVAILABLE_WARNING else "✅")
    excess_ok = "🔴" if excess_ratio < EXCESS_CRITICAL else ("⚠️" if excess_ratio < EXCESS_WARNING else "✅")
    debt_ok = "ℹ️" if debt_ratio <= 0.20 else "⚠️"

    print(f"""# {emoji} IBKR 杠杆周检查 — {datetime.now().strftime('%Y-%m-%d %H:%M')}

## 状态: {status}

### 账户快照

| 指标 | 数值 |
|---|---:|
| NetLiquidation | ${net_liq:,.0f} |
| GrossPositionValue | ${gross:,.0f} |
| Base TotalCashValue | {cash_display} |
| Margin Debt | ${margin_debt:,.0f} |
| AvailableFunds | ${avail:,.0f} |
| ExcessLiquidity | ${excess_liq:,.0f} |
| InitMarginReq | ${init_margin:,.0f} |

### 杠杆 / 风险指标

| 指标 | 当前 | 目标 | 状态 |
|---|---:|---|---:|
| **Gross / NetLiq** | **{leverage:.3f}×** | <=1.20 常态 / <=1.30 上限 / 2.00 压力线 | {leverage_ok} |
| **AvailableFunds** | **${avail:,.0f}** | >=$20k；<$5k 红线 | {avail_ok} |
| **ExcessLiq / NetLiq** | **{excess_ratio:.1%}** | >=30%；<20% 红线 | {excess_ok} |
| Base cash | {cash_display} | 信息项，无固定 floor | ℹ️ |
| Margin Debt / NetLiq | {debt_ratio:.1%} | 监测融资成本，不单独拦截 | {debt_ok} |

### 行动建议

{action}

""")


if __name__ == '__main__':
    main()
