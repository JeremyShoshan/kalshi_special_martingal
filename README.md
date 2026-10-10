# Kalshi Trend Transition Engine (Pattern C: FADE-FOLLOW-FADE)

A high-performance algorithmic trading engine designed for Kalshi crypto 15-minute beat markets (`KXBTC15M`), implementing the **Trend Transition Martingale Pattern (Pattern C)** with Binance candle body filtering.

---

## 🚀 Strategy Architecture: Pattern C ("Trend Transition Engine")

Conventional martingales fail because:
- **Pure Fading (Mean Reversion):** Bleeds catastrophic capital when BTC enters prolonged trending runs (5–8 consecutive green/red candles).
- **Pure Following (Trend Following):** Whipsaws severely in oscillating chop.
- **ALGO 3 (FOLLOW-FADE-FADE):** Placed Step 0 as `FOLLOW` on a 2-streak. In 15-minute BTC data, 2-candle streaks **revert (fade) 52.4% of the time** and only continue 47.6% of the time. Betting `FOLLOW` on Step 0 forced constant sequence advances and incurred spread drag.

### How Pattern C Harmonizes Market Structure:
1. **Trigger Condition:**
   - A streak of **2 consecutive candles** in the same direction (`streak_len >= 2`).
   - **Filter:** Trigger candle body $|Open - Close| \ge \$75$ on Binance. Filters out low-volume, choppy noise.
2. **Step 0 (FADE — Stake 1.0x / $10):**
   - **Action:** Bet **FADE** (opposite of the 2-candle streak).
   - **Rationale:** Exploits the **52.4% mean-reversion rate** of 2-candle streaks. Over 52% of campaigns win on the very first trade!
   - **Resolution:** If won, **Campaign WON!** Bank profit and reset to idle. If lost (the streak grew to 3), advance to Step 1.
3. **Step 1 (FOLLOW — Stake 2.0x / $20):**
   - **Action:** Bet **FOLLOW** (in the direction of the streak).
   - **Rationale:** If Step 0 lost, the streak reached 3 consecutive candles. The market has proven it is trending, not oscillating! Switch immediately to **ride the trend**.
   - **Resolution:** If won, **Campaign WON!** Bank profit and reset to idle. If lost (streak broke at candle 4), advance to Step 2.
4. **Step 2 (FADE — Stake 4.0x / $40):**
   - **Action:** Bet **FADE** (opposite of the original streak).
   - **Rationale:** If Step 1 lost, the trend reached exhaustion at candle 4 and reversed. Fade the exhaustion.
   - **Resolution:** If won, **Campaign WON!** Bank profit and reset to idle.
   - **Hard Cap 3 Stop-Loss:** If Step 2 loses, the hard cap of 3 is reached. The sequence is immediately terminated, banking the sequence stop-loss (-$70 base loss), and cleanly resetting to idle.

---

## 📊 Backtest Performance (All 5,219 Beats / 56 Days)

Evaluated under **strict forward out-of-sample execution** across all 5,219 chronological 15m beats in `combined_dataset\BTC` (Aug 13 – Oct 07, 2026):

| Metric | Pattern C (`Body >= $75`) | Unfiltered Pattern A | Baseline ALGO 3 |
| :--- | :---: | :---: | :---: |
| **Total Completed Campaigns** | **576** | 1,349 | 1,349 |
| **Campaign Win Rate** | **90.3%** | 89.2% | 87.7% |
| **Step Win Breakdown (0 / 1 / 2)** | **300 / 141 / 79** | 707 / 321 / 175 | 642 / 362 / 179 |
| **Net PnL @ $0.48 Maker (Base $10)** | **+$2,028.33** | +$3,517.50 | +$1,945.00 |
| **Net PnL @ $0.50 Fair (Zero-Spread)** | **+$1,280.00** | +$1,810.00 | +$210.00 |
| **Net PnL @ $0.52 Taker (Worst-Case Drag)**| **+$589.23** | +$233.85 | **-$1,391.54** |
| **Max Drawdown** | **$254.17** | $385.00 | $490.83 |
| **Return / Max Drawdown Ratio** | **7.98x** | 9.14x | 3.96x |

> **Key Advantage:** Pattern C is **profitable across all pricing conditions**, generating +$589.23 even under worst-case $0.52 taker drag where ALGO 3 loses -$1,391.54.

---

## 🛡️ Skip Case Handling & Safety Guardrails

1. **Unfilled Maker Orders (`stop_maker_sec` timeout):**
   - If a resting limit order is unfilled after `stop_maker_sec` (default 240s), it is cancelled via Kalshi API.
   - **$0 capital was risked.** The bot **NEVER doubles the stake on an unfilled order**.
   - If Step 0 was unfilled and the market reversed, the bot cleanly resets to idle ($0 loss).
   - If the streak continued or Step 1/2 was unfilled, the bot preserves the current step and stake for the next beat.
2. **Filter In-Campaign Bypass:**
   - The candle body filter ($|Open - Close| \ge \$75$) is evaluated **only on Step 0** when considering whether to trigger a new campaign.
   - Once a campaign is active (`in_campaign = True`), filter evaluation is **bypassed** on Step 1 and Step 2 so the martingale recovery sequence can execute to completion.

---

## ⚙️ Configuration (`config.json`)

```json
{
  "strategy_name": "ALGO Trend Transition (Pattern C: FADE-FOLLOW-FADE)",
  "mode": "dry_run",
  "asset": "BTC",
  "consecutive_streak_count": 2,
  "base_stake_usd": 10.0,
  "stake_multipliers": [1.0, 2.0, 4.0],
  "max_cap": 3,
  "cap": 3,
  "stop_maker_sec": 240,
  "max_maker_price": 0.48,
  "marketable_limit": true,
  "beat_duration_min": 15,
  "notifications": true,
  "delta_filter": {
    "enabled": true,
    "delta_max_range": null,
    "delta_threshold": 75.0,
    "source": "binance"
  }
}
```

---

## 🚀 How to Run

### 1. Run Verification Suite (Unit Tests)
```powershell
uv run python test_special_martingale_engine.py
```

### 2. Paper Trading / Dry-Run Mode
```powershell
uv run python bot_live.py --mode dry_run
```

### 3. Production Real Money Trading
```powershell
uv run python bot_live.py --mode real
```
