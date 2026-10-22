# WealthLens — OpenClaw Skill

> Multi-broker portfolio dashboard skill for [OpenClaw](https://github.com/openclaw/openclaw) agents.

## What It Does

Lets your OpenClaw agent query a real-time portfolio dashboard across multiple brokers and crypto exchanges, directly from chat (Telegram, Discord, etc.).

When a user says a trigger word, the agent runs the `wealthlens` CLI and returns a formatted asset summary including stocks, crypto, P&L, and FX rates.

## Trigger Words

Any of these in chat will invoke the dashboard:

- `wl`
- `看板`
- `dashboard`
- `wealthlens`

## Prerequisites

1. **WealthLens CLI** installed and on `PATH` (see [main README](../README.md))
2. **API keys** configured in `config/secrets.json` (copy from `config/secrets.example.json`)
3. At least one data source running:
   - **Futu** (port 8000) — requires Futu OpenD
   - **IBKR** (port 8001) — requires IB Gateway + IBC
   - **Crypto** — works out of the box with API keys (OKX / Binance / Bitget)

## Setup

### 1. Install the CLI

```bash
cd /path/to/wealthlens
ln -sf $(pwd)/wealthlens /usr/local/bin/wl
```

### 2. Configure API Keys

```bash
cp config/secrets.example.json config/secrets.json
# Fill in your exchange API keys (read-only recommended)
```

### 3. Add to OpenClaw Agent

In your OpenClaw agent's `AGENTS.md` or skill config, reference this skill so the agent knows to run `wl` when triggered.

Example agent instruction:
```markdown
When the user says "wl", "看板", "dashboard", or "wealthlens":
1. Run `wl` in the terminal
2. Format the output as a code block and send it back
```

## Data Sources

| Source | Port | Required Service | Notes |
|--------|------|-----------------|-------|
| Futu | 8000 | Futu OpenD + backend-futu | HK & US stocks |
| IBKR | 8001 | IB Gateway + IBC + backend-ibkr | US & global stocks |
| OKX | — | Direct API | Spot / Earn / Trading |
| Binance | — | Direct API | Spot / Earn |
| Bitget | — | Direct API | Spot |
| FX Rates | — | open.er-api.com | Auto-cached, no key needed |

All sources are optional. If a source is unavailable, it's silently skipped.

## CLI Usage

```bash
wl                    # Auto-detect backend, fallback to direct
wl --direct           # Skip backend, connect to exchanges directly
wl --watch            # Auto-refresh every 60s
wl --watch --interval 30  # Custom refresh interval
```

## Fallback Strategy

```
Backend API (:3001)
  └─ fail → Direct mode (Futu :8000 + IBKR :8001 + Crypto APIs)
               └─ fail → Disk cache (snapshot 10min / FX 24h)
                            └─ fail → Hardcoded FX rates
```

## Output Format

Terminal-colored table, sorted by market value:

```
💰 WealthLens  2026-01-15 10:30  [direct]
总资产 $125,000 (¥906,250)

── Futu ──────────────────────────────
  AAPL       $ 15,000      +$2,100
  00700      $ 12,500      +$1,800

── IBKR ──────────────────────────────
  VOO        $  5,000        +$320
  GOOG       $  3,500        +$180

── 加密 ──────────────────────────────
  OKX      $ 10,000  USDT BTC ETH
  Binance  $  5,000  USDT BNB

── 📊 盈亏 ───────────────────────────
  Futu      +$4,350
  IBKR        +$500
  总盈亏    +$4,850

  USD/CNY 7.2500 (2.3h ago)
```

## IBKR Setup (Optional)

If you use Interactive Brokers, you need [IBC](https://github.com/IbcAlpha/IBC) to automate IB Gateway login:

```bash
# 1. Install IB Gateway from https://www.interactivebrokers.com/
# 2. Install IBC from https://github.com/IbcAlpha/IBC
# 3. Configure ~/ibc/config.ini:
#    IbLoginId=your_username
#    IbPassword=your_password
#    TradingMode=live
#    OverrideTwsApiPort=4001
#    ReadOnlyApi=yes

# 4. Start IB Gateway
cd ~/ibc && bash gatewaystartmacos.sh -inline &

# 5. Wait for port 4001, then start backend
cd /path/to/wealthlens/backend-ibkr
source venv/bin/activate
uvicorn main:app --host 127.0.0.1 --port 8001
```

## Security Notes

- Use **read-only** API keys for all exchanges
- `secrets.json` is git-ignored — never commit real keys
- IB Gateway runs with `ReadOnlyApi=yes` by default (the separate [ibkr-trading](ibkr-trading/SKILL.md) skill places orders and needs a write-enabled, launchd-managed Gateway)
- All data stays local, no external telemetry
