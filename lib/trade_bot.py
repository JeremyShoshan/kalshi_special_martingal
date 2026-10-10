from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime, timezone

class BotMode(str, Enum):
    DRY_RUN = "dry_run"
    REAL = "real"

def format_ts_ms(ts: float | int | str) -> str:
    """Format a unix float/int timestamp as HH:MM:SS.mmm UTC."""
    try:
        t_val = float(ts)
        dt = datetime.fromtimestamp(t_val, tz=timezone.utc)
        return dt.strftime("%H:%M:%S") + f".{int(dt.microsecond / 1000):03d}"
    except Exception:
        return str(ts)

STAKE_USD = 10

@dataclass
class BotState:
    trade_num: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    total_fees: float = 0.0
    total_gross_pnl: float = 0.0
    win_pnl_total: float = 0.0
    loss_pnl_total: float = 0.0
    current_balance: float = 0.0
    total_fill_price_sum: float = 0.0
    total_stake: float = 0.0
    win_stake_total: float = 0.0
    loss_stake_total: float = 0.0
    skipped_count: int = 0
    skipped_wins: int = 0
    skipped_losses: int = 0
    filter_skipped_count: int = 0
    filter_skipped_wins: int = 0
    filter_skipped_losses: int = 0
    trigger_history: list[bool] = field(default_factory=list)
    # Special Martingale Campaign Metrics (ALGO 3)
    campaigns: int = 0
    campaign_wins: int = 0
    campaign_losses: int = 0

    @property
    def total_triggers(self) -> int:
        return self.trade_num + self.skipped_count

    @property
    def total_wins(self) -> int:
        return self.wins + self.skipped_wins

    @property
    def total_losses(self) -> int:
        return self.losses + self.skipped_losses

    @property
    def total_winrate(self) -> float:
        tot = self.total_triggers
        return (100.0 * self.total_wins / tot) if tot > 0 else 0.0

    @property
    def campaign_winrate(self) -> float:
        return (100.0 * self.campaign_wins / self.campaigns) if self.campaigns > 0 else 0.0

def calculate_dynamic_stake(
    base_stake: float,
    trigger_history: list[bool],
    rolling_window: int = 8,
    target_win_rate: float = 0.515,
    alpha: float = 1.5,
    min_stake: float | None = None,
    max_stake: float | None = None,
    max_consecutive_loss_cap: int = 3,
) -> tuple[float, float, float, int, int]:
    """
    Calculates dynamic stake based on rolling win rate of all triggers (executed trades + skips).
    Returns (calculated_stake, multiplier, rolling_wr, wins_in_window, total_in_window).
    """
    if not trigger_history or rolling_window <= 0:
        return round(base_stake, 2), 1.0, target_win_rate, 0, 0

    window = trigger_history[-rolling_window:]
    tot = len(window)
    wins = sum(1 for w in window if w)
    rolling_wr = wins / tot if tot > 0 else target_win_rate

    # Calculate raw multiplier: higher WR -> lower multiplier, lower WR -> higher multiplier
    raw_multiplier = 1.0 + alpha * (target_win_rate - rolling_wr)

    # Check consecutive losses from tail of trigger_history
    consec_losses = 0
    for w in reversed(trigger_history):
        if not w:
            consec_losses += 1
        else:
            break

    # Safety Guardrail: If consecutive losses >= max_consecutive_loss_cap, cap multiplier at <= 1.0
    if max_consecutive_loss_cap is not None and max_consecutive_loss_cap > 0:
        if consec_losses >= max_consecutive_loss_cap:
            raw_multiplier = min(raw_multiplier, 1.0)

    # Apply min/max bounds
    effective_min = min_stake if (min_stake is not None and min_stake > 0) else round(base_stake * 0.5, 2)
    effective_max = max_stake if (max_stake is not None and max_stake > 0) else round(base_stake * 1.5, 2)

    calculated_stake = base_stake * raw_multiplier
    clamped_stake = max(effective_min, min(effective_max, calculated_stake))
    final_stake = round(clamped_stake, 2)
    final_multiplier = round(final_stake / base_stake, 4) if base_stake > 0 else 1.0

    return final_stake, final_multiplier, rolling_wr, wins, tot

