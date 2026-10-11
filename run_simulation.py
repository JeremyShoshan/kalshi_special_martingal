"""
Kalshi Special Martingale (Pattern C: FADE-FOLLOW-FADE) - REAL ORDER BOOK REPLAY SIMULATION.

Every order is executed against the recorded Kalshi order book + trade tape
(collect_data/combined_dataset/BTC_250) exactly like bot_live.py does it in marketable-limit mode:

  * GTC limit BUY at `max_maker_price` sent at beat start (+latency).
  * Best ask <= limit  -> immediate TAKER fill at the ask (+ Kalshi taker fee).
  * Otherwise rests as MAKER; fills only when the tape shows sellers at/through our price.
  * Unfilled remainder cancelled at `stop_maker_sec` (optional IOC fallback at `taker_limit_price`).
  * Partial fills are real positions; unfilled orders follow the bot's skip rules
    (Step 0 unfilled + streak broke -> reset, otherwise keep the same step).
  * Trigger side of the just-closed beat can be taken from the bot's "decisive close" logic on the
    order book (what the live bot really sees 15s before close) or from the settled result.

Usage:
  uv run python run_simulation.py                       # config.json, strict fills
  uv run python run_simulation.py --compare             # fill-model x price matrix
  uv run python run_simulation.py --fill-model queue --queue-ahead 200
  uv run python run_simulation.py --trades-out logs/sim_trades.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from lib.orderbook_replay import (  # noqa: E402
    BEAT_SEC,
    Beat,
    ExecConfig,
    TapeStore,
    decided_close_side,
    load_markets,
    simulate_order,
)

DEFAULT_DATA_DIR = r"c:\Users\Jeremy\Documents\projects\collect_data\combined_dataset\BTC_250"
DEFAULT_CACHE_DIR = os.path.join(SCRIPT_DIR, ".sim_cache", "BTC_250")
PATTERN = ("FADE", "FOLLOW", "FADE")


def opposite(side: str) -> str:
    return "Down" if side == "Up" else "Up"


def get_streak(outcomes: list[str]) -> tuple[int, str | None]:
    if not outcomes:
        return 0, None
    side = outcomes[-1]
    n = 0
    for o in reversed(outcomes):
        if o != side:
            break
        n += 1
    return n, side


@dataclass
class StrategyParams:
    name: str = "Special Martingale Pattern C"
    consecutive_streak_count: int = 2
    multipliers: tuple[float, ...] = (1.0, 2.0, 4.0)
    base_stake_usd: float = 10.0
    min_body: float = 75.0  # 0 disables the candle body filter
    pattern: tuple[str, ...] = PATTERN


@dataclass
class CampaignSimulator:
    """Stateful forward simulator; feed beats in chronological order through `on_beat`."""
    params: StrategyParams
    exec_cfg: ExecConfig
    label: str = ""

    in_camp: bool = False
    step: int = 0
    trigger_dir: str | None = None
    camp_pnl: float = 0.0

    pnl: float = 0.0
    peak: float = 0.0
    max_dd: float = 0.0
    fees: float = 0.0
    camp_wins: int = 0
    camp_losses: int = 0
    step_wins: list[int] = field(default_factory=list)
    filter_skips: int = 0
    orders: int = 0
    filled: int = 0
    partial: int = 0
    unfilled: int = 0
    no_data: int = 0
    taker_orders: int = 0
    maker_orders: int = 0
    trade_wins: int = 0
    trade_losses: int = 0
    shares_total: float = 0.0
    cost_total: float = 0.0
    stake_requested: float = 0.0
    gap_resets: int = 0
    unfilled_by_step: list[int] = field(default_factory=list)
    fill_secs: list[float] = field(default_factory=list)
    daily_pnl: dict = field(default_factory=lambda: defaultdict(float))
    log: list[dict] = field(default_factory=list)

    def __post_init__(self):
        cap = len(self.params.pattern)
        self.step_wins = [0] * cap
        self.unfilled_by_step = [0] * cap
        self.stakes = [self.params.base_stake_usd * m for m in self.params.multipliers]

    # ------------------------------------------------------------------
    def reset_campaign(self):
        self.in_camp = False
        self.step = 0
        self.trigger_dir = None
        self.camp_pnl = 0.0

    def on_gap(self):
        if self.in_camp:
            self.gap_resets += 1
        self.reset_campaign()

    def on_beat(self, beat: Beat, prev_beat: Beat, observed_hist: list[str], tape):
        """
        Called for every beat `beat` (the beat the bot would trade) with:
          observed_hist : outcomes the bot "sees" at the decision window of prev_beat
                          (settled history + decided side of prev_beat), chronological.
        """
        p = self.params
        cap = len(p.pattern)
        streak_len, streak_side = get_streak(observed_hist)

        if not self.in_camp:
            if streak_len == p.consecutive_streak_count and streak_side is not None:
                if p.min_body > 0 and prev_beat.body < p.min_body:
                    self.filter_skips += 1
                    return
                self.in_camp = True
                self.step = 0
                self.trigger_dir = streak_side
                self.camp_pnl = 0.0
            else:
                return

        action = p.pattern[self.step]
        stake = self.stakes[self.step]
        bet_side = self.trigger_dir if action == "FOLLOW" else opposite(self.trigger_dir)

        fill = simulate_order(beat, tape, bet_side, stake, self.exec_cfg)
        self.orders += 1
        self.stake_requested += stake
        actual = beat.outcome
        row = {
            "beat": beat.slug, "date": beat.date, "step": self.step + 1, "action": action,
            "side": bet_side, "stake": round(stake, 2), "status": fill.status,
            "open_ask": fill.open_ask, "shares": round(fill.shares, 4),
            "avg_price": round(fill.avg_price, 4), "fees": round(fill.fees, 2),
            "source": fill.source, "fill_sec": fill.first_fill_sec, "outcome": actual,
            "pnl": 0.0, "cum_pnl": 0.0,
        }

        if fill.shares <= 1e-9:
            if fill.status == "NO_DATA":
                self.no_data += 1
            else:
                self.unfilled += 1
            self.unfilled_by_step[self.step] += 1
            # bot_live skip rule: Step 0 unfilled and streak broke -> clean reset, else keep step
            if self.step == 0 and actual != self.trigger_dir:
                self.reset_campaign()
            row["cum_pnl"] = round(self.pnl, 2)
            self.log.append(row)
            return

        if fill.status == "PARTIAL":
            self.partial += 1
        else:
            self.filled += 1
        if fill.taker_shares > 0:
            self.taker_orders += 1
        if fill.maker_shares > 0:
            self.maker_orders += 1
        if fill.first_fill_sec is not None:
            self.fill_secs.append(fill.first_fill_sec)
        self.shares_total += fill.shares
        self.cost_total += fill.cost
        self.fees += fill.fees

        won = actual == bet_side
        trade_pnl = (fill.shares if won else 0.0) - fill.cost - fill.fees
        self.pnl += trade_pnl
        self.camp_pnl += trade_pnl
        self.daily_pnl[beat.date] += trade_pnl
        if won:
            self.trade_wins += 1
            self.camp_wins += 1
            self.step_wins[self.step] += 1
            self.reset_campaign()
        else:
            self.trade_losses += 1
            self.step += 1
            if self.step >= cap:
                self.camp_losses += 1
                self.reset_campaign()

        self.peak = max(self.peak, self.pnl)
        self.max_dd = max(self.max_dd, self.peak - self.pnl)
        row["pnl"] = round(trade_pnl, 2)
        row["cum_pnl"] = round(self.pnl, 2)
        self.log.append(row)

    # ------------------------------------------------------------------
    def summary(self) -> dict:
        camps = self.camp_wins + self.camp_losses
        traded = self.filled + self.partial
        days = len(self.daily_pnl)
        return {
            "label": self.label,
            "fill_model": self.exec_cfg.fill_model,
            "limit": self.exec_cfg.limit_price,
            "orders": self.orders,
            "filled": self.filled,
            "partial": self.partial,
            "unfilled": self.unfilled,
            "no_data": self.no_data,
            "fill_rate": (traded / (self.orders - self.no_data) * 100.0) if self.orders - self.no_data > 0 else 0.0,
            "stake_fill_pct": (self.cost_total / self.stake_requested * 100.0) if self.stake_requested else 0.0,
            "taker_orders": self.taker_orders,
            "maker_orders": self.maker_orders,
            "avg_price": (self.cost_total / self.shares_total) if self.shares_total else 0.0,
            "avg_fill_sec": (sum(self.fill_secs) / len(self.fill_secs)) if self.fill_secs else 0.0,
            "fees": self.fees,
            "trade_wins": self.trade_wins,
            "trade_losses": self.trade_losses,
            "campaigns": camps,
            "camp_wins": self.camp_wins,
            "camp_losses": self.camp_losses,
            "camp_wr": (self.camp_wins / camps * 100.0) if camps else 0.0,
            "step_wins": self.step_wins,
            "unfilled_by_step": self.unfilled_by_step,
            "filter_skips": self.filter_skips,
            "gap_resets": self.gap_resets,
            "net_pnl": self.pnl,
            "max_dd": self.max_dd,
            "ret_dd": (self.pnl / self.max_dd) if self.max_dd > 0 else 0.0,
            "trading_days": days,
            "losing_days": sum(1 for v in self.daily_pnl.values() if v < 0),
        }


# ───────────────────────────────────────────────────────────────────────────────
def run_replay(beats: list[Beat], store: TapeStore, sims: list[CampaignSimulator],
               decision_mode: str = "orderbook", seconds_before_close: int = 15,
               decisive_up: float = 0.90, decisive_down: float = 0.10,
               history_len: int = 50) -> dict:
    """Single chronological pass feeding every simulator. Returns decision stats."""
    stats = {"decisions": 0, "mismatch": 0, "no_close_book": 0, "gaps": 0}
    settled: list[str] = []  # contiguous settled history
    prev: Beat | None = None
    for beat in beats:
        if prev is not None and abs(beat.start_ts - prev.start_ts - BEAT_SEC) > 1:
            # missing beats in the dataset -> streak history unknown, campaigns cannot continue
            stats["gaps"] += 1
            settled = []
            for s in sims:
                s.on_gap()
            prev = None
        if prev is not None:
            if decision_mode == "orderbook":
                decided, has_book = decided_close_side(prev, store.tape(prev), seconds_before_close,
                                                       decisive_up, decisive_down)
                if not has_book:
                    stats["no_close_book"] += 1
            else:
                decided = prev.outcome
            stats["decisions"] += 1
            if decided != prev.outcome:
                stats["mismatch"] += 1
            observed = settled[-(history_len - 1):] + [decided]
            tape = store.tape(beat)
            for s in sims:
                s.on_beat(beat, prev, observed, tape)
            settled.append(prev.outcome)
        prev = beat
    return stats


# ───────────────────────────────────────────────────────────────────────────────
def print_summary(r: dict, p: StrategyParams, cfg: ExecConfig, dstats: dict, beats: list[Beat], decision_mode: str):
    W = 92
    print("=" * W)
    print(f"  REAL ORDER BOOK REPLAY: {p.name}")
    print("=" * W)
    print(f"  Data                   : {beats[0].date} -> {beats[-1].date} | {len(beats):,} beats | {dstats['gaps']} data gaps")
    print(f"  Trigger / Pattern      : {p.consecutive_streak_count} consecutive | {'-'.join(p.pattern)} | x{list(p.multipliers)} of ${p.base_stake_usd:.2f}")
    print(f"  Body Filter            : {'>= $%.2f (prev beat |close-open|, Kalshi BRTI)' % p.min_body if p.min_body > 0 else 'off'}")
    print(f"  Execution              : marketable limit ${cfg.limit_price:.2f} | entry at {cfg.place_latency_sec:.0f}s | cancel at {cfg.stop_maker_sec:.0f}s"
          f" | IOC fallback {cfg.taker_limit_price if cfg.taker_limit_price else 'off'}")
    qa = f" (queue ahead {cfg.queue_ahead:g})" if cfg.fill_model == "queue" else ""
    print(f"  Maker Fill Model       : {cfg.fill_model}{qa} | book-cross fills {'on' if cfg.book_cross_fills else 'off'}")
    print(f"  Fees                   : taker {cfg.taker_fee_rate:g} | maker {cfg.maker_fee_rate:g}  (x C x P x (1-P), ceil to cent)")
    print(f"  Decision Source        : {decision_mode} | {dstats['mismatch']} of {dstats['decisions']:,} decided closes differ from settlement"
          + (f" | {dstats['no_close_book']} w/o close book (settled used)" if decision_mode == 'orderbook' else ""))
    print("-" * W)
    print(f"  Orders Sent            : {r['orders']:,}  -> Filled {r['filled']:,} | Partial {r['partial']:,} | Unfilled {r['unfilled']:,} | No data {r['no_data']:,}")
    print(f"  Fill Rate              : {r['fill_rate']:.1f}% of orders | {r['stake_fill_pct']:.1f}% of requested stake deployed")
    print(f"  Execution Mix          : Taker {r['taker_orders']:,} | Maker {r['maker_orders']:,} | Avg price ${r['avg_price']:.4f} | Avg time-to-fill {r['avg_fill_sec']:.1f}s")
    print(f"  Unfilled by Step       : {' | '.join(f'S{i+1}: {n}' for i, n in enumerate(r['unfilled_by_step']))}")
    print(f"  Fees Paid              : ${r['fees']:.2f}")
    print("-" * W)
    print(f"  Campaigns              : {r['campaigns']:,} (Filter skips {r['filter_skips']:,} | Gap resets {r['gap_resets']})")
    print(f"  Campaign Win Rate      : {r['camp_wr']:.2f}% ({r['camp_wins']} won / {r['camp_losses']} lost)")
    if r["campaigns"]:
        print(f"  Step Wins              : {' | '.join(f'S{i+1}: {n} ({n / r['campaigns'] * 100:.1f}%)' for i, n in enumerate(r['step_wins']))}")
    print(f"  Trades                 : {r['trade_wins']} won / {r['trade_losses']} lost")
    print("-" * W)
    print(f"  Net PnL (after fees)   : ${r['net_pnl']:+,.2f}")
    print(f"  Max Drawdown           : ${r['max_dd']:,.2f}")
    print(f"  Return / Drawdown      : {r['ret_dd']:.2f}x")
    print(f"  Losing Days            : {r['losing_days']} of {r['trading_days']} trading days")
    print("=" * W)


def print_matrix(results: list[dict], title: str):
    W = 150
    print("\n" + "=" * W)
    print(title)
    print("=" * W)
    print(f"{'FILL MODEL':<14} | {'LIMIT':<5} | {'ORDERS':>6} | {'FILL%':>6} | {'STAKE%':>6} | {'TAKER':>5} | {'MAKER':>5} | {'AVG PX':>6} | "
          f"{'CAMPS':>5} | {'C_WR':>6} | {'STEPS 1/2/3':<12} | {'FEES':>7} | {'NET PNL':>10} | {'MAX DD':>8} | {'RET/DD':>6}")
    print("-" * W)
    for r in results:
        steps = "/".join(str(x) for x in r["step_wins"])
        print(f"{r['label']:<14} | ${r['limit']:.2f} | {r['orders']:>6} | {r['fill_rate']:>5.1f}% | {r['stake_fill_pct']:>5.1f}% | {r['taker_orders']:>5} | {r['maker_orders']:>5} | "
              f"{r['avg_price']:>6.4f} | {r['campaigns']:>5} | {r['camp_wr']:>5.1f}% | {steps:<12} | ${r['fees']:>6.2f} | ${r['net_pnl']:>+9.2f} | ${r['max_dd']:>7.2f} | {r['ret_dd']:>5.2f}x")
    print("=" * W + "\n")


def write_trades(path: str, log: list[dict]):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if not log:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(log[0].keys()))
        w.writeheader()
        w.writerows(log)


# ───────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Kalshi Special Martingale - real order book replay simulation")
    ap.add_argument("--config", default=os.path.join(SCRIPT_DIR, "config.json"))
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ap.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR, help="Per-day parsed tape cache ('' to disable)")
    ap.add_argument("--start-date", default=None, help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--end-date", default=None, help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--compare", action="store_true", help="Run fill-model x limit-price matrix")
    ap.add_argument("--prices", default="0.45,0.48,0.50,0.52", help="Limit prices for --compare")
    ap.add_argument("--base-stake", type=float, default=None)
    ap.add_argument("--entry-price", type=float, default=None, help="Override max_maker_price (limit)")
    ap.add_argument("--min-body", type=float, default=None, help="Override body filter (0 = off)")
    ap.add_argument("--fill-model", choices=["strict", "queue", "touch", "ideal"], default="strict")
    ap.add_argument("--queue-ahead", type=float, default=100.0, help="Contracts ahead of us at our price (queue model)")
    ap.add_argument("--no-book-cross", action="store_true", help="Ignore resting asks <= limit as fill proof")
    ap.add_argument("--latency", type=float, default=1.0, help="Seconds after beat start the order reaches Kalshi")
    ap.add_argument("--taker-fee", type=float, default=0.07)
    ap.add_argument("--maker-fee", type=float, default=0.0, help="Kalshi maker fee rate (e.g. 0.0175 if charged)")
    ap.add_argument("--decision", choices=["orderbook", "result"], default="orderbook",
                    help="Side of the just-closed beat: bot's decisive-close logic on the book, or settled result")
    ap.add_argument("--trades-out", default=None, help="Write per-order log CSV")
    ap.add_argument("--pattern", default=None, help="Step actions, e.g. FADE,FADE,FOLLOW (overrides config 'pattern')")
    ap.add_argument("--streak", type=int, default=None, help="Override consecutive_streak_count")
    ap.add_argument("--multipliers", default=None, help="e.g. 1,2,3 (overrides stake_multipliers)")
    ap.add_argument("--min-maker-sec", type=float, default=None, help="Entry delay after beat start (overrides config)")
    ap.add_argument("--stop-maker-sec", type=float, default=None, help="Maker cancel time (overrides config)")
    args = ap.parse_args()

    cfg = {}
    if os.path.exists(args.config):
        with open(args.config, "r", encoding="utf-8") as f:
            cfg = json.load(f)

    delta_cfg = cfg.get("delta_filter", {}) or {}
    min_body = float(delta_cfg.get("delta_threshold") or 0.0) if delta_cfg.get("enabled", False) else 0.0
    if args.min_body is not None:
        min_body = args.min_body

    params = StrategyParams(
        name=cfg.get("strategy_name", "Special Martingale Pattern C"),
        consecutive_streak_count=args.streak if args.streak is not None else int(cfg.get("consecutive_streak_count", 2)),
        multipliers=tuple(float(x) for x in args.multipliers.split(",")) if args.multipliers
        else tuple(cfg.get("stake_multipliers", [1.0, 2.0, 4.0])),
        base_stake_usd=args.base_stake if args.base_stake is not None else float(cfg.get("base_stake_usd", 10.0)),
        min_body=min_body,
    )
    pattern_cfg = args.pattern.split(",") if args.pattern else cfg.get("pattern")
    if pattern_cfg:
        params.pattern = tuple(str(a).strip().upper() for a in pattern_cfg)
        bad = [a for a in params.pattern if a not in ("FADE", "FOLLOW")]
        if bad:
            raise SystemExit(f"Unsupported pattern actions {bad}; use FADE / FOLLOW")
        cap = len(params.pattern)
    else:
        cap = int(cfg.get("max_cap", len(params.multipliers)))
        params.pattern = PATTERN[:cap] if cap <= len(PATTERN) else PATTERN + ("FADE",) * (cap - len(PATTERN))
    if len(params.multipliers) < cap:
        params.multipliers = tuple(list(params.multipliers) + [2.0 ** i for i in range(len(params.multipliers), cap)])

    limit = args.entry_price if args.entry_price is not None else float(cfg.get("max_maker_price", 0.48))
    stop_maker = args.stop_maker_sec if args.stop_maker_sec is not None else cfg.get("stop_maker_sec", 240)
    min_maker = args.min_maker_sec if args.min_maker_sec is not None else float(cfg.get("min_maker_sec", 0) or 0)
    base_exec = dict(
        stop_maker_sec=float(stop_maker if stop_maker is not None else 240),
        place_latency_sec=min_maker + args.latency,
        queue_ahead=args.queue_ahead,
        book_cross_fills=not args.no_book_cross,
        taker_fee_rate=args.taker_fee,
        maker_fee_rate=args.maker_fee,
        taker_limit_price=cfg.get("taker_limit_price"),
    )

    print(f"Loading markets from {args.data_dir} ...")
    beats = load_markets(args.data_dir, args.start_date, args.end_date)
    print(f"Loaded {len(beats):,} settled beats ({beats[0].date} -> {beats[-1].date}).")
    if not cfg.get("marketable_limit", True):
        print("[NOTE] config has marketable_limit=false; the replay models the marketable-limit path only.")
    store = TapeStore(args.data_dir, args.cache_dir or None)

    sims: list[CampaignSimulator] = []
    if args.compare:
        prices = [float(x) for x in args.prices.split(",") if x.strip()]
        for model in ("ideal", "touch", "queue", "strict"):
            for px in prices:
                ec = ExecConfig(limit_price=px, fill_model=model, **base_exec)
                lbl = f"queue({args.queue_ahead:g})" if model == "queue" else model
                sims.append(CampaignSimulator(params, ec, label=lbl))
    else:
        ec = ExecConfig(limit_price=limit, fill_model=args.fill_model, **base_exec)
        sims.append(CampaignSimulator(params, ec, label=args.fill_model))

    dstats = run_replay(
        beats, store, sims,
        decision_mode=args.decision,
        seconds_before_close=int(cfg.get("seconds_before_close", 15)),
        decisive_up=float(cfg.get("decisive_up", 0.90)),
        decisive_down=float(cfg.get("decisive_down", 0.10)),
        history_len=int(cfg.get("history_beats_count", 50)),
    )

    if args.compare:
        body = f"Body>=${params.min_body:g}" if params.min_body > 0 else "no body filter"
        print_matrix([s.summary() for s in sims],
                     f"REAL ORDER BOOK REPLAY MATRIX | {beats[0].date} -> {beats[-1].date} | {body} | decision={args.decision} | "
                     f"maker window {base_exec['stop_maker_sec']:.0f}s | ideal = legacy 100%-fill assumption")
    else:
        s = sims[0]
        print_summary(s.summary(), params, s.exec_cfg, dstats, beats, args.decision)
        if args.trades_out:
            write_trades(args.trades_out, s.log)
            print(f"Per-order log written to {args.trades_out}")


if __name__ == "__main__":
    main()
