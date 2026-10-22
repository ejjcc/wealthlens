# IBKR 美股会话、路由与安全改单

## 路由矩阵（美东时间）

| 时段 | 正常路由 | 说明 |
|---|---|---|
| 20:00–03:50 | `OVERNIGHT` | 独立隔夜目的地，只使用保护性限价单 |
| 03:50–04:00 | 无 | 夜盘与盘前之间的间隙，拒绝立即成交单 |
| 04:00–09:30 | `SMART` | 盘前，需 `outsideRth=True` |
| 09:30–16:00 | `SMART` | 常规时段 |
| 16:00–20:00 | `SMART` | 盘后，需 `outsideRth=True` |

周末和假日还需以实时盘口为准。`stock_status.py` 的时钟分类只是第一道闸；
活动路由拿不到新鲜 bid/ask 时，`stock_order.py --execute` 仍会拒绝发送。

### 必须区分的概念

- 普通 `SMART + outsideRth=True` 只扩展到盘前/盘后，**不包含** 20:00–03:50 的隔夜市场。
- `OVERNIGHT` 是明确的直接路由；不要把 `TIF=OVT/OND` 塞给 SMART 来模拟。
- IBKR 的 “Overnight + SMART” 是独立的跨时段产品语义；本地脚本未验证前不自动拼接。需要跨时段时分阶段重新查价、重新确认。

## 标准执行流

1. 先获取一个原子快照：

   ```bash
   python3 skills/ibkr-trading/scripts/stock_status.py XYZ --json
   ```

   输出当前会话、正确路由、该路由实时盘口、持仓、活动订单和当日成交。

2. 确认卡必须写明路由和目标最终持仓。人类回复“确认”后才可执行。

3. 实盘必须显式复制确认卡中的路由：

   ```bash
   python3 skills/ibkr-trading/scripts/stock_order.py \
     --symbol XYZ --action BUY --qty 100 --limit 48.25 \
     --route SMART --execute --quota-override "<具体理由>"
   ```

4. 紧急防御或平仓使用带价格上限的可成交限价，并加确定性闸：

   ```bash
   ... --limit 48.60 --route SMART --require-marketable --execute
   ```

   `--require-marketable` 只验证限价是否穿过当前买一/卖一；实际成交仍按当时最优价格，且不会突破确认上限。

5. 下单后再次运行 `stock_status.py`。以成交、持仓和活动订单三者共同确认结果，不能只看一次 `placeOrder` 返回。

## 安全改单

禁止“撤旧单后按原数量盲下新单”。旧单可能在撤单竞争窗口成交。必须用目标最终持仓：

```bash
python3 skills/ibkr-trading/scripts/stock_order.py \
  --symbol XYZ --action BUY --target-position 100 \
  --replace 69 --limit 48.60 --route SMART \
  --require-marketable --execute --quota-override "<具体理由>"
```

脚本顺序固定为：

1. 验证待撤订单的标的和方向；
2. 撤单并等待终态；
3. 重新读取持仓和剩余活动订单；
4. 按 `目标持仓 - 当前持仓` 计算剩余整数股；
5. 若目标已达到则不发新单；否则刷新盘口后下剩余数量。

任何同标的活动订单都会阻止普通新单。实盘脚本还持有进程锁，禁止并行执行。

## OVERNIGHT API 前置条件

IBC 配置只开启特定项目：

```ini
BypassOrderPrecautions=
BypassRedirectOrderWarning=yes
```

`stock_order.py` 不只检查配置文件，还要求运行中 Gateway 的 IBC 日志出现本次启动后的：

```text
'Bypass Redirect Order warning for Stock API Orders' is now/already selected
```

这样可防止“文件已改、Gateway 尚未重启”造成错误 10329。不要开启笼统的
`BypassOrderPrecautions=yes`。

## 状态和台账

- `Cancelled`、`ApiCancelled`、`Inactive` 或 `permId=0`：下单失败，返回非零，新拒单不写台账。
- `Submitted` / `PreSubmitted` 且 `permId>0`：券商已接受；脚本继续复查成交、持仓和活动订单。
- 台账按 `permId` 更新，同一订单重复检查不会重复追加；`stock_status.py` 只同步已存在的 permId 行，成交后补写实际数量与均价，不会把无关历史成交加入额度。
- 只有显式 `--allow-queued` 才能提交当前路由不活跃的排队单；紧急交易不要使用。
