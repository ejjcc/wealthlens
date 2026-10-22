# IBKR 期权策略 Playbook

> 期权机会分析与策略选择的决策框架。2026-06-02 从一次完整的「三策略分析 + 下单」会话沉淀。
> 配套脚本：`scripts/option_chain.py`（拉链/扫机会）、`scripts/option_order.py`（单腿+combo 下单）。

## 第一原则：IV 环境决定策略

拉完期权链先看 ATM IV，再选策略——这是最重要的路由判断：

| ATM IV | 适用策略 | 逻辑 |
|---|---|---|
| **高（>60%）** | **卖方（CC / CSP）** | 权利金奇贵，卖方天堂；买方被 IV 吃掉赔率 |
| **中（35-60%）** | 卖方为主，买 call 仅在高 beta + 有催化时 | 中性区，看标的弹性和催化 |
| **低（<35%）** | **PMCC / 买正股 / 买 call** | 长腿便宜（extrinsic 小），买方占便宜 |

**反直觉但关键**：你越看好、越多人看好的票，IV 越高，买 call 越不划算。最强 thesis ≠ 最佳买 call（例：thesis 最强的标的 IV 85%，买 call breakeven +20%，改用卖 CSP）。

## 三策略速查

### 收租（卖方）
- **CC**：持股 ≥100 股，卖 OTM call（delta 0.25-0.35，年化高、被行权概率可控）。已有 short call 的标的额度已用，别裸卖。
- **CSP**：愿低位接货 + 高 IV。被行权 = 低位加仓，所以接货价要在「愿意加仓的位置」。注意接货 100 股的金额可能远超原计划仓位（例：接货 $20k vs 计划 $4k，要提示）。

### 赔率（买 call）
- 只在 **IV 相对低（<80%）+ 高 beta + 有催化** 时做。
- 高 IV 标的（>100%）裸买 OTM call 赔率被吃，改用 **bull call spread**（卖 OTM 腿对冲 IV）或 **deep-ITM call 当杠杆**（extrinsic 小，受 IV 影响小）。
- 赔率倍数 = `max(0, spot×(1+涨幅) − strike) / ask`。例：同批候选中 IV 最低（76%）且高 beta 的标的，涨 50%→4.7×，是唯一占便宜的买 call。

### PMCC（买远期 deep-ITM call 替代正股 + 卖近期 OTM call）
见下方专题。

## PMCC 专题

### 长腿选择三要素
1. **低 IV**：长腿 extrinsic（时间价值）越小越好。META/AAPL（IV 30%）extrinsic ~$11；TSLA（IV 50%）~$30；NBIS（IV 108%）~$44。**IV 高的标的不适合 PMCC**。
2. **deep-ITM（delta 0.75-0.85）**：最接近持股、被穿风险低、theta 损耗慢。delta 0.5（ATM）的长腿杠杆太高、易被穿、不是好长腿。
3. **远期 LEAP（12 月+）**：跑道长，减少换长腿频率（换长腿要付新 extrinsic）。

### 回本时间两种口径（结论可能相反）
- **口径① extrinsic 回本** = `长腿 extrinsic / 月租`。衡量多久把买长腿多付的时间价值赚回，之后长腿≈打折持股。**低 IV 标的赢**（extrinsic 小）。
- **口径② 全成本 cover** = `寿命内总租金 / 长腿成本` = `月租 × 长腿剩余月数 / 长腿成本`。衡量靠租金多久收回整个长腿本金。**高 IV 标的赢**（短腿月租厚）+ **远期长腿赢**（跑道长）。

示例：AAPL extrinsic 回本 2.3 月（赢）但全成本 cover 仅 54%；TSLA extrinsic 回本 2.7 月但全成本 cover 96%（赢）。**远期 LEAP 放大口径②**：TSLA 2028-01（19.7月）cover 179%，2027-01（7.5月）仅 96%。

### 已有正股则不做 PMCC
PMCC 的意义是「用 long call 替代正股」。已持正股的标的直接卖 covered call，开 PMCC 是重复花 extrinsic（例：已持 100+ 股且已卖 covered call，再开 PMCC 多此一举）。

## 合并敞口测算（多账户同标的）

同一标的在多账户/多策略并存时，必须加总 net delta 看真实敞口，避免无意识加倍：

```
合并 net delta = Σ(每个 PMCC: 长腿 delta − 短腿 delta) + 正股股数
```

示例：券商 A 的旧 TSLA PMCC（net 0.35）+ 券商 B 的新 PMCC（net 0.44）+ 8 股正股 = ≈87 股 TSLA 等效。**在已有成功 PMCC 的标的再开第二个 = 押注翻倍**，分散角度应换标的（缺的不是「第二个 TSLA PMCC」而是「一个非 TSLA 的 PMCC」）。

## 接力 PMCC（旧长腿 ATM/将到期时）

旧 PMCC 的长腿若已变 ATM（delta 掉到 ~0.5）或临近到期，用更健康的远期 deep-ITM 长腿「接力」：
- 不是加倍——计划旧长腿收租收尾、自然了结，过渡期双持但有退出纪律
- 新长腿选远期 deep-ITM（delta 0.77+），替代已 ATM 的旧腿

## 盘后数据坑

- 盘后/盘前远档 strike 常无 bid/ask（返回 0）。挂单一律用限价，开盘成交。
- strike 必须从 `chain.strikes`（reqSecDefOptParams）取真实档，不要自己造（高价股周权细网格 vs 月权粗网格，造 strike 会 "No security definition"）。
- 月度到期：日 15-21 之间（第三个周五附近），strike 最全。

## 下单纪律

1. 下单前确认 strike + 张数（大额 / 接货风险敞口大）
2. 卖 CSP 前提示被行权接货金额 vs 计划目标仓位的倍数
3. combo 用一张 combo 单（避免单腿成交后另一腿空着）
4. 下单脚本必 assert 合约（symbol/right/strike/expiry），readonly=False
5. **期权挂单用 DAY（当日有效），不用 GTC**。期权波动大，DAY 单没成交就让它收盘取消，**后续是否重挂由人工看盘决定，不机械自动重挂旧价位**（别建「每日自动重挂意图单」机制）。改单先确认旧单取消完成再挂新单——跨 client session 的 GTC 单本 client 取消会报 Error 10147、盘后只能 PendingCancel，否则会一度出现重复单。