class TradeBot:
    def __init__(self, base_stake: float, starting_balance: float = 0.0):
        self.base_stake = base_stake
        self.state = BotState(current_balance=starting_balance)

    def record_trade(self, trade: dict[str, float | int | str | bool]) -> None:
        self.state.trade_num += 1
        pnl = float(trade["pnl"])
        self.state.total_pnl += pnl
        self.state.total_fees += float(trade.get("total_fees", 0.0))
        self.state.total_gross_pnl += float(trade.get("gross_pnl", pnl))
        self.state.total_fill_price_sum += float(trade.get("entry_ask", 0.0))
        
        # Calculate actual dollar stake
        cost = float(trade.get("cost", 0.0))
        if cost <= 0.0:
            shares = float(trade.get("shares", 0.0))
            entry_ask = float(trade.get("entry_ask", 0.0))
            cost = shares * entry_ask
            if cost <= 0.0:
                cost = self.base_stake
        self.state.total_stake += cost

        won = bool(trade["won"])
        if won:
            self.state.wins += 1
            self.state.win_pnl_total += pnl
            self.state.win_stake_total += cost
        else:
            self.state.losses += 1
            self.state.loss_pnl_total += pnl
            self.state.loss_stake_total += cost

        # Track trigger history (keep last 50)
        self.state.trigger_history.append(won)
        if len(self.state.trigger_history) > 50:
            self.state.trigger_history = self.state.trigger_history[-50:]

        # Update current balance with PnL of this trade
        self.state.current_balance += pnl
        self.print_trade_row(self.state.trade_num, trade)
        print(f"Current Balance: ${self.state.current_balance:.2f}")

    def record_skipped_trade(self, won: bool) -> None:
        self.state.skipped_count += 1
        if won:
            self.state.skipped_wins += 1
        else:
            self.state.skipped_losses += 1

        # Track trigger history for skips (keep last 50)
        self.state.trigger_history.append(bool(won))
        if len(self.state.trigger_history) > 50:
            self.state.trigger_history = self.state.trigger_history[-50:]

    def record_filter_skipped_trade(self, won: bool) -> None:
        self.state.filter_skipped_count += 1
        if won:
            self.state.filter_skipped_wins += 1
        else:
            self.state.filter_skipped_losses += 1

    def record_campaign_result(self, won: bool) -> None:
        self.state.campaigns += 1
        if won:
            self.state.campaign_wins += 1
        else:
            self.state.campaign_losses += 1

    @staticmethod
    def print_header() -> None:
        print(
            f"{'#':>4}  {'beat_time':>12}  {'side':>5}  "
            f"{'buy_time':>12}  {'exit_time':>12}  {'buy_px':>6}  {'exit_px':>6}  "
            f"{'PnL':>9}  {'result':>6}"
        )
        print("-" * 100)

    @staticmethod
    def print_trade_row(trade_num: int, trade: dict[str, float | int | str]) -> None:
        print(
            f"{trade_num:>4}  "
            f"{format_ts_ms(trade['signal_ts']):>12}  "
            f"{trade['side']:>5}  "
            f"{format_ts_ms(trade['buy_local_ts']):>12}  "
            f"{format_ts_ms(trade['sell_local_ts']):>12}  "
            f"{float(trade['entry_ask']):>6.3f}  "
            f"{float(trade['exit_bid']):>6.3f}  "
            f"${float(trade['pnl']):+8.4f}  "
            f"{trade['result']:>6}"
        )

    def print_summary(self) -> None:
        print("-" * 96)
        win_rate = (100.0 * self.state.wins / self.state.trade_num) if self.state.trade_num else 0.0
        win_rate_dollar = (100.0 * self.state.win_stake_total / self.state.total_stake) if self.state.total_stake else 0.0
        avg_price = (self.state.total_fill_price_sum / self.state.trade_num) if self.state.trade_num else 0.0
        avg_win = (self.state.win_pnl_total / self.state.wins) if self.state.wins else 0.0
        avg_loss = (self.state.loss_pnl_total / self.state.losses) if self.state.losses else 0.0
        total_winrate = self.state.total_winrate
        camp_wr = self.state.campaign_winrate
        print(
            f"Campaigns: {self.state.campaigns}  Wins: {self.state.campaign_wins}  Losses: {self.state.campaign_losses}  (WR: {camp_wr:.2f}%)  "
            f"Trades: {self.state.trade_num}  Wins: {self.state.wins}  Losses: {self.state.losses}  "
            f"Win rate: {win_rate:.2f}% (Trade) | {win_rate_dollar:.2f}% (Dollars) | {total_winrate:.2f}% (Total inc. Skips)  Avg Price: {avg_price:.4f}  "
            f"Skipped: {self.state.skipped_count} (Wins: {self.state.skipped_wins}, Losses: {self.state.skipped_losses})  "
            f"Filter_Skips: {self.state.filter_skipped_count} (Wins: {self.state.filter_skipped_wins}, Losses: {self.state.filter_skipped_losses})  "
            f"Gross PnL: ${self.state.total_gross_pnl:+.4f}  "
            f"Total Fees: ${self.state.total_fees:.4f}  "
            f"Net PnL: ${self.state.total_pnl:+.4f}  (base stake ${self.base_stake:.2f})"
        )
        if self.state.wins:
            print(f"Average WIN:  {avg_win:+.4f} per winning trade")
        if self.state.losses:
            print(f"Average LOSS: {avg_loss:+.4f} per losing trade")

def utc_beat_start_ts(ts: int, beat_minutes: int) -> int:
    """Floor a unix timestamp to the UTC wall-clock beat start (e.g. 12:30:00, 12:35:00)."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    beat_minute = (dt.minute // beat_minutes) * beat_minutes
    beat_start = dt.replace(minute=beat_minute, second=0, microsecond=0)
    return int(beat_start.timestamp())

def next_utc_beat_start(ts: int, beat_minutes: int) -> int:
    """Next UTC beat boundary at or after ts."""
    beat = utc_beat_start_ts(ts, beat_minutes)
    if beat < ts:
        beat += beat_minutes * 60
    return beat

def seconds_into_utc_beat(ts: int, beat_minutes: int) -> int:
    return ts - utc_beat_start_ts(ts, beat_minutes)
