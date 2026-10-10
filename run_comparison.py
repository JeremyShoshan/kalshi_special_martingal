import os
import sys
import glob
import csv
import json
import argparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_DIR = r"c:\Users\Jeremy\Documents\projects\collect_data\combined_dataset\BTC"
DEFAULT_OB_CACHE = os.path.join(SCRIPT_DIR, "orderbook_fill_cache.json")


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
    ob_cache: dict | None = None,
    consecutive_streak_count: int = 2,
    pattern: tuple[str, ...] = ("FADE", "FOLLOW", "FADE"),
    multipliers: tuple[float, ...] = (1.0, 2.0, 4.0),
    base_stake_usd: float = 10.0,
    min_body: float = 0.0,
    entry_price: float = 0.48,
    stop_maker_sec: int = 240,
    strategy_name: str = "Special Martingale Pattern C",
) -> dict:
    """
    Simulates Martingale strategy using strict forward execution AND real orderbook depth:
    - If ob_cache is provided, verifies if each order realistically filled as Taker or Maker
      within stop_maker_sec.
    - If unfilled, $0 is risked, no capital lost, order is cancelled.
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
    filled_trades = 0
    unfilled_orders = 0
    maker_fills = 0
    taker_fills = 0
    step_wins = [0] * cap
    filter_skips = 0

    total_taker_fees = 0.0

    for i in range(consecutive_streak_count, N):
        slug = slug_order[i]
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

            # Check Real Orderbook Fill
            filled = True
            fill_px = entry_price
            is_taker = False

            if ob_cache:
                ob = ob_cache.get(slug)
                if ob:
                    open_ask = ob["open_up_ask"] if bet_side == "Up" else ob["open_down_ask"]
                    min_bid = ob["min_up_bid"] if bet_side == "Up" else ob["min_down_bid"]

                    # 1. Marketable Taker: ask is at or below our limit price
                    if open_ask <= entry_price:
                        filled = True
                        fill_px = open_ask
                        is_taker = True
                    # 2. Resting Maker: ask > entry_price, but market trades down to our bid within 240s
                    elif min_bid <= entry_price:
                        filled = True
                        fill_px = entry_price
                        is_taker = False
                    # 3. Timeout Unfilled: price never reached our limit price
                    else:
                        filled = False
                else:
                    # Missing from orderbook -> cannot confirm fill -> treat as unfilled
                    filled = False

            if not filled:
                unfilled_orders += 1
                # If Step 0 was unfilled and market reversed, clean reset!
                if step == 0 and actual != trigger_dir:
                    in_camp = False
                    step = 0
                    trigger_dir = None
                # If streak continued or Step 1/2 was unfilled, preserve current step
                continue

            # Order Filled!
            filled_trades += 1
            if is_taker:
                taker_fills += 1
            else:
                maker_fills += 1

            shares = stake / fill_px
            taker_fee = (shares * 0.07 * fill_px * (1.0 - fill_px)) if is_taker else 0.0
            total_taker_fees += taker_fee

            if actual == bet_side:
                # WON STEP
                camp_wins += 1
                step_wins[step] += 1
                gross_profit = shares * (1.0 - fill_px)
                net_profit = gross_profit - taker_fee
                pnl += net_profit
                in_camp = False
                step = 0
                trigger_dir = None
            else:
                # LOST STEP
                pnl -= (stake + taker_fee)
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

    tot_camps = camp_wins + camp_losses
    c_wr = (camp_wins / tot_camps * 100.0) if tot_camps > 0 else 0.0
    tot_orders = filled_trades + unfilled_orders
    fill_rate = (filled_trades / tot_orders * 100.0) if tot_orders > 0 else 0.0
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
        "filled_trades": filled_trades,
        "unfilled_orders": unfilled_orders,
        "fill_rate": fill_rate,
        "maker_fills": maker_fills,
        "taker_fills": taker_fills,
        "taker_fees": total_taker_fees,
        "step_wins": step_wins,
        "filter_skips": filter_skips,
        "net_pnl": pnl,
        "max_drawdown": max_dd,
        "ret_dd_ratio": ret_dd,
        "used_orderbook": bool(ob_cache),
    }


def print_single_summary(r: dict):
    print("\n" + "=" * 85)
    print(f"  SIMULATION RESULTS: {r['strategy_name']}")
    print("=" * 85)
    print(f"  Data Mode              : {'REAL ORDERBOOK TICK REPLAY' if r['used_orderbook'] else 'FLAT FILL ASSUMPTION'}")
    print(f"  Trigger Streak         : {r['consecutive_streak_count']} consecutive candles")
    print(f"  Action Pattern         : {r['pattern']} (Steps 1-{len(r['step_wins'])})")
    print(f"  Stake Multipliers      : {r['multipliers']} (Base: ${r['base_stake_usd']:.2f})")
    print(f"  Candle Body Filter     : Body >= ${r['min_body']:.2f}" if r['min_body'] > 0 else "  Candle Body Filter     : None")
    print(f"  Limit Entry Price      : ${r['entry_price']:.2f}")
    print(f"  Total Analyzed Beats   : {r['total_beats']:,}")
    print("-" * 85)
    print(f"  Orderbook Fill Rate    : {r['fill_rate']:.1f}% ({r['filled_trades']:,} Filled / {r['unfilled_orders']:,} Unfilled Timeouts)")
    print(f"  Execution Breakdown    : Maker: {r['maker_fills']:,} ($0 fee) | Taker: {r['taker_fills']:,} (Fees: ${r['taker_fees']:.2f})")
    print("-" * 85)
    print(f"  Completed Campaigns    : {r['campaigns']:,} (Filter Skips: {r['filter_skips']:,})")
    print(f"  Campaign Win Rate      : {r['camp_wr']:.2f}% ({r['camp_wins']:,} Won / {r['camp_losses']:,} Lost)")
    
    step_str_parts = []
    for s_idx, s_wins in enumerate(r['step_wins']):
        pct = (s_wins / r['campaigns'] * 100.0) if r['campaigns'] > 0 else 0.0
        step_str_parts.append(f"Step {s_idx+1}: {s_wins:,} ({pct:.1f}%)")
    print(f"  Step Wins Breakdown    : {' | '.join(step_str_parts)}")
    print(f"  Hard Cap Stop Losses   : {r['camp_losses']:,} campaigns (-${r['base_stake_usd'] * sum(r['multipliers']):.2f} each)")
    print("-" * 85)
    print(f"  Net Profit / Loss      : ${r['net_pnl']:+,.2f}")
    print(f"  Maximum Drawdown       : ${r['max_drawdown']:,.2f}")
    print(f"  Return / Drawdown Ratio: {r['ret_dd_ratio']:.2f}x")
    print("=" * 85 + "\n")


def print_comparison_table(results: list[dict]):
    print("\n" + "=" * 145)
    print("REAL ORDERBOOK TICK REPLAY COMPARISON MATRIX")
    print("=" * 145)
    header = (
        f"{'STRATEGY NAME':<38} | {'PX':<5} | {'FILL%':<6} | {'FILLED':<7} | {'UNFILLED':<8} | "
        f"{'CAMPS':<6} | {'C_WR':<7} | {'NET PNL':<12} | {'MAX DD':<9} | {'RET/DD':<7}"
    )
    print(header)
    print("-" * 145)
    for r in results:
        f_str = f"Body>={int(r['min_body'])}" if r['min_body'] > 0 else "None"
        row = (
            f"{r['strategy_name'][:38]:<38} | "
            f"${r['entry_price']:.2f} | "
            f"{r['fill_rate']:5.1f}% | "
            f"{r['filled_trades']:<7} | "
            f"{r['unfilled_orders']:<8} | "
            f"{r['campaigns']:<6} | "
            f"{r['camp_wr']:<6.1f}% | "
            f"${r['net_pnl']:<+11.2f} | "
            f"${r['max_drawdown']:<8.2f} | "
            f"{r['ret_dd_ratio']:<6.2f}x"
        )
        print(row)
    print("=" * 145 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Kalshi Special Martingale Simulation on Real Orderbook Data")
    parser.add_argument("--config", type=str, default=os.path.join(SCRIPT_DIR, "config.json"), help="Path to config JSON file")
    parser.add_argument("--data-dir", type=str, default=DEFAULT_DATA_DIR, help="Path to BTC market dataset")
    parser.add_argument("--ob-cache", type=str, default=DEFAULT_OB_CACHE, help="Path to orderbook fill cache JSON")
    parser.add_argument("--compare", action="store_true", help="Run multi-strategy comparison suite")
    parser.add_argument("--base-stake", type=float, default=None, help="Override base stake USD")
    parser.add_argument("--entry-price", type=float, default=None, help="Override maker entry price (e.g. 0.20, 0.40, 0.45, 0.48)")
    parser.add_argument("--min-body", type=float, default=None, help="Override Binance candle body threshold")

    args = parser.parse_args()

    print(f"Loading market dataset from {args.data_dir}...")
    outcomes, bodies, open_times, slug_order, markets = load_dataset(args.data_dir)
    print(f"Loaded {len(outcomes):,} chronological beats ({open_times[0]} to {open_times[-1]}).")

    ob_cache = None
    if os.path.exists(args.ob_cache):
        with open(args.ob_cache, "r", encoding="utf-8") as f:
            ob_cache = json.load(f)
        print(f"Loaded real orderbook tick depth cache for {len(ob_cache):,} beats.")
    else:
        print("[WARNING] orderbook_fill_cache.json not found! Running in flat fill mode.")

    if args.compare:
        prices_to_compare = [0.20, 0.40, 0.45, 0.48, 0.50, 0.52]
        base_stake = args.base_stake if args.base_stake is not None else 10.0
        min_body = args.min_body if args.min_body is not None else 75.0

        results = []
        for p in prices_to_compare:
            name = f"Pattern C (Body>={int(min_body)}, Limit ${p:.2f})"
            r = simulate_martingale(
                outcomes=outcomes,
                bodies=bodies,
                slug_order=slug_order,
                ob_cache=ob_cache,
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
        # Load single config
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
        min_body = 0.0
        if delta_cfg.get("enabled", False):
            min_body = float(delta_cfg.get("delta_threshold", 75.0) or 0.0)
        if args.min_body is not None:
            min_body = args.min_body

        pattern = ("FADE", "FOLLOW", "FADE")

        res = simulate_martingale(
            outcomes=outcomes,
            bodies=bodies,
            slug_order=slug_order,
            ob_cache=ob_cache,
            consecutive_streak_count=streak_count,
            pattern=pattern,
            multipliers=multipliers,
            base_stake_usd=base_stake,
            min_body=min_body,
            entry_price=price,
            strategy_name=f"{strategy_name} (${price:.2f})",
        )

        print_single_summary(res)


if __name__ == "__main__":
    main()
