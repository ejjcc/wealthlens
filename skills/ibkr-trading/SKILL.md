---
name: ibkr-trading
description: IBKR trading toolkit for AI agents — live (IB Gateway) and historical (Flex) queries, session-aware stock orders, single-leg and combo option orders, daily option risk check, one-shot price alerts, and a weekly stock-buy window ledger. All order scripts are dry-run by default.
metadata:
  version: 1.0.1
---

# IBKR Trading Skill

Companion to the read-only WealthLens dashboard. Where WealthLens only reads balances, this skill **can place live orders** on Interactive Brokers through a local IB Gateway. Treat it accordingly: every order script is dry-run unless `--execute` is passed, and agents must follow the confirmation protocol below.

## Safety model

1. **Dry-run by default.** `stock_order.py` / `option_order.py` only validate and print unless `--execute` is given.
2. **Human confirmation before any live order.** The agent posts a confirmation card and waits for an explicit "confirm" reply. Dry-runs, quotes and queries need no card.
3. **Serialized execution.** Live stock orders hold a process lock; an active order on the same symbol blocks a plain new order (use `--replace` + `--target-position`).
4. **Static client IDs.** `scripts/_client_ids.py` assigns one fixed IB API client ID per script plus a local `flock`, so cron jobs and manual runs never collide (Error 326). Order owners are fixed: `option_order=90`, `stock_order=91`.
5. **Weekly stock-buy window** (`trade_quota.py`): per ISO week (US/Eastern dates) only one trading day may contain discretionary stock BUYs — the day of the first non-exempt BUY becomes that week's buy day, with unlimited BUYs on that day. SELLs are never restricted and never open the window; options are not restricted. `--quota-override "<reason>"` exists for exceptions, but only when the human explicitly asks for it; overrides are still logged. The ledger lives at `logs/trade_quota.json` (git-ignored).

### Confirmation card

```
LIVE ORDER · <BUY/SELL> <symbol> <option: expiry + strike + P/C; blank for stock>
├ <qty> @ <LMT $price / MKT> · <DAY/GTC> · IBKR <route: SMART/OVERNIGHT>
├ Cash flow   <cost ~$X / premium ±$X | assignment price $Y (-Z%) | margin ~$W>
├ Exposure    <current position → after this order (vs your cap/target)>
├ Rationale   <plan / thesis> · leverage <current×, impact of this order>
├ Buy window  <stock BUY: not opened / open today / closed (MM-DD) + override reason if any;
│              stock SELL: "not restricted"; options: "not restricted">
└ Reply "confirm" to execute / "change …" to adjust
```

