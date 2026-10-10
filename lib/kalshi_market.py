from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Kalshi series tickers for crypto up/down markets.
# Verify live series on https://kalshi.com before trading (names can change).
DEFAULT_SERIES_BY_ASSET: dict[tuple[str, int], str] = {
    ("BTC", 5): "KXBTC5M",
    ("BTC", 15): "KXBTC15M",
    ("ETH", 5): "KXETH5M",
    ("ETH", 15): "KXETH15M",
    ("SOL", 5): "KXSOL5M",
    ("SOL", 15): "KXSOL15M",
}


def series_ticker_for(asset: str, beat_duration_min: int) -> str:
    asset = asset.upper()
    key = (asset, beat_duration_min)
    if key in DEFAULT_SERIES_BY_ASSET:
        return DEFAULT_SERIES_BY_ASSET[key]
    return f"KX{asset}{beat_duration_min}M"


def beat_ts_to_market_ticker(series_ticker: str, beat_start_ts: int, beat_duration_min: int = 15) -> str:
    """
    Build a Kalshi market ticker from a UTC beat start timestamp.
    Kalshi market tickers use the market expiration (close) timestamp formatted in America/New_York (US/Eastern) time.

    Example: A 15m beat starting at 12:00 UTC (closing at 12:15 UTC / 08:15 EDT) -> KXBTC15M-26JUL270815-15.
    """
    close_ts = beat_start_ts + beat_duration_min * 60
    close_dt = datetime.fromtimestamp(close_ts, tz=timezone.utc).astimezone(ZoneInfo("America/New_York"))
    date_part = close_dt.strftime("%y%b%d").upper()
    time_part = close_dt.strftime("%H%M")
    suffix = time_part[-2:]
    return f"{series_ticker.upper()}-{date_part}{time_part}-{suffix}"


def token_id_for_side(market_ticker: str, side: str) -> str:
    """Encode market + outcome side as a stable token_id string used by the bot."""
    outcome = "yes" if side == "Up" else "no"
    return f"{market_ticker.upper()}|{outcome}"


def parse_token_id(token_id: str) -> tuple[str, str]:
    """Return (market_ticker, outcome) where outcome is 'yes' or 'no'."""
    if "|" not in token_id:
        raise ValueError(f"Invalid Kalshi token_id: {token_id}")
    ticker, outcome = token_id.rsplit("|", 1)
    outcome = outcome.lower()
    if outcome not in ("yes", "no"):
        raise ValueError(f"Invalid outcome in token_id: {token_id}")
    return ticker.upper(), outcome


def side_from_outcome(outcome: str) -> str:
    return "Up" if outcome == "yes" else "Down"


def outcome_from_side(side: str) -> str:
    return "yes" if side == "Up" else "no"


def kalshi_price_for_side(side: str, display_price: float) -> float:
    """
    Convert a display per-contract price into Kalshi YES-leg book price.

    Up (buy YES): price is the YES bid target.
    Down (buy NO): equivalent ask on the YES book at (1 - NO price).
    """
    if side == "Up":
        return display_price
    return 1.0 - display_price


def kalshi_book_side_for_side(side: str) -> str:
    """Kalshi V2 book side: bid = buy YES, ask = sell YES (buy NO)."""
    return "bid" if side == "Up" else "ask"
