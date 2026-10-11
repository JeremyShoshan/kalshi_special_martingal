# Kalshi Trend Transition Engine (Pattern C: FADE-FOLLOW-FADE)

A high-performance algorithmic trading engine designed for Kalshi crypto 15-minute beat markets (`KXBTC15M`), implementing the **Trend Transition Martingale Pattern (Pattern C)** with Binance candle body filtering.

---

## ✅ Active Strategy (config.json): Streak-3 Double-Fade Breakout (FADE-FADE-FOLLOW)

Selected by the real order-book pattern search (`research/martingale_pattern_search.py`, 55 days BTC_250).

| Step | Action | Stake | Meaning |
| :---: | :---: | :---: | :--- |
| Trigger | - | - | exactly **3** consecutive same-side beats, last beat body >= **$50** (Binance) |
| 1 | FADE | 1x ($10) | bet the streak reverses |
| 2 | FADE | 2x ($20) | streak hit 4 - fade again |
| 3 | FOLLOW | 3x ($30) | streak hit 5 - breakout confirmed, ride it; loss = hard cap |

Execution: marketable limit **$0.45**, order sent **15s** into the beat (after the previous beat settles), unfilled remainder cancelled at **120s**, no taker fallback.

Real replay (strict fills, $10 base): **+$1,067.83**, max DD **$148.45**, campaign win rate **88.9%** (217/27), fill rate 66%, fees $89.83; all 4 chronological blocks positive.

Live-engine notes:
- `pattern` (list of FADE/FOLLOW per step) is configurable; default stays FADE-FOLLOW-FADE.
- After every settled result the bot recomputes the next step's side/stake from `pattern`; the delayed entry waits until the previous beat is evaluated (never trades a stale step).
- Legacy history-based sync corrections are off (`sync_corrections: false`); the replay never used them.

---

## 🚀 Strategy Architecture (legacy): Pattern C ("Trend Transition Engine")

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

> [!WARNING]
> The table below assumes **every order fills 100% at a fixed price** (no order book). Replaying the
> same signals against the real Kalshi order book + trade tape (`combined_dataset\BTC_250`) changes the
> picture completely - see **Real Order Book Replay** right after the table.

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

### Real Order Book Replay (`run_simulation.py`, BTC_250, Body >= $75, base $10)

Each order is replayed exactly like `bot_live.py` sends it (marketable limit at `max_maker_price`, taker at the
best ask if it is <= limit, otherwise resting maker filled only by real tape prints at/through the limit,
cancel at `stop_maker_sec`, Kalshi fees, partial fills, unfilled-skip rules, decisive-close trigger).

| Limit | Ideal (old assumption) | Real replay - strict | Real replay - queue(100) | Real replay - touch (optimistic) |
| :---: | :---: | :---: | :---: | :---: |
| $0.45 | +$3,064 | **-$537** | -$416 | -$367 |
| $0.48 | +$1,824 | **-$85** (DD $531) | +$257 | +$307 |
| $0.50 | +$1,080 | **-$30** | +$330 | +$450 |
| $0.52 | +$393 | **+$182** (DD $391) | +$105 | +$120 |

Why the edge disappears: **adverse selection + fees**. Orders that never fill are the ones that would
have won (75% win rate on unfilled orders), while resting bids that do get hit win far less often
(Step 1 maker fills proven by a seller crossing below the bid win only ~32%). Taker fills pay ~$230 in fees.

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
  "strategy_name": "ALGO Streak3 Double-Fade Breakout (FADE-FADE-FOLLOW)",
  "mode": "dry_run",
  "asset": "BTC",
  "consecutive_streak_count": 3,
  "pattern": ["FADE", "FADE", "FOLLOW"],
  "base_stake_usd": 10.0,
  "stake_multipliers": [1.0, 2.0, 3.0],
  "max_cap": 3,
  "cap": 3,
  "min_maker_sec": 15,
  "stop_maker_sec": 120,
  "max_maker_price": 0.45,
  "marketable_limit": true,
  "taker_limit_price": null,
  "sync_corrections": false,
  "beat_duration_min": 15,
  "notifications": true,
  "delta_filter": {
    "enabled": true,
    "delta_max_range": null,
    "delta_threshold": 50.0,
    "source": "binance"
  }
}
```

---

## 🚀 How to Run

### 1. Run Verification Suite (Unit Tests)
```powershell
uv run python test_special_martingale_engine.py
uv run python run_simulation.py --config config.json --trades-out logs/streak3_trades.csv
uv run python test_streak3_pattern.py   # cross-checks every replay order vs bot_live state machine
```

### 1b. Real Order Book Replay Simulation
```powershell
uv run python run_simulation.py                                  # config.json, strict maker fills
uv run python run_simulation.py --compare                        # fill model x limit price matrix
uv run python run_simulation.py --fill-model queue --queue-ahead 200 --trades-out logs/sim.csv
uv run python run_simulation.py --decision result --maker-fee 0.0175 --start-date 2026-09-01
```

### 2. Paper Trading / Dry-Run Mode
```powershell
uv run python bot_live.py --mode dry_run
```

### 3. Production Real Money Trading
```powershell
uv run python bot_live.py --mode real
```