Before a stock card, run `stock_status.py` once and build the card from that single snapshot (session, route, live quote on that route, position, active orders, today's fills). The executed `stock_order.py --route ...` must match the card; if the quote has moved through the confirmed limit, stop and re-issue the card.

## Prerequisites

### IB Gateway (live tools)

- IB Gateway reachable at `127.0.0.1:4001` (override with `IBKR_HOST` / `IBKR_PORT`), started through [IBC](https://github.com/IbcAlpha/IBC).
- Do not use the dashboard's `ReadOnlyApi=yes` / `gatewaystartmacos.sh -inline &` setup from the main README for this skill: orders need `ReadOnlyApi=no`, and the scripts assume Gateway is owned by a launchd job labelled `com.user.ibc-gateway` and start it with `launchctl kickstart -k gui/$(id -u)/com.user.ibc-gateway`. Never spawn `ibcstart.sh` from a short-lived shell: the session may be reaped during 2FA, and a second instance on the same login kicks the first one off.
- `_gw.ensure_gateway()` lets cron consumers (`option_risk.py`, `price_alert.py`) start Gateway on demand with three anti-2FA-spam guards: quiet hours 02:00–09:00 local, a 40-minute shared throttle (`~/ibc/run/cron_autostart.json`), and at most 2 attempts per outage.
- `ibc-overrides/maintenance-reconnect/` is an optional IBC 3.23.0 patch that lets Gateway reconnect natively after the transient TLS error seen during IBKR's APAC maintenance reset, instead of cold-restarting and re-prompting 2FA.
- Overnight (20:00–03:50 ET) stock orders need `BypassRedirectOrderWarning=yes` in IBC's `config.ini`; see `references/stock-session-routing.md`.

### Python

```bash
/opt/homebrew/bin/python3.12 -m venv .venv-ibkr
.venv-ibkr/bin/python -m pip install -r skills/ibkr-trading/requirements.txt
```

`scripts/_ib.py` prefers `ib_async` and falls back to `ib_insync`; `IBKR_PYTHON_BACKEND=ib_insync|ib_async` forces one. Run live orders with the venv interpreter. Details in `references/ib-async-migration.md`.

### Flex Web Service (historical tools)

1. IBKR Client Portal → Settings → Account Settings → Reporting → **Flex Web Service** → enable and copy the **Token**.
2. Reporting → Flex Queries → new **Activity Flex Query**: sections Trades + Open Positions + Cash Transactions, period Last 365 Days, format XML. Note the **Query ID**.
3. Put both in a git-ignored `.env` at the repo root:

```
IBKR_FLEX_TOKEN=<token>
IBKR_FLEX_QUERY_ID=<id>
```

### Telegram (optional notifications)

`option_risk.py --notify` and `price_alert.py` read `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from the environment.

## Tools

| Purpose | Script | Source |
|---|---|---|
| Today's fills / positions / account funds | `live_fills.py`, `live_positions.py`, `live_account.py` | Gateway |
| Stock session + quote + position + orders snapshot | `stock_status.py` | Gateway |
| Stock orders (limit / market / stop, safe replace) | `stock_order.py` | Gateway |
| Option chain scan (CSP / CC / long call / PMCC) | `option_chain.py` | Gateway |
| Option quotes with Greeks | `quote_option.py` | Gateway |
| Roll / spread / covered-call candidates from positions | `option_roll.py` | Gateway |
| Option orders (single leg + combo) | `option_order.py` | Gateway |
| Daily option risk report | `option_risk.py` | Gateway |
| Account leverage and liquidity check | `check_leverage.py` | Gateway |
| One-shot intraday price alert | `price_alert.py` | Gateway |
| Offline Black-Scholes Greeks / implied vol | `bs_greeks.py` | none |
| Historical trades | `flex_trades.py` | Flex |
| Reconcile a position against a local markdown note | `flex_reconcile.py` | Flex |
| Weekly buy-window ledger | `trade_quota.py` | local |

Rule of thumb: "today / just now" → live tools; "yesterday / last month / reconcile" → Flex.

## Usage

All commands run from the repo root; `PY=.venv-ibkr/bin/python`, `S=skills/ibkr-trading/scripts`.

### Live queries

```bash
$PY $S/live_fills.py --symbol AAPL
$PY $S/live_positions.py --asset OPT
$PY $S/live_account.py --json          # check AvailableFunds / InitMarginReq before ordering
$PY $S/check_leverage.py
```

### Stock orders

```bash
# 1. snapshot before the confirmation card
$PY $S/stock_status.py XYZ --action BUY --limit 48.60

# 2. after "confirm": route must match the card
$PY $S/stock_order.py --symbol XYZ --action BUY --qty 100 --limit 48.60 \
  --route SMART --require-marketable --execute

# safe replace: size by final target position, absorbing fills during the cancel race
$PY $S/stock_order.py --symbol XYZ --action BUY --target-position 100 --replace 69 \
  --limit 48.60 --route SMART --require-marketable --execute

# server-side stop: --stop + --limit = STP LMT, --stop + --market = STP
$PY $S/stock_order.py --symbol XYZ --action SELL --qty 100 --stop 45.00 --limit 43.00 \
  --tif GTC --route SMART --execute
```

- Defaults: `outsideRth=True` and IBKR Price Management Algo on; disable with `--no-outside-rth` / `--no-price-mgmt`. `outsideRth` does **not** cover the 20:00–03:50 ET overnight session — use `--route OVERNIGHT` there.
- A route that does not match the current session is refused unless `--allow-queued`.
- Emergency exits use a marketable limit with `--require-marketable`, never an uncapped market order.
- Stops are SELL-only, must trigger below the live bid, fire in RTH only by default, force PMA off and are not supported on OVERNIGHT. A gap below the protective limit leaves an STP LMT unfilled — by design.
- Verify every live order afterwards with `stock_status.py`: fills, position and active orders together, not a single `placeOrder` return.

### Options

```bash
$PY $S/option_chain.py AMD --right P --moneyness otm                      # CSP scan
$PY $S/option_chain.py PLTR --right C --moneyness otm --payoff            # long-call payoff multiples
$PY $S/option_chain.py TSLA --right C --moneyness itm --dte-min 150 --dte-max 400   # PMCC long leg
$PY $S/option_roll.py META 2>/dev/null                                    # roll an existing short leg
$PY $S/bs_greeks.py --spot 630 --strike 600 --dte 30 --type call --price 50

# sell a CSP (dry-run first, then add --execute)
$PY $S/option_order.py --symbol AMD --expiry 20260618 --strike 200 --right P \
  --action SELL --qty 1 --limit 8.20

# credit roll as one combo: BUY combo with a NEGATIVE net limit = receive credit
$PY $S/option_order.py --symbol META --combo \
  --leg BUY:20260821:650:C:1 --leg SELL:20260918:650:C:1 \
  --net-action BUY --net-limit -8.10
```

- Option orders default to DAY limit orders and are never auto-re-placed; use `--tif GTC` explicitly.
- Combo direction follows IBKR convention: a `BUY` combo executes legs as defined, a `SELL` combo reverses every leg. Credits use a negative limit on a `BUY` combo; `SELL +X` posts reversed legs that never fill.
- HK options: `--exchange SEHK --currency HKD --trading-class <class> --no-outside-rth` (look up the class via `reqSecDefOptParams`, e.g. Tencent 700 → `TCH`).
- `option_roll.py` hides candidates whose total premium is below `--min-total` (default $500; `0` disables). Its `score` column is a generic ranking — judge by OTM% / annualized / DTE yourself.
- Strategy notes (IV regime routing, PMCC payback, combined exposure): `references/option-strategy-playbook.md`.

### Historical (Flex)

```bash
$PY $S/flex_trades.py --symbol AAPL --asset OPT --since 2026-01-01
$PY $S/flex_reconcile.py --symbol AAPL --md-path docs/positions/US.AAPL.md --json   # exit 0 match / 1 error / 2 diff
```

### Buy window and alerts

```bash
$PY $S/trade_quota.py status
$PY $S/trade_quota.py check --action BUY        # exit 2 when the window is closed
$PY $S/price_alert.py --symbol AAPL --above 300 --note "reclaimed 300"
```

`price_alert.py` fires once per alert (state in `logs/price_alert.json`; delete the key to re-arm) and exits silently outside RTH. If Gateway is down it goes through `ensure_gateway()` (same guards as above) and sends a Telegram notice only when it actually triggered a start that timed out waiting for 2FA.

## Scheduling the daily option risk report

`option_risk.py` checks every option position (short leg breached, CSP assignment, near expiry, long-call theta, PMCC long-leg health, uncovered shorts) and grades each 🔴 / 🟡 / 🟢 with a roll / close / add suggestion. After the close IBKR returns no model Greeks, so it back-solves a Black-Scholes delta from the option price. `--guard` only lets it run 09:40–10:20 ET, so two cron lines one hour apart cover both DST regimes:

```cron
0 22 * * 1-5 cd /path/to/wealthlens && set -a && . ./.env && set +a && .venv-ibkr/bin/python skills/ibkr-trading/scripts/option_risk.py --notify --guard >> logs/option_risk.log 2>&1
0 23 * * 1-5 cd /path/to/wealthlens && set -a && . ./.env && set +a && .venv-ibkr/bin/python skills/ibkr-trading/scripts/option_risk.py --notify --guard >> logs/option_risk.log 2>&1
```

(The times above are for a UTC+8 host; adjust to land around 10:00 ET.)

## Known constraints

- Market data is not auto-downgraded by the API: without a real-time subscription (e.g. SEHK), `reqMktData` returns `nan` with Error 354. Call `reqMarketDataType(3)` for delayed data (last/close only, bid/ask `-1`).
- Option `markPrice` without an OPRA subscription may be 0 or the last close.
- `option_chain.py` / `option_roll.py` emit IB `Error 200` noise for strikes missing in some expiries; filter with `2>/dev/null`.
- Flex is rate-limited to about one request per minute per query; responses are cached for one hour in `~/.cache/ibkr-flex/`. Only complete `FlexStatement` responses are cached — warnings, errors and empty responses fail closed. Same-day fills appear in Flex with ~30 min delay.
- Flex tokens are read-only but expose the full account history; reset immediately if leaked.

## Layout

```
skills/ibkr-trading/
├── SKILL.md
├── requirements.txt
├── ibc-overrides/maintenance-reconnect/   # optional IBC patch (Java source, test, build script)
├── references/
│   ├── stock-session-routing.md           # SMART / OVERNIGHT routing and safe replace
│   ├── option-strategy-playbook.md        # option strategy framework
│   └── ib-async-migration.md              # ib_insync → ib_async notes
└── scripts/
    ├── _ib.py  _gw.py  _client_ids.py  _stock.py  _flex.py   # internal helpers
    ├── *.py                                                  # CLI tools (see table)
    └── tests/                                                # offline unit tests + paper matrix
```

Offline tests never touch a Gateway:

```bash
cd skills/ibkr-trading/scripts && ../../../.venv-ibkr/bin/python -m unittest discover -s tests -p 'test_*.py'
```

`tests/paper_exec_matrix.py` exercises real order paths against a **paper** Gateway on port 4002 and refuses any non-`DU` account.
