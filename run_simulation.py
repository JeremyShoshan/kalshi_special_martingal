import os
import sys
import glob
import csv
import json
import argparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_DIR = r"c:\Users\Jeremy\Documents\projects\collect_data\combined_dataset\BTC"


def load_dataset(data_dir: str):
    """
    Loads all btc_15m_markets_*.csv files from data_dir in chronological order.
    Returns (outcomes, bodies, open_times, slug_order, markets).
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

    return outcomes, bodies, open_times, slug_order, markets


def simulate_martingale(
    outcomes: list[str],
    bodies: list[float],
    slug_order: list[str],
    consecutive_streak_count: int = 2,
    pattern: tuple[str, ...] = ("FADE", "FOLLOW", "FADE"),
    multipliers: tuple[float, ...] = (1.0, 2.0, 4.0),
    base_stake_usd: float = 10.0,
    min_body: float = 75.0,
    entry_price: float = 0.48,
    strategy_name: str = "Special Martingale Pattern C",
) -> dict:
    """
    Simulates Martingale strategy using strict forward execution across chronological beats.
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
    step_wins = [0] * cap
    filter_skips = 0
    total_trades = 0

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
            action = pattern[step]
            stake = stakes[step]

            if action == "FOLLOW":
                bet_side = trigger_dir
            elif action == "FADE":
                bet_side = "Down" if trigger_dir == "Up" else "Up"

            fill_px = entry_price
            shares = stake / fill_px
            total_trades += 1

            if actual == bet_side:
                # WON STEP
                camp_wins += 1
                step_wins[step] += 1
                profit = (stake / fill_px) * (1.0 - fill_px)
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
            dd = peak - pnl
            if dd > max_dd:
                max_dd = dd

    total_camps = camp_wins + camp_losses
    camp_wr = (camp_wins / total_camps * 100.0) if total_camps > 0 else 0.0
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
        "campaigns": total_camps,
        "camp_wins": camp_wins,
        "camp_losses": camp_losses,
        "camp_wr": camp_wr,
        "total_trades": total_trades,
        "step_wins": step_wins,
        "filter_skips": filter_skips,
        "net_pnl": pnl,
        "max_drawdown": max_dd,
        "ret_dd_ratio": ret_dd,
    }


def print_single_summary(r: dict):
    print("=" * 85)
    print(f"  SIMULATION RESULTS: {r['strategy_name']}")
    print("=" * 85)
    print(f"  Trigger Streak         : {r['consecutive_streak_count']} consecutive candles")
    print(f"  Action Pattern         : {r['pattern']} (Steps 1-{len(r['step_wins'])})")
    print(f"  Stake Multipliers      : {r['multipliers']} (Base: ${r['base_stake_usd']:.2f})")
    print(f"  Candle Body Filter     : Body >= ${r['min_body']:.2f}")
    print(f"  Limit Entry Price      : ${r['entry_price']:.2f}")
    print(f"  Total Analyzed Beats   : {r['total_beats']:,}")
    print("-" * 85)
    print(f"  Completed Campaigns    : {r['campaigns']} (Filter Skips: {r['filter_skips']})")
    print(f"  Campaign Win Rate      : {r['camp_wr']:.2f}% ({r['camp_wins']} Won / {r['camp_losses']} Lost)")
    
    step_str = " | ".join([f"Step {idx+1}: {cnt} ({cnt/r['campaigns']*100:.1f}%)" for idx, cnt in enumerate(r['step_wins'])]) if r['campaigns'] > 0 else ""
    print(f"  Step Wins Breakdown    : {step_str}")
    print(f"  Hard Cap Stop Losses   : {r['camp_losses']} campaigns (-${sum(r['multipliers'])*r['base_stake_usd']:.2f} each)")
    print("-" * 85)
    print(f"  Net Profit / Loss      : ${r['net_pnl']:+,.2f}")
    print(f"  Maximum Drawdown       : ${r['max_drawdown']:,.2f}")
    print(f"  Return / Drawdown Ratio: {r['ret_dd_ratio']:.2f}x")
    print("=" * 85)


