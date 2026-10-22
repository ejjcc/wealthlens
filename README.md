# 💰 WealthLens

A multi-broker **wealth aggregation tool** with three interfaces:

🖥️ **GUI** — Next.js dashboard for visual overview
⌨️ **CLI** — `wealthlens` / `wl` command, works with or without backend
💬 **Chat** — AI-powered queries via Telegram/OpenClaw

### Supported Data Sources
- 📈 **Stocks**: Futu (HK/US) + Interactive Brokers (US/Global)
- 💎 **Crypto**: OKX, Binance, Bitget (spot + earn)
- 💱 **FX Rates**: Multi-source with smart caching & disk persistence

---

## 🎯 Key Features

### Multi-Source Integration
- **CEX**: OKX, Binance, Bitget spot/earn/funding
- **Stocks (Futu)**: HK & US markets via Futu OpenD API
- **Stocks (IBKR)**: US & global markets via IB Gateway + IBC
- **Manual**: Custom entries for cash, funds, etc.

### Smart Data Pipeline
- Three-tier data flow: Backend API → Direct exchange APIs → Disk cache
- 10-minute snapshot caching with automatic invalidation
- FX rate caching: open.er-api.com → disk cache (24h) → hardcoded fallback
- Proxy-aware HTTP client (auto-detects `HTTPS_PROXY`, uses curl for external requests)

### CLI Dashboard
- Colorized terminal output with P&L
- Automatic currency conversion (HKD → USD)
- Options detection & display (e.g., `TSLA期权`)
- CJK-aware column alignment

### Architecture
```
┌──────────┐  ┌──────────┐  ┌──────────┐
│ Browser  │  │ CLI/TUI  │  │ OpenClaw │
│ :3000    │  │ wl / wealthlens │  │ Telegram │
└────┬─────┘  └────┬─────┘  └────┬─────┘
     │             │              │
     └─────────────┼──────────────┘
                   │ HTTP
                   ▼
            ┌──────────────┐
            │   NestJS     │
            │   Backend    │
            │   :3001      │
            └──────┬───────┘
                   │
     ┌─────────┬───┴───┬──────────┐
     │         │       │          │
     ▼         ▼       ▼          ▼
┌─────────┐ ┌──────┐ ┌────────┐ ┌───────┐
│  Futu   │ │ IBKR │ │ Crypto │ │  FX   │
│ :8000   │ │ :8001│ │ Direct │ │ Rates │
└─────────┘ └──────┘ └────────┘ └───────┘
     │         │         │
     ▼         ▼         ▼
  Futu OpenD  IB GW   OKX/Binance/Bitget
              :4001
```

---

## 🚀 Quick Start

### 1. Configure API Keys

```bash
cp config/secrets.example.json config/secrets.json
# Edit with your exchange API keys (read-only recommended)
```

`secrets.json` format:
```json
{
  "okx": { "apiKey": "...", "secret": "...", "password": "..." },
  "binance": { "apiKey": "...", "secret": "..." },
  "bitget": { "apiKey": "...", "secret": "...", "password": "..." }
}
```

### 2. Install CLI

```bash
# Symlink to PATH (pick one)
ln -sf $(pwd)/wealthlens /usr/local/bin/wl
ln -sf $(pwd)/wealthlens /usr/local/bin/wealthlens
```

### 3. Use It

```bash
# One-shot view (auto-detects backend, falls back to direct)
wl

# Force direct mode (bypasses backend, talks to exchanges directly)
wl --direct

# Auto-refresh every 60s
wl --watch

# Custom interval
wl --watch --interval 30
```

---

## 📦 Services Setup

### Futu (Port 8000)

