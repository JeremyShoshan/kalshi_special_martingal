# Kalshi Special Martingale Bot (ALGO 3: Trend-First Reversion Engine)

A high-performance algorithmic trading engine designed for Kalshi crypto 15-minute beat markets (`KXBTC15M`), implementing **ALGO 3 (Trend-First Reversion Engine)**.

---

## 🚀 Strategy Overview: ALGO 3 ("Trend-First Reversion")

Conventional martingale systems fail in directional markets because they repeatedly fight prolonged trends. Conversely, naive trend-following systems bleed out during prolonged chop.

**ALGO 3 solves this dilemma by harmonizing continuation momentum with mean-reversion exhaustion:**

1. **Step 1 (FOLLOW - Stake 1x / $10):**
   - **Trigger:** A streak of $\ge 2$ consecutive candles in the same direction (e.g., 2 Up or 2 Down).
   - **Action:** Bet **FOLLOW** (ride the continuation into candle 3).
   - **Rationale:** Two consecutive candles statistically have strong momentum to continue into a 3rd candle.
   - **Resolution:** If won, **Campaign WON!** Bank profit and reset to Step 1. If lost (broke into chop), advance to Step 2.

2. **Step 2 (FADE - Stake 2x / $20):**
   - **Action:** Bet **FADE** (opposite of the original streak).
   - **Rationale:** If the 3rd candle broke the streak, market has entered mean-reverting chop.
   - **Resolution:** If won, **Campaign WON!** Bank profit and reset to Step 1. If lost, advance to Step 3.

3. **Step 3 (FADE - Stake 4x / $40):**
   - **Action:** Bet **FADE** (opposite of the original streak).
   - **Resolution:** If won, **Campaign WON!** Bank profit and reset to Step 1.
   - **Hard Cap 3 Stop-Loss:** If Step 3 loses, the hard cap is reached. The sequence is immediately terminated, banking the sequence loss (-$70 base loss), and resetting to idle.

---

## 📊 Backtest Performance (55-Day Exact Orderbook Data)

Tested across **55 days of tick-level orderbook data** (5,219 fifteen-minute beats) on `KXBTC15M` with **exact Kalshi taker fee formulas deducted** (7% of $P(1-P)$) and **realistic GTC marketable limit order execution**:

| Metric | Result ($10 Base Stake) |
| :--- | :--- |
| **Total Completed Campaigns** | **2,398** |
| **Campaign Win Rate** | **98.7%** (2,367 Wins / 31 Losses) |
| **Net PnL (After All Fees)** | **+$21,677.80** |
| **Total Kalshi Taker Fees Paid** | **$779.44** |
| **Max Drawdown** | **$144.72** |
| **Return / Max Drawdown Ratio** | **149.8x** |
| **Overall Orderbook Fill Rate** | **90.8%** |
| **Order Fill Breakdown** | 73.6% Taker (with price improvement) / 26.4% Maker ($0 fee) |

> **Key Takeaway:** The hard cap of 3 limits maximum sequence loss to $70 ($10 + $20 + $40), while the 98.7% campaign win rate produces consistent, compounded growth with minimal drawdown.

---

## ⚙️ Order Execution Architecture

- **Order Type:** **Marketable Limit Order** (`GTC` with limit price `$0.52`).
  - At beat open ($t=0$), if the best ask is $\le \$0.52$, the order matches immediately as a **Taker** with full price improvement.
  - If the best ask is $> \$0.52$, the order rests on the book as a **Maker** at `$0.52`. If filled before `stop_maker_sec`, it incurs **$0 fee**.
- **Maker Timeout (`stop_maker_sec: 240`):**
  - If the order is unfilled after 240 seconds, it is automatically cancelled via the Kalshi API.

### 🛡️ Skip Case Handling & Safety Guardrails

1. **Unfilled Orders (Maker Timeout):**
   - If an order times out unfilled, **$0 capital was risked**.
   - **The bot NEVER doubles the stake on an unfilled order.**
   - **Step 1 Unfilled & Streak Broke:** If the market reversed while unfilled at Step 1, the bot cleanly resets to idle ($0 loss).
   - **Streak Continued or Step 2/3 Unfilled:** The bot preserves the current step and stake for the next beat.
2. **Filter Skips (Volatility / Delta Filters):**
   - If enabled and a candle fails the volatility or delta filter, the candidate bet is skipped.
   - The campaign step does NOT advance.
   - The bot records the skipped resolution (Filter Skip Win / Loss) and sends full Telegram telemetry.

---

## 🛠️ Setup & Configuration

### 1. Requirements
- Python 3.10+
- `uv` (recommended) or standard `venv`

### 2. Environment Variables (`.env`)
Create or verify `.env` in the project root:
```env
KALSHI_API_KEY_ID=your-api-key-id
KALSHI_PRIVATE_KEY_PATH=C:/path/to/kalshi-private-key.key
KALSHI_USE_DEMO=0

# Optional Telegram Notifications
TELEGRAM_BOT_TOKEN=your-telegram-bot-token
TELEGRAM_CHAT_ID=your-telegram-chat-id
MASK_PNL=0
```

### 3. Verify Connection
```powershell
uv run lib/kalshi_client.py
```

---

## 🏃 How to Run

### A. Dry-Run / Paper Trading Mode (Recommended First)
Simulates order fills, taker fees, state transitions, and Telegram notifications without spending real capital:
```powershell
uv run bot_live.py --mode dry_run
```

Or using the preset dry-run config:
```powershell
uv run bot_live.py --config configs/special_martingale_btc_dry_run.json
```

### B. Production Real Money Trading
Runs live trading on Kalshi BTC 15m beats (`KXBTC15M`):
```powershell
uv run bot_live.py --mode real
```

Or using the production preset config:
```powershell
uv run bot_live.py --config configs/special_martingale_btc.json
```

### C. Custom Parameters CLI Override
You can customize base stake, multipliers, limit price, or timeout directly via CLI flags:
```powershell
uv run bot_live.py --mode real --base-stake-usd 10 --stake-multipliers 1,2,4 --max-cap 3 --max-maker-price 0.52 --stop-maker-sec 240
```

---

## 📁 Configuration File (`config.json`)

```json
{
  "strategy_name": "ALGO 3 - Special Martingale (Trend-First Reversion Engine)",
  "mode": "dry_run",
  "asset": "BTC",
  "consecutive_streak_count": 2,
  "base_stake_usd": 10.0,
  "stake_multipliers": [1.0, 2.0, 4.0],
  "max_cap": 3,
  "stop_maker_sec": 240,
  "max_maker_price": 0.52,
  "marketable_limit": true,
  "beat_duration_min": 15,
  "notifications": true
}
```

---

## 📱 Telegram Notifications & Logs

- **Rotating Logs:** All console logs are automatically written to rotating 5MB log files under `logs/YYYY-MM-DD/`.
- **Rich Telegram Alerts:**
  - `⚡ SPECIAL MARTINGALE - TRIGGER` with step badges `[STEP 1/3 (FOLLOW)]`, `[STEP 2/3 (FADE)]`.
  - `✅ SPECIAL MARTINGALE - TRADE CLOSED` with campaign status and campaign win rate.
  - `🚨 SPECIAL MARTINGALE - ORDER UNFILLED (SKIPPED)` showing preserved step or clean reset.
  - `🛡️ SPECIAL MARTINGALE - FILTER_SKIP` showing condition breakdowns.