def print_comparison_table(results: list[dict]):
    print("\n" + "=" * 130)
    print("REAL DATA COMPARISON MATRIX (FULL 56-DAY CHRONOLOGICAL REPLAY)")
    print("=" * 130)
    print(f"{'STRATEGY NAME':<38} | {'PX':<5} | {'FILTER':<10} | {'CAMPS':<6} | {'C_WR':<8} | {'STEP WINS (1/2/3)':<18} | {'NET PNL':<12} | {'MAX DD':<10} | {'RET/DD':<7}")
    print("-" * 130)
    for r in results:
        step_str = f"{r['step_wins'][0]}/{r['step_wins'][1]}/{r['step_wins'][2]}"
        filter_str = f">=${int(r['min_body'])}" if r['min_body'] > 0 else "None"
        print(f"{r['strategy_name']:<38} | ${r['entry_price']:.2f} | {filter_str:<10} | {r['campaigns']:<6} | {r['camp_wr']:>5.1f}  % | {step_str:<18} | ${r['net_pnl']:>+10.2f} | ${r['max_drawdown']:>8.2f} | {r['ret_dd_ratio']:>5.2f} x")
    print("=" * 130 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Kalshi Special Martingale Simulation")
    parser.add_argument("--config", type=str, default=os.path.join(SCRIPT_DIR, "config.json"), help="Path to config JSON file")
    parser.add_argument("--data-dir", type=str, default=DEFAULT_DATA_DIR, help="Path to BTC market dataset")
    parser.add_argument("--compare", action="store_true", help="Run multi-price comparison suite")
    parser.add_argument("--base-stake", type=float, default=None, help="Override base stake USD")
    parser.add_argument("--entry-price", type=float, default=None, help="Override entry price (e.g. 0.48, 0.50, 0.52)")
    parser.add_argument("--min-body", type=float, default=None, help="Override candle body threshold")

    args = parser.parse_args()

    print(f"Loading market dataset from {args.data_dir}...")
    outcomes, bodies, open_times, slug_order, markets = load_dataset(args.data_dir)
    print(f"Loaded {len(outcomes):,} chronological beats ({open_times[0]} to {open_times[-1]}).")

    if args.compare:
        min_body = args.min_body if args.min_body is not None else 75.0
        base_stake = args.base_stake if args.base_stake is not None else 10.0
        prices = [0.45, 0.48, 0.50, 0.52]

        results = []
        for p in prices:
            mode_lbl = "Taker" if p > 0.50 else ("Fair Mid" if p == 0.50 else "Maker")
            name = f"Pattern C ({mode_lbl} ${p:.2f})"
            r = simulate_martingale(
                outcomes=outcomes,
                bodies=bodies,
                slug_order=slug_order,
                consecutive_streak_count=2,
                pattern=("FADE", "FOLLOW", "FADE"),
                multipliers=(1.0, 2.0, 4.0),
                base_stake_usd=base_stake,
                min_body=min_body,
                entry_price=p,
                strategy_name=name,
            )
            results.append(r)

        print_comparison_table(results)

    else:
        cfg = {}
        if os.path.exists(args.config):
            with open(args.config, "r", encoding="utf-8") as f:
                cfg = json.load(f)

        strategy_name = cfg.get("strategy_name", "Special Martingale Pattern C")
        streak_count = cfg.get("consecutive_streak_count", 2)
        base_stake = args.base_stake if args.base_stake is not None else cfg.get("base_stake_usd", 10.0)
        multipliers = tuple(cfg.get("stake_multipliers", [1.0, 2.0, 4.0]))
        price = args.entry_price if args.entry_price is not None else cfg.get("max_maker_price", 0.48)

        delta_cfg = cfg.get("delta_filter", {})
        min_body = 75.0
        if delta_cfg.get("enabled", False):
            min_body = float(delta_cfg.get("delta_threshold", 75.0) or 75.0)
        if args.min_body is not None:
            min_body = args.min_body

        pattern = ("FADE", "FOLLOW", "FADE")

        res = simulate_martingale(
            outcomes=outcomes,
            bodies=bodies,
            slug_order=slug_order,
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