Requires [Futu OpenD](https://openapi.futunn.com/) running on port 11111.

```bash
cd backend-futu
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python main.py  # Starts on :8000
```

### IBKR (Port 8001)

Requires [IB Gateway](https://www.interactivebrokers.com/) + [IBC](https://github.com/IbcAlpha/IBC).

```bash
# 1. Start IB Gateway via IBC
cd ~/ibc && bash gatewaystartmacos.sh -inline &
# Wait for port 4001 to be ready

# 2. Start IBKR backend
cd backend-ibkr
source venv/bin/activate
uvicorn main:app --host 127.0.0.1 --port 8001
```

IBC config (`~/ibc/config.ini`) key settings:
```ini
IbLoginId=your_username
IbPassword=your_password
TradingMode=live
OverrideTwsApiPort=4001
ReadOnlyApi=yes
```

> ⚠️ **Session conflict**: IBKR only allows one active session per username. If you log into IB Gateway with the same username you use on the mobile TWS app, **the mobile session will be kicked off** (and vice versa).
>
> **Solution**: Create a second username (sub-account) in IBKR Account Management, and use it exclusively for IB Gateway. This way your mobile TWS and the API gateway can run simultaneously without conflicts.

### NestJS Backend (Port 3001) — Optional

Full backend with snapshots, history, and aggregation:

```bash
cd backend && npm install && npm run build
npm run start:dev  # or: pm2 start ecosystem.config.js
```

### Frontend (Port 3000) — Optional

```bash
cd frontend && npm install
npm run dev
```

---

## 🔄 Data Flow & Fallback

The CLI uses a three-tier fallback strategy:

1. **Backend API** (`:3001`) — Full snapshot with all sources aggregated
2. **Direct Mode** — Connects to Futu (`:8000`), IBKR (`:8001`), and crypto exchanges directly
3. **Disk Cache** — Falls back to cached snapshots (10-min TTL) and FX rates (24h TTL)

If any source fails, it's silently skipped — you always get whatever data is available.

### Caching

| Cache | TTL | Location |
|-------|-----|----------|
| Snapshot | 10 min | `data/snapshot-cache.json` |
| FX Rates | 24h (API) / 8h (refresh) | `data/rates-cache.json` |

---

## 📊 Output Example

```
💰 WealthLens  2026-01-15 10:30  [direct]
总资产 $125,000 (¥906,250)

── Futu ──────────────────────────────────
  AAPL       $ 15,000      +$2,100
  00700      $ 12,500      +$1,800
  MSFT       $  8,200        +$450

── IBKR ──────────────────────────────────
  VOO        $  5,000        +$320
  GOOG       $  3,500        +$180

── 加密 ──────────────────────────────────
  OKX      $ 10,000  USDT BTC ETH
  Binance  $  5,000  USDT BNB

── 📊 盈亏 ───────────────────────────────
  Futu      +$4,350
  IBKR        +$500
  总盈亏    +$4,850

  USD/CNY 7.2500 (2.3h ago)
```

---

## 🤖 Agent Skill: IBKR Trading

[`skills/ibkr-trading/`](skills/ibkr-trading/SKILL.md) is an optional agent skill that goes beyond the read-only dashboard: live/historical IBKR queries, session-aware stock orders (SMART / OVERNIGHT routing, safe replace, server-side stops), single-leg and combo option orders, a daily option risk report, price alerts and a weekly stock-buy window ledger. Order scripts are dry-run unless `--execute` is passed, and the skill defines a confirmation card the agent must get approved before any live order.

It needs a different Gateway setup from the dashboard above: `ReadOnlyApi=no`, and Gateway owned by a launchd job (`com.user.ibc-gateway`) instead of `gatewaystartmacos.sh -inline &` from a shell. See its [prerequisites](skills/ibkr-trading/SKILL.md#prerequisites).

---

## 🔐 Security & Privacy

- ✅ All API keys stored in `config/secrets.json` (git-ignored)
- ✅ No secrets in source code
- ✅ Read-only API keys recommended
- ✅ IB Gateway runs with `ReadOnlyApi=yes` for the dashboard (the optional IBKR trading skill needs a write-enabled API session — run it on a separate Gateway login if you want the dashboard to stay read-only)
- ✅ All data stays local — no external analytics or telemetry

---

## 🛠️ Customization

### Add New Exchange

Add a `fetch_xxx(secrets)` function in the CLI that returns:
```python
[{"symbol": "BTC", "amount": 0.5, "value_usd": 25000, "price": 50000, "exchange": "NewExchange", "type": "spot"}]
```

Then call it in the `main()` direct mode block.

### Adjust Snapshot Schedule

Edit `backend/src/scheduler/snapshot.scheduler.ts`:
```typescript
@Cron('0 */6 * * *')  // Every 6 hours
async handleSnapshotCron() { ... }
```

---

## 🐛 Troubleshooting

| Issue | Solution |
|-------|----------|
| No Futu data | Check Futu OpenD is running (port 11111) and backend-futu on port 8000 |
| No IBKR data | Start IB Gateway via IBC, wait for port 4001, then start backend-ibkr |
| "Invalid API signature" | Verify key format in `secrets.json`, check system clock sync |
| Stale data | Delete `data/snapshot-cache.json`, or use `wl --direct` |
| Proxy issues | Set `HTTPS_PROXY` env var, CLI auto-detects and uses curl |

---

## 🤖 AI Agent Integration

WealthLens ships with a skill definition for AI agents. Say `wl` or `看板` in chat and your agent runs the dashboard for you.

Full skill spec: **[skills/SKILL.md](./skills/SKILL.md)**

### OpenClaw

1. Clone or symlink this repo into your agent's skill directory:
   ```bash
   # Option A: symlink
   ln -s /path/to/wealthlens ~/.openclaw/workspace/skills/wealthlens

   # Option B: reference in AGENTS.md
   echo '- wealthlens skill: /path/to/wealthlens/skills/SKILL.md' >> ~/.openclaw/workspace/AGENTS.md
   ```

2. Make sure `wl` is on PATH:
   ```bash
   ln -sf /path/to/wealthlens/wealthlens /usr/local/bin/wl
   ```

3. The agent will automatically pick up the skill. Say `wl` or `看板` in any connected channel (Telegram, Discord, etc.) and get your portfolio.

### Claude Code / Codex

Add the skill to your `AGENTS.md` or project instructions:

```markdown
## Tools

When the user says "wl", "看板", "dashboard", or "wealthlens":
1. Run `wl` in the terminal
2. Return the output as a code block

WealthLens CLI reference: see skills/SKILL.md
```

Or pass it as a system prompt flag:
```bash
claude --system "$(cat /path/to/wealthlens/skills/SKILL.md)"
```

### Other AI Agents (Cursor, Windsurf, etc.)

The skill is a plain Markdown file — drop `skills/SKILL.md` into whatever context/rules/instructions file your agent uses. The key info:

- **Command**: `wl` (or `wl --direct` if no backend)
- **Trigger words**: `wl`, `看板`, `dashboard`, `wealthlens`
- **Output**: Terminal text, best rendered in a code block
- **No dependencies**: Pure Python CLI, zero pip packages

## 📖 Related Docs

- **[skills/SKILL.md](./skills/SKILL.md)** — OpenClaw agent skill (chat-based dashboard)
- **[skills/ibkr-trading/SKILL.md](./skills/ibkr-trading/SKILL.md)** — IBKR trading agent skill (orders, option risk, Flex history)
- **[SETUP.md](./SETUP.md)** — Complete setup guide with troubleshooting
- **[QUICKSTART.md](./QUICKSTART.md)** — Quick reference for common tasks
- **[AGENT_HANDOFF.md](./AGENT_HANDOFF.md)** — For AI agents to understand the system

---

## 📄 License

MIT License — Free for personal use.

**Disclaimer:** This tool is for personal wealth tracking only. Always verify financial data against official exchange statements.

---

**Made with ❤️ for personal finance tracking**

*Last updated: 2026-03-13*
