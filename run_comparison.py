import os
import sys
import glob
import csv
import json
import argparse
from datetime import datetime

DEFAULT_DATA_DIR = r"c:\Users\Jeremy\Documents\projects\collect_data\combined_dataset\BTC"

def load_dataset(data_dir: str):
    """
    Loads all btc_15m_markets_*.csv files from data_dir in chronological order.
    Returns (outcomes, bodies, open_times, tickers).
    """
    market_files = sorted(glob.glob(os.path.join(data_dir, "**", "btc_15m_markets_*.csv"), recursive=True))
    if not market_files:
        raise FileNotFoundError(f"No market CSV files found in {data_dir}")

    markets = {}
    chronological_slugs = []

    for mf in market_files:
        with open(mf, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ticker = row.get("market_ticker")
                res = str(row.get("result", "")).strip().lower()
                if res in ["yes", "up"]:
                    outcome = "Up"
                elif res in ["no", "down"]:
                    outcome = "Down"
                else:
                    continue
                open_time = str(row.get("open_time", ""))
                markets[ticker] = {
                    "outcome": outcome,
                    "open_time": open_time,
                    "coin_start": float(row.get("coin_price_start", 0) or 0),
                    "coin_end": float(row.get("coin_price_end", 0) or 0),
                }
                chronological_slugs.append((open_time, ticker))

    chronological_slugs.sort(key=lambda x: x[0])
    seen = set()
    slug_order = []
    for s in chronological_slugs:
        if s[1] not in seen and s[1] in markets:
            seen.add(s[1])
            slug_order.append(s[1])

    outcomes = [markets[s]["outcome"] for s in slug_order]
    bodies = [abs(markets[s]["coin_end"] - markets[s]["coin_start"]) for s in slug_order]
    open_times = [markets[s]["open_time"] for s in slug_order]
    tickers = slug_order

    return outcomes, bodies, open_times, tickers


def simulate_martingale(
    outcomes: list[str],
    bodies: list[float],
    consecutive_streak_count: int = 2,
    pattern: tuple[str, ...] = ("FADE", "FOLLOW", "FADE"),
    multipliers: tuple[float, ...] = (1.0, 2.0, 4.0),
    base_stake_usd: float = 10.0,
    min_body: float = 0.0,
    entry_price: float = 0.48,
    strategy_name: str = "Special Martingale Pattern C",
) -> dict:
    """
    Simulates a Martingale campaign strategy using strict forward out-of-sample execution.
    At beat i:
      Historical window is 0 .. i-1.
      Order is placed on beat i, resolved against outcomes[i].
    """
    N = len(outcomes)
    cap = len(pattern)
    stakes = [base_stake_usd * m for m in multipliers]

    in_camp = False
    step = 0
    trigger_dir = None

    pnl = 0.0
    peak = 0.0
    max_dd = 0.0

    camp_wins = 0
    camp_losses = 0
    total_trades = 0
    trade_wins = 0
    step_wins = [0] * cap
    filter_skips = 0

    pnl_history = []

    for i in range(consecutive_streak_count, N):
        actual = outcomes[i]

        # Calculate streak on resolved tail [0 .. i-1]
        streak_len = 0
        streak_side = None
        for j in range(i - 1, -1, -1):
            out = outcomes[j]
            if streak_side is None:
                streak_side = out
                streak_len = 1
            elif out == streak_side:
                streak_len += 1
            else:
                break

        # Check for trigger if idle (trigger only on fresh streak_len == consecutive_streak_count)
        if not in_camp:
            if streak_len == consecutive_streak_count and streak_side is not None:
                if min_body > 0.0 and bodies[i - 1] < min_body:
                    filter_skips += 1
                    continue
                in_camp = True
                step = 0
                trigger_dir = streak_side

        if in_camp:
            total_trades += 1
            action = pattern[step]
            stake = stakes[step]

            if action == "FOLLOW":
                bet_side = trigger_dir
            elif action == "FADE":
                bet_side = "Down" if trigger_dir == "Up" else "Up"

            if actual == bet_side:
                # WON STEP
                trade_wins += 1
                camp_wins += 1
                step_wins[step] += 1
                profit = (stake / entry_price) * (1.0 - entry_price)
                pnl += profit
                in_camp = False
                step = 0
                trigger_dir = None
            else:
                # LOST STEP
                pnl -= stake
                step += 1
                if step >= cap:
                    camp_losses += 1
                    in_camp = False
                    step = 0
                    trigger_dir = None

            if pnl > peak:
                peak = pnl
            if (peak - pnl) > max_dd:
                max_dd = peak - pnl

            pnl_history.append(pnl)

    tot_camps = camp_wins + camp_losses
    c_wr = (camp_wins / tot_camps * 100.0) if tot_camps > 0 else 0.0
    t_wr = (trade_wins / total_trades * 100.0) if total_trades > 0 else 0.0
    ret_dd = (pnl / max_dd) if max_dd > 0 else 0.0

    return {
        "strategy_name": strategy_name,
        "consecutive_streak_count": consecutive_streak_count,
        "pattern": "-".join(pattern),
        "multipliers": multipliers,
        "base_stake_usd": base_stake_usd,
        "min_body": min_body,
        "entry_price": entry_price,
        "total_beats": N,
        "campaigns": tot_camps,
        "camp_wins": camp_wins,
        "camp_losses": camp_losses,
        "camp_wr": c_wr,
        "total_trades": total_trades,
        "trade_wins": trade_wins,
        "trade_wr": t_wr,
        "step_wins": step_wins,
        "filter_skips": filter_skips,
        "net_pnl": pnl,
        "max_drawdown": max_dd,
        "ret_dd_ratio": ret_dd,
    }


def print_single_summary(r: dict):
    print("\n" + "=" * 80)
    print(f"  SIMULATION RESULTS: {r['strategy_name']}")
    print("=" * 80)
    print(f"  Trigger Streak         : {r['consecutive_streak_count']} consecutive candles")
    print(f"  Action Pattern         : {r['pattern']} (Steps 1-{len(r['step_wins'])})")
    print(f"  Stake Multipliers      : {r['multipliers']} (Base: ${r['base_stake_usd']:.2f})")
    print(f"  Candle Body Filter     : Body >= ${r['min_body']:.2f}" if r['min_body'] > 0 else "  Candle Body Filter     : None")
    print(f"  Limit Entry Price      : ${r['entry_price']:.2f}")
    print(f"  Total Analyzed Beats   : {r['total_beats']:,}")
    print("-" * 80)
    print(f"  Completed Campaigns    : {r['campaigns']:,} (Filter Skips: {r['filter_skips']:,})")
    print(f"  Campaign Win Rate      : {r['camp_wr']:.2f}% ({r['camp_wins']:,} Won / {r['camp_losses']:,} Lost)")
    
    step_str_parts = []
    for s_idx, s_wins in enumerate(r['step_wins']):
        pct = (s_wins / r['campaigns'] * 100.0) if r['campaigns'] > 0 else 0.0
        step_str_parts.append(f"Step {s_idx+1}: {s_wins:,} ({pct:.1f}%)")
    print(f"  Step Wins Breakdown    : {' | '.join(step_str_parts)}")
    print(f"  Hard Cap Stop Losses   : {r['camp_losses']:,} campaigns (-${r['base_stake_usd'] * sum(r['multipliers']):.2f} each)")
    print("-" * 80)
    print(f"  Total Individual Bets  : {r['total_trades']:,} (Trade Win Rate: {r['trade_wr']:.2f}%)")
    print(f"  Net Profit / Loss      : ${r['net_pnl']:+,.2f}")
    print(f"  Maximum Drawdown       : ${r['max_drawdown']:,.2f}")
    print(f"  Return / Drawdown Ratio: {r['ret_dd_ratio']:.2f}x")
    print("=" * 80 + "\n")


def print_comparison_table(results: list[dict]):
    print("\n" + "=" * 135)
    print("STRATEGY COMPARISON MATRIX")
    print("=" * 135)
    header = (
        f"{'STRATEGY NAME':<38} | {'PATTERN':<18} | {'MULT':<16} | {'FILTER':<11} | {'PX':<5} | "
        f"{'CAMPS':<6} | {'C_WR':<7} | {'NET PNL':<12} | {'MAX DD':<9} | {'RET/DD':<7}"
    )
    print(header)
    print("-" * 135)
    for r in results:
        f_str = f"Body>={int(r['min_body'])}" if r['min_body'] > 0 else "None"
        mult_str = str(tuple(r['multipliers']))
        row = (
            f"{r['strategy_name'][:38]:<38} | "
            f"{r['pattern']:<18} | "
            f"{mult_str:<16} | "
            f"{f_str:<11} | "
            f"${r['entry_price']:.2f} | "
            f"{r['campaigns']:<6} | "
            f"{r['camp_wr']:<6.1f}% | "
            f"${r['net_pnl']:<+11.2f} | "
            f"${r['max_drawdown']:<8.2f} | "
            f"{r['ret_dd_ratio']:<6.2f}x"
        )
        print(row)
    print("=" * 135 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Kalshi Special Martingale Simulation & Comparison Runner")
    parser.add_argument("--config", type=str, default="config.json", help="Path to config JSON file")
    parser.add_argument("--data-dir", type=str, default=DEFAULT_DATA_DIR, help="Path to BTC market dataset")
    parser.add_argument("--compare", action="store_true", help="Run multi-strategy comparison suite")
    parser.add_argument("--base-stake", type=float, default=None, help="Override base stake USD")
    parser.add_argument("--entry-price", type=float, default=None, help="Override maker entry price (e.g. 0.48, 0.50, 0.52)")
    parser.add_argument("--min-body", type=float, default=None, help="Override Binance candle body threshold")

    args = parser.parse_args()

    print(f"Loading market dataset from {args.data_dir}...")
    outcomes, bodies, open_times, tickers = load_dataset(args.data_dir)
    print(f"Loaded {len(outcomes):,} chronological beats ({open_times[0]} to {open_times[-1]}).")

    if args.compare:
        # Define comparison configurations
        compare_configs = [
            {
                "name": "Pattern C (Body >= $75) [Recommended]",
                "streak": 2,
                "pattern": ("FADE", "FOLLOW", "FADE"),
                "multipliers": (1.0, 2.0, 4.0),
                "min_body": 75.0,
                "price": 0.48,
            },
            {
                "name": "Pattern C (Body >= $75, Fair $0.50)",
                "streak": 2,
                "pattern": ("FADE", "FOLLOW", "FADE"),
                "multipliers": (1.0, 2.0, 4.0),
                "min_body": 75.0,
                "price": 0.50,
            },
            {
                "name": "Pattern C (Body >= $75, Taker $0.52)",
                "streak": 2,
                "pattern": ("FADE", "FOLLOW", "FADE"),
                "multipliers": (1.0, 2.0, 4.0),
                "min_body": 75.0,
                "price": 0.52,
            },
            {
                "name": "Pattern C (Gentle Multipliers 1-1.5-2.5)",
                "streak": 2,
                "pattern": ("FADE", "FOLLOW", "FADE"),
                "multipliers": (1.0, 1.5, 2.5),
                "min_body": 75.0,
                "price": 0.48,
            },
            {
                "name": "Pattern A (No Filter, Maker $0.48)",
                "streak": 2,
                "pattern": ("FADE", "FOLLOW", "FADE"),
                "multipliers": (1.0, 2.0, 4.0),
                "min_body": 0.0,
                "price": 0.48,
            },
            {
                "name": "Baseline ALGO 3 (FOLLOW-FADE-FADE)",
                "streak": 2,
                "pattern": ("FOLLOW", "FADE", "FADE"),
                "multipliers": (1.0, 2.0, 4.0),
                "min_body": 0.0,
                "price": 0.48,
            },
            {
                "name": "Pattern F Sniper (Streak 4, Body>=100)",
                "streak": 4,
                "pattern": ("FADE", "FOLLOW", "FADE", "FADE"),
                "multipliers": (1.0, 2.0, 4.0, 8.0),
                "min_body": 100.0,
                "price": 0.48,
            },
        ]

        base_stake = args.base_stake if args.base_stake is not None else 10.0
        results = []
        for c in compare_configs:
            r = simulate_martingale(
                outcomes=outcomes,
                bodies=bodies,
                consecutive_streak_count=c["streak"],
                pattern=c["pattern"],
                multipliers=c["multipliers"],
                base_stake_usd=base_stake,
                min_body=c["min_body"],
                entry_price=c["price"],
                strategy_name=c["name"],
            )
            results.append(r)

        print_comparison_table(results)

    else:
        # Load single config
        if not os.path.exists(args.config):
            raise FileNotFoundError(f"Config file not found: {args.config}")

        with open(args.config, "r", encoding="utf-8") as f:
            cfg = json.load(f)

        strategy_name = cfg.get("strategy_name", "Special Martingale")
        streak_count = cfg.get("consecutive_streak_count", 2)
        base_stake = args.base_stake if args.base_stake is not None else cfg.get("base_stake_usd", 10.0)
        multipliers = tuple(cfg.get("stake_multipliers", [1.0, 2.0, 4.0]))
        price = args.entry_price if args.entry_price is not None else cfg.get("max_maker_price", 0.48)
        
        delta_cfg = cfg.get("delta_filter", {})
        min_body = 0.0
        if delta_cfg.get("enabled", False):
            min_body = float(delta_cfg.get("delta_threshold", 75.0) or 0.0)
        if args.min_body is not None:
            min_body = args.min_body

        # Pattern mapping
        pattern = ("FADE", "FOLLOW", "FADE")
        if "algo 3" in strategy_name.lower():
            pattern = ("FOLLOW", "FADE", "FADE")

        res = simulate_martingale(
            outcomes=outcomes,
            bodies=bodies,
            consecutive_streak_count=streak_count,
            pattern=pattern,
            multipliers=multipliers,
            base_stake_usd=base_stake,
            min_body=min_body,
            entry_price=price,
            strategy_name=strategy_name,
        )

        print_single_summary(res)


if __name__ == "__main__":
    main()
